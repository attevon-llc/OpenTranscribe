"""
Profile Embedding Service for managing speaker profile embeddings.

This service handles the consolidation and management of speaker embeddings
at the profile level to improve cross-file speaker recognition accuracy.

The service provides intelligent embedding aggregation that:
- Consolidates multiple speaker embeddings into profile-level representations
- Enables accurate cross-video speaker recognition
- Maintains embedding quality through incremental updates
- Supports both PostgreSQL metadata and OpenSearch vector storage
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.embedding_models import get_speaker_index
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.services.opensearch_service import get_speaker_embedding
from app.services.opensearch_service.client import CLUSTER_UNAVAILABLE_ERRORS
from app.services.opensearch_service.client import OpenSearchUnavailableError
from app.services.opensearch_service.profiles import SPEAKER_PROFILE_MATCH_THRESHOLD
from app.services.permission_service import file_ids_in_scope
from app.services.privacy.policy import Decision
from app.services.privacy.policy import declined_file_ids
from app.services.privacy.redact_view import PrivacyView
from app.services.search.availability_field import exclude_unavailable_clause
from app.utils.cosine_space import raw_cosine_from_opensearch_score

logger = logging.getLogger(__name__)

# What an OpenSearch call here can raise: the cluster could not answer, or the service helper said so.
_OPENSEARCH_ERRORS = (*CLUSTER_UNAVAILABLE_ERRORS, OpenSearchUnavailableError)


class ProfileEmbeddingRecomputeError(RuntimeError):
    """A strict profile-embedding recompute could not replace the stored embedding."""


def _clear_profile_embedding_from_opensearch(profile_uuid: str) -> None:
    """Clear a profile embedding from OpenSearch.

    Keyed by UUID: the document id is ``profile_<uuid>``. This took the integer id
    once, which matched no document, so a profile left with no speakers kept its
    averaged voiceprint indexed.
    """
    try:
        from app.services.opensearch_service import remove_profile_embedding

        remove_profile_embedding(profile_uuid)
    except _OPENSEARCH_ERRORS as e:
        logger.warning(
            f"Could not clear profile {profile_uuid} embedding from OpenSearch: {e}", exc_info=True
        )


def _store_profile_embedding_to_opensearch(
    profile_id: int,
    profile_uuid: str,
    profile_name: str,
    embedding: list[float],
    speaker_count: int,
    user_id: int,
    organization_id: int | None = None,
) -> bool:
    """Store a profile embedding to OpenSearch (org-stamped for org profiles).

    Returns:
        Whether OpenSearch accepted the write. A failure is logged, not raised.
    """
    try:
        from app.services.opensearch_service import store_profile_embedding

        return bool(
            store_profile_embedding(
                profile_id=profile_id,
                profile_uuid=profile_uuid,
                profile_name=profile_name,
                embedding=embedding,
                speaker_count=speaker_count,
                user_id=user_id,
                organization_id=organization_id,
            )
        )
    except _OPENSEARCH_ERRORS as e:
        logger.warning(
            f"Failed to sync profile {profile_id} embedding to OpenSearch: {e}", exc_info=True
        )
        return False


def _collect_speaker_embeddings(speakers: list[Speaker]) -> dict[int, list[float]]:
    """Collect embeddings for all speakers, keyed by speaker ID.

    Uses batch fetch to avoid N+1 OpenSearch calls per speaker.

    Raises:
        OpenSearchUnavailableError: The index could not answer; an outage must not read as
            "no speaker has a voiceprint" and rewrite profile centroids from nothing.
    """
    from app.services.opensearch_service import get_speaker_embeddings_batch

    uuid_to_id = {str(s.uuid): int(s.id) for s in speakers}
    batch_result = get_speaker_embeddings_batch(list(uuid_to_id.keys()), strict=True)
    return {uuid_to_id[uuid]: emb for uuid, emb in batch_result.items() if uuid in uuid_to_id}


def _calculate_average_embedding(embeddings: list[list[float]]) -> list[float]:
    """Calculate the L2-normalized average of multiple embeddings.

    Averaging L2-normalized vectors produces a non-unit vector. Re-normalizing
    ensures consistent cosine similarity when stored alongside per-speaker
    embeddings that are always L2-normalized.
    """
    embeddings_array = np.array(embeddings)
    avg_array = np.mean(embeddings_array, axis=0)
    norm = np.linalg.norm(avg_array)
    if norm > 1e-8:
        avg_array = avg_array / norm
    result: list[float] = avg_array.tolist()
    return result


@dataclass
class _ProfileState:
    """Plain copy of a profile, so OpenSearch work needs no live ORM row (or transaction)."""

    uuid: str
    name: str
    user_id: int
    organization_id: int | None
    embedding_count: int | None
    last_embedding_update: datetime | None


@dataclass
class _MemberRef:
    """Plain copy of the one speaker field the voiceprint read needs."""

    uuid: str


def _process_profile_with_no_speakers(
    profile: SpeakerProfile | _ProfileState,
    profile_id: int,
) -> bool:
    """Handle case when profile has no speakers assigned."""
    profile.embedding_count = 0
    profile.last_embedding_update = datetime.now(UTC)
    _clear_profile_embedding_from_opensearch(str(profile.uuid))
    return True


def _process_profile_with_embeddings(
    profile: SpeakerProfile | _ProfileState,
    profile_id: int,
    embeddings: list[list[float]],
) -> bool:
    """Process a profile that has valid embeddings."""
    averaged_embedding = _calculate_average_embedding(embeddings)

    profile.embedding_count = len(embeddings)
    profile.last_embedding_update = datetime.now(UTC)

    stored = _store_profile_embedding_to_opensearch(
        profile_id=profile_id,
        profile_uuid=str(profile.uuid),
        profile_name=str(profile.name),
        embedding=averaged_embedding,
        speaker_count=len(embeddings),
        user_id=int(profile.user_id),
        # Full-recalc must preserve the tenant stamp, or an org profile's doc
        # would silently fall back to personal scope until the next backfill.
        organization_id=profile.organization_id,
    )

    if stored:
        logger.info(
            f"Updated profile {profile_id} embedding with {len(embeddings)} speaker embeddings"
        )
    return stored


def _check_opensearch_profile_prerequisites(
    user_id: int, *, strict: bool = False
) -> tuple[Any, Any, bool]:
    """
    Check prerequisites for OpenSearch profile similarity search.

    Returns:
        Tuple of (opensearch_client, None, should_continue)
    """
    from app.services.opensearch_service import opensearch_client

    if not opensearch_client:
        if strict and settings.OPENSEARCH_ENABLED:
            raise OpenSearchUnavailableError("OpenSearch client unavailable")
        logger.warning("OpenSearch client not initialized")
        return None, None, False

    if not opensearch_client.indices.exists(index=get_speaker_index()):
        logger.info("Speakers index does not exist yet, skipping profile similarity search")
        return None, None, False

    return opensearch_client, None, True


def _check_profiles_exist_in_opensearch(
    opensearch_client: Any,
    index_name: str,
    user_id: int,
    accessible_profile_ids: set[int] | None = None,
) -> bool:
    """Check if any profile documents exist for the user/scope in OpenSearch."""
    from app.services.opensearch_service import _is_index_corruption_error
    from app.services.opensearch_service import _repair_index

    # Build filter based on scope
    user_filter: dict[str, Any]
    if accessible_profile_ids is not None:
        user_filter = {"terms": {"profile_id": list(accessible_profile_ids)}}
    else:
        user_filter = {"term": {"user_id": user_id}}

    profile_check_query = {
        "size": 1,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"document_type": "profile"}},
                    user_filter,
                ]
            }
        },
    }

    try:
        profile_check = opensearch_client.search(index=index_name, body=profile_check_query)
        if profile_check["hits"]["total"]["value"] == 0:
            logger.info(f"No profile documents found for user/scope {user_id}, skipping KNN search")
            return False
    except _OPENSEARCH_ERRORS as e:
        if _is_index_corruption_error(e):
            logger.warning(f"Index corruption during profile check, attempting repair: {e}")
            if _repair_index(index_name):
                try:
                    profile_check = opensearch_client.search(
                        index=index_name, body=profile_check_query
                    )
                    if profile_check["hits"]["total"]["value"] == 0:
                        logger.info(
                            f"No profile documents found for user/scope {user_id} after repair"
                        )
                        return False
                    return True
                except _OPENSEARCH_ERRORS as retry_err:
                    logger.exception(f"Profile check retry after repair failed: {retry_err}")
        logger.warning(
            f"Profile document check failed: {e}, proceeding with KNN query", exc_info=True
        )

    return True


def _execute_knn_search(
    opensearch_client: Any,
    index_name: str,
    embedding: list[float],
    user_id: int,
    threshold: float,
    accessible_profile_ids: set[int] | None = None,
    organization_id: int | None = None,
    *,
    strict: bool = False,
) -> list[dict]:
    """Execute KNN search and return matches above threshold (``strict``: raise on failure)."""
    from app.services.opensearch_service import _is_index_corruption_error
    from app.services.opensearch_service import _repair_index
    from app.services.opensearch_service import _speaker_org_filter_clauses

    if accessible_profile_ids is not None:
        filters = [
            {"term": {"document_type": "profile"}},
            {"terms": {"profile_id": list(accessible_profile_ids)}},
        ]
    else:
        filters = [
            {"term": {"document_type": "profile"}},
            {"term": {"user_id": user_id}},
        ]
    # Tenant gate on profile docs (org term, else exclude org-stamped).
    filters.extend(_speaker_org_filter_clauses(organization_id))
    # Profile documents carry no file (a no-op today); every kNN body uses the one helper.
    filters.append(exclude_unavailable_clause())

    query = {
        "size": 25,
        "query": {
            "knn": {
                "embedding": {
                    "vector": embedding,
                    "k": 25,
                    "filter": {"bool": {"filter": filters}},
                }
            }
        },
    }

    try:
        response = opensearch_client.search(index=index_name, body=query)
    except _OPENSEARCH_ERRORS as e:
        if _is_index_corruption_error(e):
            logger.warning(f"Index corruption during KNN search, attempting repair: {e}")
            if _repair_index(index_name):
                try:
                    response = opensearch_client.search(index=index_name, body=query)
                    return _extract_matches_from_response(response, threshold)
                except _OPENSEARCH_ERRORS as retry_err:
                    logger.exception(f"KNN search retry after repair failed: {retry_err}")
        logger.error(f"Error in OpenSearch KNN search: {e}")
        if strict:
            raise OpenSearchUnavailableError("profile kNN failed") from e
        return []

    return _extract_matches_from_response(response, threshold)


def _extract_matches_from_response(response: dict, threshold: float) -> list[dict]:
    """Extract and filter matches from OpenSearch response.

    OpenSearch cosinesimil returns ``(1 + cosine) / 2`` (range 0-1).
    We convert back to raw cosine (range -1 to 1) so thresholds like
    0.75 mean real cosine similarity.
    """
    matches = []
    for hit in response["hits"]["hits"]:
        cosine_sim = raw_cosine_from_opensearch_score(hit["_score"])

        if cosine_sim < threshold:
            continue

        source = hit["_source"]
        profile_id = source.get("profile_id")
        profile_uuid = source.get("profile_uuid")
        profile_name = source.get("profile_name")

        if not profile_id or not profile_name:
            continue

        matches.append(
            {
                "profile_id": profile_id,
                "profile_uuid": profile_uuid or str(profile_id),
                "profile_name": profile_name,
                "similarity": cosine_sim,
                "embedding_count": source.get("speaker_count", 1),
                "last_update": source.get("updated_at"),
            }
        )

    return matches


def _not_recomputed(strict: bool, reason: str) -> bool:
    """The stored embedding was not replaced: raise for a strict caller, else report False."""
    if strict:
        raise ProfileEmbeddingRecomputeError(reason)
    return False


def _consenting_member_speakers(
    db: Session, profile: SpeakerProfile, profile_id: int
) -> list[Speaker]:
    """The profile's speakers that may shape its voiceprint.

    Only speakers whose file is of the profile's tenant — a speaker linked across
    tenants never shapes the voiceprint (NULL-safe: personal profile <-> personal
    files) — and, per-file consent (#1356), never a voice from an opted-out file.
    """
    speakers = (
        db.query(Speaker)
        .filter(
            Speaker.profile_id == profile_id,
            Speaker.media_file_id.in_(file_ids_in_scope(profile.organization_id)),
        )
        .all()
    )
    excluded = declined_file_ids(db, (s.media_file_id for s in speakers))
    return [s for s in speakers if s.media_file_id not in excluded]


def _member_embeddings(
    speakers: Sequence[Speaker | _MemberRef], profile_id: int
) -> list[list[float]]:
    """Stored embeddings of the given speakers, skipping (and logging) unreadable ones."""
    embeddings: list[list[float]] = []
    for speaker in speakers:
        embedding = get_speaker_embedding(str(speaker.uuid))
        if embedding:
            embeddings.append(embedding)
        else:
            logger.warning(f"No embedding found for speaker {speaker.uuid} in profile {profile_id}")
    return embeddings


def _discard_orphaned_profile_document(profile_uuid: str) -> None:
    """Clear the document of a profile deleted while its embedding was being written."""
    _clear_profile_embedding_from_opensearch(profile_uuid)


def _write_profile_embedding_state(db: Session, profile_id: int, snapshot: Any) -> bool:
    """The short write phase: persist the count the OpenSearch document was built from.

    Re-addresses the row by primary key instead of resuming the ORM instance, so a profile
    renamed or deleted while OpenSearch ran is neither clobbered nor an error. A profile
    deleted meanwhile leaves a document nothing owns; the caller clears it (this function
    holds a session, and the clear is OpenSearch work).
    """
    updated = (
        db.query(SpeakerProfile)
        .filter(SpeakerProfile.id == profile_id)
        .update(
            {
                SpeakerProfile.embedding_count: snapshot.embedding_count,
                SpeakerProfile.last_embedding_update: snapshot.last_embedding_update,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return updated != 0


class ProfileEmbeddingService:
    """Service for managing profile-level speaker embeddings."""

    @staticmethod
    def update_profile_embedding(db: Session, profile_id: int, *, strict: bool = False) -> bool:
        """
        Update the consolidated embedding for a speaker profile by averaging
        all embeddings from speakers assigned to this profile.

        Args:
            db: Database session
            profile_id: ID of the speaker profile to update
            strict: Raise :class:`ProfileEmbeddingRecomputeError` whenever the stored
                embedding was NOT replaced by one computed from the current members (no
                member left, no readable member voiceprint, a refused or failed write)
                instead of returning ``False``. A speaker leaving the profile needs this
                (issue #1214): the old embedding still averages in that speaker's voice,
                so the caller must erase it rather than keep it.

        Returns:
            True if successful, False otherwise
        """
        try:
            # Get the profile
            profile = db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()
            if not profile:
                logger.error(f"Profile {profile_id} not found")
                return _not_recomputed(strict, f"profile {profile_id} not found")

            speakers = _consenting_member_speakers(db, profile, profile_id)

            # Privacy policy (#1280): an averaged profile voiceprint is a voiceprint write.
            # Clearing an emptied profile is a delete and stays allowed; computing a new
            # average is not. Under `strict` (a speaker left the profile) the raise makes
            # the caller ERASE the stored embedding, which is right: it still holds the
            # removed voice and cannot be rebuilt while the scope is off.
            decision = (
                PrivacyView(db).voiceprints_for_scope(int(profile.user_id), profile.organization_id)
                if speakers
                else None
            )

            # Three phases: the reads above, then OpenSearch with NO Postgres transaction
            # open, then one short write. Everything the middle phase needs is copied to
            # plain objects first, because ``commit`` expires the ORM rows (a later
            # attribute read would reopen the very transaction being released).
            snapshot = _ProfileState(
                uuid=str(profile.uuid),
                name=str(profile.name),
                user_id=int(profile.user_id),
                organization_id=profile.organization_id,
                embedding_count=profile.embedding_count,
                last_embedding_update=profile.last_embedding_update,
            )
            member_stubs = [_MemberRef(uuid=str(s.uuid)) for s in speakers]
            db.commit()

            if not speakers:
                logger.warning(f"No speakers assigned to profile {profile_id}")
                _process_profile_with_no_speakers(snapshot, profile_id)
                if not _write_profile_embedding_state(db, profile_id, snapshot):
                    _discard_orphaned_profile_document(snapshot.uuid)
                if strict:
                    # The best-effort clear above swallows its own failure; the strict
                    # caller erases again and verifies the document is gone.
                    raise ProfileEmbeddingRecomputeError(f"profile {profile_id} has no members")
                return True

            assert decision is not None
            if not decision.enabled:
                logger.info(
                    "Profile %s embedding not recomputed: voiceprints off (%s)",
                    profile_id,
                    decision.locked_by,
                )
                return _not_recomputed(
                    strict, f"profile {profile_id} voiceprints disabled by privacy policy"
                )

            embeddings = _member_embeddings(member_stubs, profile_id)
            if not embeddings:
                logger.warning(f"No valid embeddings found for profile {profile_id}")
                return _not_recomputed(strict, f"profile {profile_id} has no member voiceprint")

            # Delegate averaging + OpenSearch storage to shared helpers
            stored = _process_profile_with_embeddings(snapshot, profile_id, embeddings)
            if not _write_profile_embedding_state(db, profile_id, snapshot):
                _discard_orphaned_profile_document(snapshot.uuid)
            if not stored:
                return _not_recomputed(
                    strict, f"OpenSearch refused the embedding write for profile {profile_id}"
                )

            logger.info(
                f"Updated profile {profile_id} embedding with average of {len(embeddings)} speaker embeddings"
            )
            return True

        except ProfileEmbeddingRecomputeError:
            raise
        except (SQLAlchemyError, *_OPENSEARCH_ERRORS, ValueError, TypeError) as e:
            logger.error(f"Error updating profile embedding for profile {profile_id}: {e}")
            db.rollback()
            if strict:
                raise ProfileEmbeddingRecomputeError(str(e)) from e
            return False

    @staticmethod
    def _group_speakers_by_profile(speakers: list[Speaker]) -> dict[int, list[Speaker]]:
        """Group speakers by their profile ID."""
        speakers_by_profile: dict[int, list[Speaker]] = {}
        for speaker in speakers:
            # Callers pass speakers filtered on profile_id (non-None grouping key)
            assert speaker.profile_id is not None
            profile_id = int(speaker.profile_id)
            if profile_id not in speakers_by_profile:
                speakers_by_profile[profile_id] = []
            speakers_by_profile[profile_id].append(speaker)
        return speakers_by_profile

    @staticmethod
    def _process_single_profile_in_batch(
        profile: SpeakerProfile,
        profile_id: int,
        speakers: list[Speaker],
        speaker_embeddings: dict[int, list[float]],
    ) -> bool:
        """Process a single profile during batch update."""
        if not speakers:
            return _process_profile_with_no_speakers(profile, profile_id)

        embeddings = [
            speaker_embeddings[int(speaker.id)]
            for speaker in speakers
            if int(speaker.id) in speaker_embeddings
        ]

        if not embeddings:
            logger.warning(f"No valid embeddings found for profile {profile_id}")
            return False

        return _process_profile_with_embeddings(profile, profile_id, embeddings)

    @staticmethod
    def profiles_off_by_policy(db: Session, profile_ids: list[int]) -> dict[int, Decision]:
        """The profiles among ``profile_ids`` whose owner scope has voiceprints switched off.

        Returns ``profile_id -> Decision`` for the off ones only (so ``locked_by`` says which
        layer). A profile that does not exist is not reported here.
        """
        if not profile_ids:
            return {}
        view = PrivacyView(db)
        off: dict[int, Decision] = {}
        for pid, owner, org in (
            db.query(SpeakerProfile.id, SpeakerProfile.user_id, SpeakerProfile.organization_id)
            .filter(SpeakerProfile.id.in_(profile_ids))
            .all()
        ):
            decision = view.voiceprints_for_scope(int(owner), org)
            if not decision.enabled:
                off[int(pid)] = decision
        return off

    @staticmethod
    def batch_update_profile_embeddings(db: Session, profile_ids: list[int]) -> dict[int, bool]:
        """
        Update embeddings for multiple profiles in a batch operation.

        This method efficiently processes multiple profiles by:
        - Collecting all speaker data in bulk queries
        - Processing embeddings in batches
        - Minimizing database round trips

        Args:
            db: SQLAlchemy database session
            profile_ids: List of profile IDs to update

        Returns:
            Dictionary mapping profile_id to success status (True/False)
        """
        if not profile_ids:
            return {}

        results: dict[int, bool] = {}

        try:
            # Bulk fetch all profiles and speakers
            profiles = db.query(SpeakerProfile).filter(SpeakerProfile.id.in_(profile_ids)).all()
            profile_map: dict[int, SpeakerProfile] = {
                int(profile.id): profile for profile in profiles
            }

            all_speakers = db.query(Speaker).filter(Speaker.profile_id.in_(profile_ids)).all()
            speakers_by_profile = ProfileEmbeddingService._group_speakers_by_profile(all_speakers)
            speaker_embeddings = _collect_speaker_embeddings(all_speakers)

            # Privacy policy (#1280): a profile whose scope is off is left exactly as it is
            # (reported False, never written). Callers that need to tell "skipped" from
            # "failed" ask `profiles_off_by_policy` first.
            policy_off = ProfileEmbeddingService.profiles_off_by_policy(db, list(profile_map))

            # Process each profile
            for profile_id in profile_ids:
                profile = profile_map.get(profile_id)
                if not profile:
                    logger.error(f"Profile {profile_id} not found in batch")
                    results[profile_id] = False
                    continue
                if profile_id in policy_off:
                    results[profile_id] = False
                    continue

                try:
                    speakers = speakers_by_profile.get(profile_id, [])
                    results[profile_id] = ProfileEmbeddingService._process_single_profile_in_batch(
                        profile, profile_id, speakers, speaker_embeddings
                    )
                except (
                    Exception
                ) as e:  # boundary: per-item isolation, one profile must not sink the batch
                    logger.exception(f"Error updating profile {profile_id} in batch: {e}")
                    results[profile_id] = False

            db.commit()
            success_count = sum(1 for r in results.values() if r)
            logger.info(f"Batch updated {success_count} profiles successfully")

        except (SQLAlchemyError, *_OPENSEARCH_ERRORS, ValueError, TypeError) as e:
            logger.exception(f"Error in batch profile embedding update: {e}")
            db.rollback()
            for profile_id in profile_ids:
                results[profile_id] = False

        return results

    @staticmethod
    def get_profile_embedding(db: Session, profile_id: int) -> list[float] | None:
        """
        Get the consolidated embedding for a speaker profile.

        Args:
            db: Database session
            profile_id: ID of the speaker profile

        Returns:
            The profile's embedding vector, or None if not available
        """
        try:
            # Get profile object to extract UUID
            from app.models.speaker import SpeakerProfile

            profile = db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()
            if not profile:
                logger.error(f"Profile {profile_id} not found")
                return None

            # Get embedding from OpenSearch (primary storage for vectors) using UUID
            from app.services.opensearch_service import get_profile_embedding

            result = get_profile_embedding(str(profile.uuid))
            return list(result) if result else None

        except (SQLAlchemyError, *_OPENSEARCH_ERRORS) as e:
            logger.exception(f"Error retrieving profile embedding for profile {profile_id}: {e}")
            return None

    @staticmethod
    def calculate_profile_similarity(
        embedding: list[float],
        user_id: int,
        threshold: float = SPEAKER_PROFILE_MATCH_THRESHOLD,
        accessible_profile_ids: set[int] | None = None,
        organization_id: int | None = None,
        *,
        strict: bool = False,
    ) -> list[dict]:
        """
        Find speaker profiles using OpenSearch native kNN search for optimal performance.

        Completely leverages OpenSearch's HNSW algorithm with cosine similarity
        instead of manual calculations for maximum efficiency.

        Args:
            embedding: The embedding to compare against
            user_id: User ID to filter profiles
            threshold: Minimum similarity threshold
            accessible_profile_ids: Optional set of profile IDs to search within
                (for shared profile scope). If None, filters by user_id.
            organization_id: Active org id (None = personal) — tenant gate.
            strict: Raise instead of returning ``[]`` when the cluster could not
                answer, for callers that act on "no profile matched".

        Returns:
            List of matching profiles with similarity scores

        Raises:
            OpenSearchUnavailableError: ``strict`` and the search failed.
        """
        try:
            opensearch_client, _, should_continue = _check_opensearch_profile_prerequisites(
                user_id, strict=strict
            )
            if not should_continue:
                return []

            speaker_index = get_speaker_index()
            if not _check_profiles_exist_in_opensearch(
                opensearch_client,
                speaker_index,
                user_id,
                accessible_profile_ids=accessible_profile_ids,
            ):
                return []

            matches = _execute_knn_search(
                opensearch_client,
                speaker_index,
                embedding,
                user_id,
                threshold,
                accessible_profile_ids=accessible_profile_ids,
                organization_id=organization_id,
                strict=strict,
            )

            logger.info(
                f"OpenSearch found {len(matches)} profile matches above threshold {threshold}"
            )
            return matches

        except (*_OPENSEARCH_ERRORS, ValueError, KeyError, TypeError) as e:
            logger.error(f"Error in OpenSearch profile similarity search: {e}")
            if strict:
                if isinstance(e, OpenSearchUnavailableError):
                    raise
                raise OpenSearchUnavailableError("profile similarity search failed") from e
            return []
