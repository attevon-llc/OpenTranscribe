"""Finalize step of the v3->v4 speaker embedding migration.

Backfills v4 profile documents, validates counts and swaps the speaker alias.
"""

import logging
from typing import Any

from app.core.celery import celery_app
from app.core.constants import NOTIFICATION_TYPE_MIGRATION_FINALIZED
from app.core.embedding_models import get_speaker_index
from app.core.embedding_models import get_speaker_index_v4
from app.core.queues import UtilityPriority
from app.db.session_utils import session_scope
from app.services.embedding_mode_service import EmbeddingModeService
from app.services.migration_progress_service import migration_progress
from app.services.privacy.redact_view import PrivacyView
from app.tasks.embedding_migration_v4_scope import _off_scope_exclusion
from app.tasks.embedding_migration_v4_scope import _scopes_off_by_policy
from app.tasks.embedding_migration_v4_scope import _take_skipped_by_policy
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Finalize task (safe version)
# ---------------------------------------------------------------------------

_PROFILE_DOC_QUERY = {"query": {"term": {"document_type": "profile"}}}


def migrate_profile_documents_to_v4(client, v4_index: str) -> int:
    """Backfill v4 profile-voiceprint documents from already-migrated v4 speakers.

    Defect 1 of issue #657: the streaming per-file writer (
    ``_embedding_result_writer``) only ever produced speaker documents, never
    ``document_type: "profile"`` docs. Profile voiceprints are consolidated
    centroids that live only in the ``speakers`` alias's dual-write path
    (``store_profile_embedding``/``store_profile_embedding_v4``), so any
    profile not touched by a file transcribed *during* the migration window
    would have no v4 counterpart at all, and ``swap_speaker_alias`` would
    silently strand its voiceprint in the (soon inactive) v3 index.

    This recomputes every profile's centroid directly from the v4 speaker
    documents that already carry a ``profile_uuid`` — the only source of a
    256-dim vector for that profile, since a v3 512-dim profile embedding
    cannot be reused at a different dimension without re-running the model.
    A profile with zero migrated v4 speakers is therefore still not
    recoverable here; that is a hard requirement of the dimension change, not
    an oversight, and is why this is called *before* the finalize profile-
    count guard, which will then correctly refuse the swap if any profile
    still cannot be accounted for.

    Args:
        client: OpenSearch client.
        v4_index: Concrete v4 index to read speakers from / write profiles to.

    Returns:
        Number of profile documents written.
    """
    import numpy as np

    profiles: dict[str, dict] = {}

    try:
        response = client.search(
            index=v4_index,
            body={
                "size": 500,
                "query": {
                    "bool": {
                        "must": [{"exists": {"field": "profile_uuid"}}],
                        "must_not": [{"exists": {"field": "document_type"}}],
                    }
                },
                "_source": [
                    "profile_id",
                    "profile_uuid",
                    "user_id",
                    "organization_id",
                    "segment_count",
                    "embedding",
                ],
            },
            scroll="2m",
        )
        scroll_id = response.get("_scroll_id")
        hits = response["hits"]["hits"]
        # Hard bound on scroll pages: a malformed/unexpected response shape
        # (e.g. missing "_scroll_id") must not spin forever re-scrolling.
        max_pages = 10_000
        pages = 0
        while hits and pages < max_pages:
            pages += 1
            for hit in hits:
                source = hit["_source"]
                profile_uuid = source.get("profile_uuid")
                if not profile_uuid:
                    continue
                weight = max(1, int(source.get("segment_count") or 1))
                entry = profiles.setdefault(
                    profile_uuid,
                    {
                        "profile_id": source.get("profile_id"),
                        "user_id": source.get("user_id"),
                        "organization_id": source.get("organization_id"),
                        "vectors": [],
                        "weights": [],
                        "speaker_count": 0,
                    },
                )
                entry["vectors"].append(source["embedding"])
                entry["weights"].append(weight)
                entry["speaker_count"] += 1
            if not scroll_id:
                break
            response = client.scroll(scroll_id=scroll_id, scroll="2m")
            scroll_id = response.get("_scroll_id")
            hits = response["hits"]["hits"]
    except Exception as e:
        logger.error(f"Failed to scroll v4 speaker docs for profile backfill: {e}")
        return 0

    if not profiles:
        return 0

    from app.models.speaker import SpeakerProfile
    from app.services.opensearch_service import store_profile_embedding_v4

    profile_ids = [e["profile_id"] for e in profiles.values() if e.get("profile_id")]
    profile_names: dict[int, str] = {}
    if profile_ids:
        with session_scope() as db:
            rows = (
                db.query(SpeakerProfile.id, SpeakerProfile.name)
                .filter(SpeakerProfile.id.in_(profile_ids))
                .all()
            )
            profile_names = {row.id: row.name for row in rows}

    # Privacy policy (#1280): a profile voiceprint is not written for a scope that is off.
    with session_scope() as db:
        profile_view = PrivacyView(db)
        profiles_allowed = {
            profile_uuid: (
                entry.get("user_id") is not None
                and profile_view.voiceprints_for_scope(
                    int(entry["user_id"]), entry.get("organization_id")
                ).enabled
            )
            for profile_uuid, entry in profiles.items()
        }

    written = 0
    for profile_uuid, entry in profiles.items():
        if not profiles_allowed[profile_uuid]:
            logger.info("Profile backfill: %s skipped by privacy policy", profile_uuid)
            continue
        try:
            vectors = np.array(entry["vectors"], dtype=float)
            weights = np.array(entry["weights"], dtype=float)
            weighted = np.average(vectors, axis=0, weights=weights)
            norm = np.linalg.norm(weighted)
            if norm > 0:
                weighted = weighted / norm

            ok = store_profile_embedding_v4(
                profile_id=entry["profile_id"],
                profile_uuid=profile_uuid,
                profile_name=profile_names.get(entry["profile_id"], ""),
                embedding=weighted.tolist(),
                speaker_count=entry["speaker_count"],
                user_id=entry["user_id"],
                organization_id=entry.get("organization_id"),
            )
            if ok:
                written += 1
        except Exception as e:
            logger.error(f"Failed to backfill v4 profile {profile_uuid}: {e}")

    logger.info(f"Profile backfill: wrote {written}/{len(profiles)} v4 profile documents")
    return written


def _count_profile_docs(
    client, index: str, *, exclude: list[dict[str, Any]] | None = None
) -> int | None:
    """Count `document_type: "profile"` docs in an index.

    Fail-closed: returns ``None`` (never ``0``) whenever the count cannot be
    trusted — the index is missing, or the count query itself errors. Callers
    MUST treat ``None`` as "refuse", never as "no profiles to worry about".
    Profile documents (voiceprints) are OpenSearch's ONLY copy — Postgres
    stores no embedding vectors (see `app/models/media.py`'s note on
    `Speaker` / `SpeakerProfile`) — so an unresolvable count is not a reason
    to proceed, it is the reason to stop.
    """
    try:
        if not client.indices.exists(index=index):
            return None
        if exclude:
            body: dict[str, Any] = {
                "query": {
                    "bool": {
                        "filter": [_PROFILE_DOC_QUERY["query"]],
                        "must_not": exclude,
                    }
                }
            }
        else:
            body = _PROFILE_DOC_QUERY
        return int(client.count(index=index, body=body)["count"])
    except Exception as e:
        logger.error(f"Profile-doc count failed for index '{index}': {e}")
        return None


@celery_app.task(bind=True, name="finalize_v4_migration", priority=UtilityPriority.BACKGROUND)
def finalize_v4_migration_task(self, user_id: int = 1):
    """Finalize the v4 migration by swapping indices.

    Safety checks:
    - Count validation: reject if v4 has <50% of v3 docs
    - Profile-voiceprint guard: reject on ANY shortfall of `document_type:
      "profile"` docs between v3 and v4, or if either count is unresolvable.
      OpenSearch is the only copy of these embeddings (issue #657) and the
      total-doc-count ratio above cannot detect a profile-only loss, so this
      is a separate, fail-closed gate alongside it — not a replacement.
    - Flush v4 before swap to ensure all docs are persisted
    - Post-swap verification: reject if new main has <95% of v4 count
    - finally block ALWAYS clears mode cache + releases migration lock
    """
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if not client:
        return {"status": "error", "message": "OpenSearch not available"}

    v4_index = get_speaker_index_v4()
    main_index = get_speaker_index()

    try:
        # ---- Pre-flight checks ----
        if not client.indices.exists(index=v4_index):
            return {"status": "error", "message": "V4 index does not exist"}

        # Flush v4 to ensure all docs are persisted
        client.indices.flush(index=v4_index, wait_if_ongoing=True)

        v4_count = client.count(index=v4_index)["count"]
        if v4_count == 0:
            return {"status": "error", "message": "V4 index is empty"}

        # Get v3 count for validation (resolve alias if needed)
        from app.services.opensearch_service import get_active_versioned_index
        from app.services.opensearch_service import swap_speaker_alias

        v3_index = get_active_versioned_index()  # e.g. "speakers_v3" or "speakers"
        import contextlib

        # Privacy policy (#1280): a scope with voiceprints off was skipped by the migration
        # (D5), so the v3 documents of those scopes are not "missing" from v4. They are left
        # out of both reference counts below; every OTHER scope is still held to the same
        # guards, so a real loss is still caught. A policy that cannot be read aborts.
        try:
            off_scopes = _scopes_off_by_policy()
        except Exception as e:
            logger.error(f"Cannot read the privacy policy for finalize: {e}")
            return {
                "status": "error",
                "message": "Cannot read the privacy policy; refusing to swap the index.",
            }
        off_exclusion = _off_scope_exclusion(off_scopes)

        v3_count = 0
        with contextlib.suppress(Exception):
            if client.indices.exists(index=v3_index):
                if off_exclusion:
                    v3_count = client.count(
                        index=v3_index,
                        body={"query": {"bool": {"must_not": off_exclusion}}},
                    )["count"]
                else:
                    v3_count = client.count(index=v3_index)["count"]

        # Count validation: v4 should have at least 50% of v3 docs
        if v3_count > 0 and v4_count < v3_count * 0.5:
            return {
                "status": "error",
                "message": (
                    f"V4 index has only {v4_count} docs vs {v3_count} in v3 "
                    f"({v4_count / v3_count * 100:.0f}%). "
                    "Migration appears incomplete. Aborting finalize."
                ),
            }

        # ---- Backfill profile documents into v4 (defect 1 of issue #657) ----
        # The streaming per-file writer never produced profile docs; recompute
        # every profile's centroid from its already-migrated v4 speakers before
        # the guard below counts them. A profile with zero migrated speakers
        # cannot be recovered here (no v3->v4 dimension-preserving copy exists)
        # and will correctly fail the guard that follows.
        backfilled = migrate_profile_documents_to_v4(client, v4_index)
        if backfilled:
            logger.info(f"Finalize: backfilled {backfilled} v4 profile document(s)")

        # ---- Profile-voiceprint guard (fail-closed, issue #657) ----
        # The total-doc-count ratio above cannot catch a profile-only loss:
        # v3 legitimately holds more docs than v4 for unrelated reasons, so
        # the ratio absorbs a missing set of profile docs. Check separately.
        v3_profile_count = _count_profile_docs(client, v3_index, exclude=off_exclusion)
        v4_profile_count = _count_profile_docs(client, v4_index)

        if v3_profile_count is None or v4_profile_count is None:
            return {
                "status": "error",
                "message": (
                    "Cannot verify profile voiceprint counts "
                    f"(v3='{v3_index}': {v3_profile_count}, "
                    f"v4='{v4_index}': {v4_profile_count}). "
                    "Refusing to swap — OpenSearch is the only copy of these "
                    "embeddings and the count could not be confirmed safe."
                ),
            }

        if v3_profile_count > v4_profile_count:
            return {
                "status": "error",
                "message": (
                    f"V4 index has only {v4_profile_count} profile voiceprint "
                    f"doc(s) vs {v3_profile_count} in v3. Finalizing would "
                    "silently drop profile voiceprints (OpenSearch is their "
                    "only copy — Postgres stores no embedding vectors). "
                    "Aborting finalize; carry profile docs into v4 before "
                    "retrying."
                ),
            }

        # ---- Swap alias (atomic, no data copy) ----
        # The 'speakers' alias now points to speakers_v4 instead of speakers_v3.
        # speakers_v3 is preserved automatically as the backup — no separate
        # backup index needed since we never delete or move data.
        swap_result = swap_speaker_alias(v4_index)
        if swap_result.get("status") != "success":
            return {
                "status": "error",
                "message": f"Alias swap failed: {swap_result}",
            }

        logger.info(
            f"V4 migration finalized: alias '{main_index}' → '{v4_index}' "
            f"({v4_count} docs). V3 data preserved in '{v3_index}'."
        )
        result = {
            "status": "success",
            "message": "Migration complete",
            "v4_documents": v4_count,
            "v3_preserved_in": v3_index,
            # Privacy policy (#1280): scopes with voiceprints off were skipped, so the index
            # now live holds none for them. Their v3 documents are preserved in the backup
            # index and are not restored into the alias.
            "skipped_by_policy": {
                "scopes": len(off_scopes),
                "files": _take_skipped_by_policy(),
            },
        }
        send_ws_event(user_id, NOTIFICATION_TYPE_MIGRATION_FINALIZED, result)
        return result

    except Exception as e:
        logger.error(f"Error finalizing migration: {e}")
        result = {"status": "error", "message": str(e)}
        send_ws_event(user_id, NOTIFICATION_TYPE_MIGRATION_FINALIZED, result)
        return result

    finally:
        # ALWAYS clear caches and release the migration lock, even on error.
        # deactivate() is safe to call even if activate() was never reached
        # for this run (e.g. a manual /force-complete with no orchestrator
        # run in between) - it floors the ref count at 0 rather than going
        # negative.
        from app.services.migration_lock_service import migration_lock

        EmbeddingModeService.clear_cache()
        migration_progress.clear_status()
        migration_lock.deactivate()
