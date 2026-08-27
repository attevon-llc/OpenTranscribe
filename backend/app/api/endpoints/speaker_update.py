"""
Speaker Update and Profile Management Module.

This module provides automatic speaker profile creation and management functionality.
It handles the core business logic for:
- Automatic profile creation when speakers are labeled
- Cross-video speaker matching and assignment
- Profile embedding updates and consolidation

The module implements intelligent speaker recognition that learns from user labeling
patterns and automatically groups speakers across multiple videos.
"""

import logging
import uuid
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerMatch
from app.models.media import SpeakerProfile
from app.services.opensearch_service import get_speaker_embedding

logger = logging.getLogger(__name__)


def calculate_cosine_similarity(embedding1: np.ndarray, embedding2: np.ndarray) -> float:
    """
    Calculate cosine similarity between two voice embeddings.

    This function now delegates to the centralized SimilarityService for
    optimal performance and consistency across the application.

    Args:
        embedding1 (np.ndarray): First voice embedding vector.
        embedding2 (np.ndarray): Second voice embedding vector.

    Returns:
        float: Similarity score between 0 and 1, where 1 is identical voices.
    """
    from app.services.similarity_service import SimilarityService

    return SimilarityService.cosine_similarity(embedding1, embedding2)


def auto_create_or_assign_profile(speaker: Speaker, display_name: str, db: Session) -> bool:
    """
    Automatically create or assign speaker to a profile when labeled.

    This function implements the core auto-profiling logic:
    1. Searches for existing profiles with the same name (case-insensitive)
    2. If found, assigns speaker to existing profile and updates embedding
    3. If not found, creates new profile and assigns speaker
    4. Updates the profile's consolidated voice embedding

    Args:
        speaker (Speaker): The speaker instance to assign to a profile.
        display_name (str): The display name/label for the speaker.
        db (Session): SQLAlchemy database session.

    Returns:
        bool: True if profile was successfully created/assigned, False otherwise.

    Raises:
        Exception: Logs errors but does not re-raise to avoid breaking speaker updates.
    """
    try:
        # Check if a profile with this name already exists for this user
        existing_profile = (
            db.query(SpeakerProfile)
            .filter(
                SpeakerProfile.user_id == speaker.user_id,
                SpeakerProfile.name.ilike(display_name.strip()),
            )
            .first()
        )

        if existing_profile:
            # Assign speaker to existing profile
            speaker.profile_id = existing_profile.id
            logger.info(
                f"Assigned speaker {speaker.id} to existing profile '{existing_profile.name}' (ID: {existing_profile.id})"
            )

            # Update profile embedding
            try:
                from app.services.profile_embedding_service import ProfileEmbeddingService

                ProfileEmbeddingService.add_speaker_to_profile_embedding(
                    db, speaker.id, existing_profile.id
                )
            except Exception as e:
                logger.warning(f"Failed to update profile embedding: {e}")

            # Sync profile assignment to OpenSearch
            try:
                from app.services.opensearch_service import update_speaker_profile

                # Get profile UUID if profile is assigned
                profile_uuid = None
                if speaker.profile_id and existing_profile:
                    profile_uuid = str(existing_profile.uuid)

                update_speaker_profile(
                    speaker_uuid=str(speaker.uuid),
                    profile_id=int(speaker.profile_id) if speaker.profile_id else None,
                    profile_uuid=profile_uuid,
                    verified=bool(speaker.verified),
                )
                logger.info(f"Synced speaker {speaker.uuid} profile assignment to OpenSearch")
            except Exception as e:
                logger.warning(f"Failed to sync speaker {speaker.uuid} profile to OpenSearch: {e}")

        else:
            # Create new profile for this speaker name (org-stamped from the
            # speaker's tenant — issue #262e; None = personal/community).
            new_profile = SpeakerProfile(
                user_id=speaker.user_id,
                name=display_name.strip(),
                description=f"Auto-created profile for {display_name.strip()}",
                uuid=str(uuid.uuid4()),
                organization_id=int(speaker.organization_id) if speaker.organization_id else None,
            )
            db.add(new_profile)
            db.flush()  # Get the ID without committing

            # Assign speaker to new profile
            speaker.profile_id = new_profile.id
            logger.info(
                f"Created new profile '{new_profile.name}' (ID: {new_profile.id}) and assigned speaker {speaker.id}"
            )

            # Initialize profile embedding
            try:
                from app.services.profile_embedding_service import ProfileEmbeddingService

                ProfileEmbeddingService.add_speaker_to_profile_embedding(
                    db, speaker.id, new_profile.id
                )
            except Exception as e:
                logger.warning(f"Failed to initialize profile embedding: {e}")

            # Sync new profile assignment to OpenSearch
            try:
                from app.services.opensearch_service import update_speaker_profile

                update_speaker_profile(
                    speaker_uuid=str(speaker.uuid),
                    profile_id=int(speaker.profile_id) if speaker.profile_id else None,
                    profile_uuid=str(new_profile.uuid),
                    verified=bool(speaker.verified),
                )
                logger.info(f"Synced speaker {speaker.uuid} new profile assignment to OpenSearch")
            except Exception as e:
                logger.warning(
                    f"Failed to sync speaker {speaker.uuid} new profile to OpenSearch: {e}"
                )

        return True

    except Exception as e:
        logger.exception(f"Error in auto profile creation/assignment: {e}")
        return False


def _get_profile_uuid(db: Session, profile_id: int | None) -> str | None:
    """Get the UUID string for a profile by its ID."""
    if not profile_id:
        return None
    profile = db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()
    return str(profile.uuid) if profile else None


def _speaker_sync_document(db: Session, speaker: Speaker) -> dict[str, Any]:
    """Snapshot the fields a speaker's search document carries, as **plain data**.

    Reading the profile UUID may cost a second SELECT, so it is resolved here, while
    a session is legitimately open, rather than during the push. Nothing in the
    returned dict is an ORM instance, so the caller can close its transaction before
    any of it reaches the network.
    """
    profile_id = int(speaker.profile_id) if speaker.profile_id else None
    return {
        "speaker_id": int(speaker.id),
        "speaker_uuid": str(speaker.uuid),
        "display_name": str(speaker.display_name) if speaker.display_name else None,
        "profile_id": profile_id,
        "profile_uuid": _get_profile_uuid(db, profile_id),
        "verified": bool(speaker.verified),
    }


def _push_speaker_documents(documents: list[dict[str, Any]]) -> None:
    """Write speaker documents to the search index.

    **Takes no ``Session``.** Two OpenSearch round trips per document, and
    retroactive matching produces one document per matched speaker across the whole
    library — the exact per-item loop that used to run inside a single transaction.
    Each document is pushed independently so one index failure cannot drop the rest,
    matching the previous per-speaker try/except.
    """
    from app.services.opensearch_service import update_speaker_display_name
    from app.services.opensearch_service import update_speaker_profile

    for document in documents:
        try:
            update_speaker_display_name(document["speaker_uuid"], document["display_name"])
            update_speaker_profile(
                speaker_uuid=document["speaker_uuid"],
                profile_id=document["profile_id"],
                profile_uuid=document["profile_uuid"],
                verified=document["verified"],
            )
            logger.info(
                f"Synced speaker {document['speaker_id']} to OpenSearch: "
                f"display_name='{document['display_name']}', "
                f"profile_id={document['profile_id']}, verified={document['verified']}"
            )
        except Exception as e:
            logger.exception(f"Failed to sync speaker {document['speaker_id']} to OpenSearch: {e}")


def _update_profile_embedding(db: Session, speaker_id: int, profile_id: int) -> None:
    """Update the profile embedding when a speaker is added to a profile."""
    try:
        from app.services.profile_embedding_service import ProfileEmbeddingService

        ProfileEmbeddingService.add_speaker_to_profile_embedding(db, speaker_id, profile_id)
    except Exception as e:
        logger.warning(f"Failed to update profile embedding for speaker {speaker_id}: {e}")


def _chunk_rename_for(db: Session, speaker: Speaker) -> tuple[str, str] | None:
    """The ``(file_uuid, old_name)`` pair a rename of ``speaker`` invalidates.

    Read **before** the display name is overwritten: the chunk plane was indexed
    with ``display_name or name``, and after the write nothing in Postgres can
    reconstruct which of the two it was (issue #405).

    Returns **plain data**, never an ORM instance, so the caller can dispatch it
    with the transaction already committed.
    """
    old_chunk_name = str(speaker.display_name or speaker.name or "")
    if not old_chunk_name or not speaker.media_file_id:
        return None
    row = db.query(MediaFile.uuid).filter(MediaFile.id == speaker.media_file_id).first()
    return (str(row[0]), old_chunk_name) if row else None


def _apply_high_confidence_match(
    db: Session, speaker: Speaker, trigger: dict[str, Any]
) -> tuple[dict[str, Any], tuple[str, str] | None]:
    """Apply automatic labeling for high confidence matches (75%+).

    **Postgres only.** The OpenSearch write this used to do inline is now returned
    as a plain document and pushed by ``_push_speaker_documents`` after the
    transaction commits — one index round trip per auto-applied speaker was
    happening inside the matching transaction, once per match, for the whole library.

    Returns:
        ``(document, chunk_rename)`` — the speaker's search document, and the
        ``(file_uuid, old_name)`` pair whose chunk documents this label just made
        stale (``None`` when there is nothing to rewrite). The rename is captured
        **before** the assignment below, because after it Postgres can no longer
        say what the chunks were indexed with (issue #405).
    """
    chunk_rename = _chunk_rename_for(db, speaker)

    speaker.display_name = trigger["display_name"]  # type: ignore[assignment]
    speaker.verified = True  # type: ignore[assignment]

    if trigger["profile_id"]:
        speaker.profile_id = trigger["profile_id"]  # type: ignore[assignment]
        _update_profile_embedding(db, speaker.id, int(trigger["profile_id"]))

    logger.info(
        f"Auto-applied {trigger['display_name']} to {speaker.name} "
        f"({speaker.confidence:.1%} confidence)"
    )
    return _speaker_sync_document(db, speaker), chunk_rename


def _score_candidates(
    embedding_array: np.ndarray,
    candidates: list[dict[str, Any]],
    trigger_display_name: str | None,
) -> list[dict[str, Any]]:
    """Fetch each candidate's voiceprint and score it against the labeled speaker.

    **Takes no ``Session``.** This is one OpenSearch read per candidate — every
    speaker the user owns — and it used to run inside the caller's transaction, with
    the write for each match interleaved. Reads and writes are now separated so the
    N round trips happen with nothing held.

    Returns the candidates at or above the 0.5 suggestion floor, each with its
    ``similarity``; ordering follows the input.
    """
    scored: list[dict[str, Any]] = []

    for candidate in candidates:
        # Skip already verified speakers with different names
        if (
            candidate["verified"]
            and candidate["display_name"]
            and candidate["display_name"] != trigger_display_name
        ):
            logger.info(
                f"Skipping speaker {candidate['id']} ({candidate['name']}): "
                f"already verified as '{candidate['display_name']}'"
            )
            continue

        other_embedding = get_speaker_embedding(candidate["uuid"])
        if not other_embedding:
            logger.warning(
                f"No embedding found for speaker {candidate['uuid']} ({candidate['name']})"
            )
            continue

        similarity = calculate_cosine_similarity(embedding_array, np.array(other_embedding))
        logger.info(
            f"Similarity between {trigger_display_name} and {candidate['name']}: {similarity:.3f}"
        )

        if similarity < 0.5:
            continue

        scored.append({**candidate, "similarity": similarity})

    return scored


def _persist_match_results(
    db: Session, trigger: dict[str, Any], scored: list[dict[str, Any]]
) -> tuple[int, int, list[dict[str, Any]], list[tuple[str, str]]]:
    """Write every scored match to Postgres.

    Returns ``(auto_applied_count, suggested_count, documents, chunk_renames)``.
    ``documents`` are the search documents for the auto-applied speakers and
    ``chunk_renames`` the ``(file_uuid, old_name)`` pairs their new labels left
    stale in the chunk plane (issue #405); both are dispatched once the
    transaction has committed.
    """
    documents: list[dict[str, Any]] = []
    chunk_renames: list[tuple[str, str]] = []
    auto_applied_count = 0
    suggested_count = 0

    if not scored:
        return auto_applied_count, suggested_count, documents, chunk_renames

    rows = {
        int(row.id): row
        for row in db.query(Speaker).filter(Speaker.id.in_([m["id"] for m in scored])).all()
    }

    for match in scored:
        speaker = rows.get(match["id"])
        if speaker is None:
            continue

        similarity = match["similarity"]
        speaker.confidence = similarity  # type: ignore[assignment]
        speaker.suggested_name = trigger["display_name"]  # type: ignore[assignment]
        speaker.suggestion_source = "voice_match"  # type: ignore[assignment]
        store_speaker_match(trigger["id"], speaker.id, similarity, db)

        if similarity >= 0.75:
            document, chunk_rename = _apply_high_confidence_match(db, speaker, trigger)
            documents.append(document)
            if chunk_rename:
                chunk_renames.append(chunk_rename)
            auto_applied_count += 1
            continue

        # Medium confidence (50-75%): just suggest. Only ``suggested_name`` is
        # written, so ``display_name`` — the value the chunk plane carries — is
        # unchanged and this tier contributes no rename.
        logger.info(
            f"Suggested {trigger['display_name']} for {speaker.name} ({similarity:.1%} confidence)"
        )
        suggested_count += 1

    return auto_applied_count, suggested_count, documents, chunk_renames


def _dispatch_chunk_renames(chunk_renames: list[tuple[str, str]], new_name: str | None) -> None:
    """Queue chunk-plane propagation for every auto-applied label (issue #405).

    **Takes no ``Session``.** ``new_name`` arrives as plain data off the trigger
    snapshot, and the pairs were captured before their speakers were overwritten,
    so this runs with the transaction already committed — which is also what makes
    it correct: a rolled-back match must never reach the index.

    Best-effort: retroactive matching has already committed, and a broker that is
    down must not turn that into an exception the caller reports as a failed
    labelling.
    """
    if not chunk_renames or not new_name:
        return
    try:
        from app.tasks.rename_propagation_task import dispatch_speaker_rename

        files = dispatch_speaker_rename(chunk_renames, new_name)
        logger.info(f"Queued chunk speaker-rename propagation for {files} file(s)")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not queue chunk-plane rename propagation: {e}")


def _load_suggestion_speaker_documents(
    db: Session, trigger: dict[str, Any]
) -> list[dict[str, Any]]:
    """Snapshot every medium-confidence suggestion this label produced.

    Postgres only — the documents it returns are pushed later, with no session
    open. The caller must have committed first: ``autoflush`` is off on this
    project's sessionmaker, so pending confidences would otherwise be invisible to
    the filter below.
    """
    try:
        suggestion_speakers = (
            db.query(Speaker)
            .filter(
                Speaker.user_id == trigger["user_id"],
                Speaker.suggested_name == trigger["display_name"],
                Speaker.confidence >= 0.5,
                Speaker.confidence < 0.75,
                Speaker.verified == False,  # noqa: E712 - SQLAlchemy requires == for SQL generation
            )
            .all()
        )
        return [_speaker_sync_document(db, speaker) for speaker in suggestion_speakers]
    except Exception as e:
        logger.exception(f"Error collecting suggestion speakers for OpenSearch sync: {e}")
        return []


def _send_bulk_update_notification(
    trigger: dict[str, Any], auto_applied_count: int, suggested_count: int
) -> None:
    """Send WebSocket notification about bulk speaker updates.

    Takes the labeled speaker as plain data: the caller has committed by this point,
    and reading an attribute off an expired ORM instance would open a fresh
    transaction just to render a notification.

    ``speakers_bulk_updated`` has no dedicated frontend handler (issue #603) — it is
    absent from ``NotificationType`` in ``frontend/src/stores/websocket.ts``, so it
    falls through the generic unmatched-type path and is stored in
    ``$ws.notifications`` without a targeted UI reaction. Harmless (the per-speaker
    ``speaker_processing_complete``/``speaker_updated`` events already drive the
    file-detail page's live update), but intentionally unhandled rather than an
    oversight — leave it emitting until a UI surface actually consumes it.
    """
    if auto_applied_count == 0:
        return

    try:
        import asyncio

        from app.api.websockets import publish_notification

        coro = publish_notification(
            user_id=trigger["user_id"],
            notification_type="speakers_bulk_updated",
            data={
                "trigger_speaker_id": trigger["id"],
                "display_name": str(trigger["display_name"]),
                "auto_applied_count": auto_applied_count,
                "suggested_count": suggested_count,
                "message": f"Auto-applied '{trigger['display_name']}' to {auto_applied_count} additional speakers",
            },
        )

        # This sync function runs in a threadpool, so schedule on the main event loop
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            # No running loop in this thread - use run() to create one
            asyncio.run(coro)

        logger.info(
            f"Sent WebSocket notification for bulk speaker update: {auto_applied_count} speakers"
        )
    except Exception as e:
        logger.warning(f"Failed to send WebSocket notification for bulk speaker update: {e}")


def trigger_retroactive_matching(updated_speaker: Speaker, db: Session) -> dict[str, int]:
    """
    Apply retroactive voice matching when a speaker is labeled.

    This function implements intelligent cross-video speaker recognition:
    1. Compares the newly labeled speaker's voice against all other speakers
    2. Auto-applies labels for high confidence matches (>=75%)
    3. Creates suggestions for medium confidence matches (50-74%)
    4. Automatically assigns matched speakers to the same profile

    The function uses voice embedding similarity to identify the same speaker
    across different videos and automatically consolidates them under a single
    profile, reducing manual labeling effort.

    Args:
        updated_speaker (Speaker): The speaker that was just labeled by the user.
        db (Session): SQLAlchemy database session.

    Returns:
        dict with 'auto_applied_count' and 'suggested_count' keys

    Note:
        - Only processes unverified speakers or those with matching names
        - Updates profile embeddings for all automatically assigned speakers
        - Logs detailed information about matching decisions for debugging

    Session lifetime:
        This runs in ``process_speaker_update_background`` and used to hold the
        caller's transaction across *every* phase — one OpenSearch voiceprint read
        per speaker the user owns, plus two index writes per match. A plain SELECT
        holds ACCESS SHARE for the life of its transaction, so that queued every
        ``ALTER TABLE`` (an Alembic upgrade) and pinned the vacuum horizon for as
        long as the whole pass took. It is now read → commit → score (no
        transaction) → write → commit → push (no transaction). The ``db.commit()``
        calls are deliberate: they are what actually releases the locks, and this
        function has always owned the transaction boundary here (the pre-split code
        committed mid-body too).
    """
    try:
        # Phase 0 — snapshot the labeled speaker as plain data, then release the
        # transaction the caller handed us. Nothing below touches the instance.
        trigger: dict[str, Any] = {
            "id": int(updated_speaker.id),
            "uuid": str(updated_speaker.uuid),
            "user_id": int(updated_speaker.user_id),
            "display_name": str(updated_speaker.display_name)
            if updated_speaker.display_name
            else None,
            "profile_id": int(updated_speaker.profile_id) if updated_speaker.profile_id else None,
        }
        logger.info(
            f"Starting retroactive matching for speaker {trigger['id']} "
            f"labeled as '{trigger['display_name']}'"
        )
        db.commit()

        # Phase 1 — voiceprints and similarity. NO transaction is open here.
        embedding = get_speaker_embedding(trigger["uuid"])
        if not embedding:
            logger.warning(f"No embedding found for speaker {trigger['uuid']}")
            return {"auto_applied_count": 0, "suggested_count": 0}

        embedding_array = np.array(embedding)

        candidates = [
            {
                "id": int(row.id),
                "uuid": str(row.uuid),
                "name": str(row.name),
                "display_name": row.display_name,
                "verified": bool(row.verified),
            }
            for row in db.query(
                Speaker.id,
                Speaker.uuid,
                Speaker.name,
                Speaker.display_name,
                Speaker.verified,
            )
            .filter(
                Speaker.user_id == trigger["user_id"],
                Speaker.id != trigger["id"],
            )
            .all()
        ]
        db.commit()

        logger.info(
            f"Checking {len(candidates)} speakers for matches with {trigger['display_name']}"
        )
        scored = _score_candidates(embedding_array, candidates, trigger["display_name"])

        # Phase 2 — the writes, then commit so the suggestion snapshot below sees
        # them (autoflush is off) and the locks are released before the push.
        auto_applied_count, suggested_count, documents, chunk_renames = _persist_match_results(
            db, trigger, scored
        )
        db.commit()
        documents.extend(_load_suggestion_speaker_documents(db, trigger))
        db.commit()

        # Phase 3 — index writes. NO transaction is open here. Auto-applied labels
        # rewrite the chunk plane too, and one recording can contribute several
        # matched speakers — dispatch_speaker_rename coalesces them into one
        # update_by_query per file (issue #405). Both run after the commit so a
        # rolled-back match never reaches the index.
        _push_speaker_documents(documents)
        _dispatch_chunk_renames(chunk_renames, trigger["display_name"])

        logger.info(
            f"Retroactive matching complete: {auto_applied_count} auto-applied, {suggested_count} suggested"
        )

        _send_bulk_update_notification(trigger, auto_applied_count, suggested_count)

        return {"auto_applied_count": auto_applied_count, "suggested_count": suggested_count}

    except Exception as e:
        logger.exception(f"Error in retroactive matching: {e}")
        db.rollback()
        return {"auto_applied_count": 0, "suggested_count": 0}


def store_speaker_match(speaker1_id: int, speaker2_id: int, confidence: float, db: Session) -> None:
    """
    Store or update a speaker voice similarity match in the database.

    Maintains a record of voice similarity scores between speakers for
    analytics and debugging purposes. Uses consistent ID ordering to
    avoid duplicate entries.

    Args:
        speaker1_id (int): ID of the first speaker.
        speaker2_id (int): ID of the second speaker.
        confidence (float): Voice similarity confidence score (0-1).
        db (Session): SQLAlchemy database session.

    Returns:
        None
    """
    # Ensure consistent ordering (smaller ID first)
    smaller_id = min(speaker1_id, speaker2_id)
    larger_id = max(speaker1_id, speaker2_id)

    # Check if match already exists
    existing_match = (
        db.query(SpeakerMatch)
        .filter(
            SpeakerMatch.speaker1_id == smaller_id,
            SpeakerMatch.speaker2_id == larger_id,
        )
        .first()
    )

    if existing_match:
        # Update confidence if higher
        if confidence > existing_match.confidence:
            existing_match.confidence = confidence  # type: ignore[assignment]
    else:
        # Create new match
        speaker_match = SpeakerMatch(
            speaker1_id=smaller_id, speaker2_id=larger_id, confidence=confidence
        )
        db.add(speaker_match)
