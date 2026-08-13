"""Speaker-embedding extraction, matching, and v4 staging writes.

Two paths exist: native PyAnnote 256-dim centroids (no extra model load)
and the traditional ``SpeakerEmbeddingService`` 512-dim path.
"""

import logging
import os
import time
from typing import Any

from app.core.constants import get_speaker_index_v4
from app.db.session_utils import session_scope
from app.services.speaker_matching_service import SpeakerMatchingService
from app.utils.task_utils import update_task_status

from .context import TranscriptionContext

logger = logging.getLogger(__name__)


def _process_speaker_embeddings(
    ctx: TranscriptionContext, audio_file_path: str, processed_segments: list, speaker_mapping: dict
) -> None:
    """Extract speaker embeddings and match profiles using warm cached model."""
    import time

    from app.services.speaker_embedding_service import get_cached_embedding_service
    from app.utils.hardware_detection import detect_hardware

    total_start = time.perf_counter()

    # Force GPU synchronization before embedding extraction
    sync_start = time.perf_counter()
    hardware_config = detect_hardware()
    hardware_config.optimize_memory_usage()
    logger.info(f"TIMING: GPU sync completed in {time.perf_counter() - sync_start:.3f}s")

    # Use cached embedding service (warm model, avoids 40-60s cold start)
    cache_start = time.perf_counter()
    embedding_service = get_cached_embedding_service()
    cache_elapsed = time.perf_counter() - cache_start
    logger.info(
        f"TIMING: get_cached_embedding_service completed in {cache_elapsed:.3f}s - "
        f"mode: {embedding_service.mode} ({embedding_service.model_name})"
    )

    # Audio slicing + model inference, deliberately OUTSIDE any session. When
    # this ran inside ``session_scope`` the whole extraction (up to 5 ffmpeg
    # slices per speaker plus inference on each) sat inside one Postgres
    # transaction, holding ACCESS SHARE and pinning the vacuum horizon for its
    # entire duration.
    extract_start = time.perf_counter()
    raw_embeddings = embedding_service.extract_embeddings_for_segments(
        audio_file_path, processed_segments, speaker_mapping
    )
    # ``aggregate_embeddings`` mean-pools + L2-normalizes, exactly what
    # ``process_speaker_segments`` did internally before matching.
    aggregated_embeddings = {
        speaker_id: embedding_service.aggregate_embeddings(embeddings)
        for speaker_id, embeddings in raw_embeddings.items()
        if embeddings
    }
    logger.info(
        f"TIMING: extract_embeddings_for_segments completed in "
        f"{time.perf_counter() - extract_start:.3f}s (no DB session held)"
    )

    matching_start = time.perf_counter()
    with session_scope() as db:
        # Compute accessible profiles for cross-user matching via shared collections
        from app.services.permission_service import PermissionService

        accessible_ids = PermissionService.get_accessible_profile_ids(db, ctx.user_id)

        # ``embedding_service=None``: the embeddings arrive already aggregated
        # and normalized, which is the only thing the matching service used it
        # for on this path.
        matching_service = SpeakerMatchingService(db, embedding_service=None)
        logger.info(f"Starting speaker matching for {len(speaker_mapping)} speakers")
        speaker_results = matching_service.process_speaker_embeddings_native(
            media_file_id=ctx.file_id,
            user_id=ctx.user_id,
            native_embeddings=aggregated_embeddings,
            accessible_profile_ids=accessible_ids,
        )
        matching_elapsed = time.perf_counter() - matching_start
        logger.info(
            f"TIMING: speaker profile matching completed in {matching_elapsed:.3f}s - "
            f"got {len(speaker_results) if speaker_results else 0} results"
        )
        update_task_status(db, ctx.task_id, "in_progress", progress=0.82)

    total_elapsed = time.perf_counter() - total_start
    logger.info(
        f"TIMING: _process_speaker_embeddings TOTAL completed in {total_elapsed:.3f}s - "
        f"{len(speaker_results) if speaker_results else 0} speakers processed"
    )


def _process_speaker_embeddings_native(
    ctx: TranscriptionContext,
    native_embeddings: dict,
    processed_segments: list,
    speaker_mapping: dict,
) -> None:
    """Process speaker embeddings using pre-computed PyAnnote centroids (native path).

    Uses 256-dim WeSpeaker centroids from diarization instead of loading a separate
    embedding model. Saves 5-80s GPU time and ~500MB VRAM per file.

    Args:
        ctx: Transcription context.
        native_embeddings: Dict mapping speaker labels (e.g. "SPEAKER_00") to
            L2-normalized centroid vectors from PyAnnote.
        processed_segments: Processed transcript segments.
        speaker_mapping: Mapping of speaker labels to database IDs.
    """
    import time

    import numpy as np

    total_start = time.perf_counter()

    # Map speaker labels -> DB IDs using speaker_mapping
    db_embeddings: dict[int, np.ndarray] = {}
    for label, embedding in native_embeddings.items():
        db_id = speaker_mapping.get(label)
        if db_id is not None:
            db_embeddings[db_id] = embedding
        else:
            logger.debug(f"No DB mapping for speaker label '{label}', skipping embedding")

    if not db_embeddings:
        logger.warning("No speaker embeddings could be mapped to DB IDs")
        return

    matching_start = time.perf_counter()
    with session_scope() as db:
        # Compute accessible profiles for cross-user matching via shared collections
        from app.services.permission_service import PermissionService

        accessible_ids = PermissionService.get_accessible_profile_ids(db, ctx.user_id)

        matching_service = SpeakerMatchingService(db, embedding_service=None)
        logger.info(
            f"Starting native speaker matching for {len(db_embeddings)} speakers "
            f"(dim={next(iter(db_embeddings.values())).shape[0]})"
        )
        speaker_results = matching_service.process_speaker_embeddings_native(
            media_file_id=ctx.file_id,
            user_id=ctx.user_id,
            native_embeddings=db_embeddings,
            accessible_profile_ids=accessible_ids,
        )
        matching_elapsed = time.perf_counter() - matching_start
        logger.info(
            f"TIMING: process_speaker_embeddings_native completed in {matching_elapsed:.3f}s - "
            f"got {len(speaker_results) if speaker_results else 0} results"
        )
        update_task_status(db, ctx.task_id, "in_progress", progress=0.82)

    total_elapsed = time.perf_counter() - total_start
    logger.info(
        f"TIMING: _process_speaker_embeddings_native TOTAL completed in {total_elapsed:.3f}s - "
        f"{len(speaker_results) if speaker_results else 0} speakers processed"
    )


def _load_v4_profile_batch(
    touched_profile_ids: set[int],
    native_embeddings: dict,
    speaker_mapping: dict[str, int],
) -> list[dict]:
    """Phase 1 — read every profile's identity + current-file embeddings.

    One short session for the whole batch, returning **plain data**: the
    OpenSearch phase that follows must run with no transaction open, and an
    escaping ``SpeakerProfile`` instance would lazy-load and silently reopen one.
    """
    import numpy as np

    from app.models.media import Speaker
    from app.models.media import SpeakerProfile

    batch: list[dict] = []
    with session_scope() as db:
        for profile_id in touched_profile_ids:
            profile = db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()
            if not profile:
                logger.warning(f"v4 staging: Profile {profile_id} not found")
                continue

            # Embeddings from current file's speakers assigned to this profile
            current: list = []
            speaker_ids = {
                int(row[0])
                for row in db.query(Speaker.id).filter(Speaker.profile_id == profile_id).all()
            }
            for label, db_id in speaker_mapping.items():
                if db_id in speaker_ids and label in native_embeddings:
                    emb = native_embeddings[label]
                    current.append(np.array(emb) if not isinstance(emb, np.ndarray) else emb)

            batch.append(
                {
                    "profile_id": int(profile.id),
                    "profile_uuid": str(profile.uuid),
                    "profile_name": str(profile.name),
                    "user_id": int(profile.user_id),
                    "organization_id": profile.organization_id,
                    "current_file_embeddings": current,
                }
            )
    return batch


def _fetch_existing_v4_embeddings(profile_id: int, current_file_speaker_uuids: set[str]) -> list:
    """Existing v4 speaker docs for this profile from other files (OpenSearch).

    Called with **no DB session open** — it is an OpenSearch search per profile.
    """
    import numpy as np

    found: list = []
    try:
        from app.services.opensearch_service import get_opensearch_client

        client = get_opensearch_client()
        if not client:
            raise RuntimeError("OpenSearch client unavailable")
        v4_index = get_speaker_index_v4()
        resp = client.search(
            index=v4_index,
            body={
                "query": {
                    "bool": {
                        "must": [{"term": {"profile_id": profile_id}}],
                        "must_not": [{"term": {"document_type": "profile"}}],
                    }
                },
                "size": 500,
                "_source": ["embedding"],
            },
        )
        for hit in resp.get("hits", {}).get("hits", []):
            existing_emb = hit["_source"].get("embedding")
            if existing_emb and hit["_id"] not in current_file_speaker_uuids:
                found.append(np.array(existing_emb))
    except Exception as e:
        logger.debug(f"v4 staging: Could not fetch existing v4 docs for profile {profile_id}: {e}")

    return found


def _update_v4_profile_embeddings(
    touched_profile_ids: set[int],
    native_embeddings: dict,
    speaker_mapping: dict[str, int],
    current_file_speaker_uuids: set[str],
) -> int:
    """Update consolidated profile embeddings in v4 for touched profiles.

    Two phases: one short read session for the whole batch, then the OpenSearch
    work — a search **and** a ``store_profile_embedding_v4`` write per profile —
    with **no DB session held**. Previously one session wrapped the loop, so N
    profiles meant 2N OpenSearch round trips inside a single open transaction.

    Returns:
        Number of profiles successfully updated.
    """
    import numpy as np

    from app.services.opensearch_service import store_profile_embedding_v4

    # Phase 1 — read (short session, Postgres only).
    batch = _load_v4_profile_batch(touched_profile_ids, native_embeddings, speaker_mapping)

    # Phase 2 — OpenSearch. NO DB session is held here.
    update_count = 0
    for entry in batch:
        profile_id = entry["profile_id"]
        try:
            v4_embeddings = list(entry["current_file_embeddings"])
            v4_embeddings.extend(
                _fetch_existing_v4_embeddings(profile_id, current_file_speaker_uuids)
            )
            if not v4_embeddings:
                logger.debug(f"v4 staging: No v4 embeddings for profile {profile_id}")
                continue

            # Average and L2-normalize for consistent cosine similarity
            avg_vec = np.mean(v4_embeddings, axis=0)
            norm = np.linalg.norm(avg_vec)
            if norm < 1e-8:
                logger.warning(f"v4 staging: Zero-norm profile embedding for {profile_id}")
                continue
            avg_embedding = (avg_vec / norm).tolist()
            store_profile_embedding_v4(
                profile_id=profile_id,
                profile_uuid=entry["profile_uuid"],
                profile_name=entry["profile_name"],
                embedding=avg_embedding,
                speaker_count=len(v4_embeddings),
                user_id=entry["user_id"],
                organization_id=entry["organization_id"],
            )
            update_count += 1

        except Exception as e:
            logger.warning(f"v4 staging: Error updating profile {profile_id}: {e}")

    return update_count


def _store_native_centroids_in_v4_staging(
    ctx: TranscriptionContext,
    native_embeddings: dict,
    speaker_mapping: dict[str, int],
) -> None:
    """Store 256-dim native centroids in speakers_v4 staging index.

    Phase 1: Store per-speaker centroids (inheriting labels from DB).
    Phase 2: Update consolidated profile embeddings in v4 for any
             profiles touched by this file's speakers.

    Fire-and-forget: failures logged but don't affect main pipeline.
    """
    from app.models.media import Speaker
    from app.services.opensearch_service import bulk_add_speaker_embeddings_v4
    from app.services.opensearch_service import ensure_v4_index_exists

    if not ensure_v4_index_exists():
        logger.warning("v4 staging: Could not create/verify speakers_v4 index, skipping")
        return

    touched_profile_ids: set[int] = set()
    current_file_speaker_uuids: set[str] = set()
    bulk_payload: list[dict[str, Any]] = []

    # Phase 1: assemble per-speaker centroid payload. All entries are then
    # shipped to OpenSearch in ONE ``_bulk`` request — turns an N-round-trip
    # loop into a single round-trip (Phase 2 PR #9, item D14). A 10-speaker
    # meeting saves ~200-500 ms of OpenSearch latency.
    with session_scope() as db:
        # Batch-fetch all speakers for this file (avoids N+1 per-label queries)
        from sqlalchemy.orm import joinedload

        needed_ids = [v for v in speaker_mapping.values() if v is not None]
        speakers_batch = (
            db.query(Speaker)
            .options(joinedload(Speaker.profile))
            .filter(Speaker.id.in_(needed_ids))
            .all()
            if needed_ids
            else []
        )
        speaker_by_id = {int(s.id): s for s in speakers_batch}

        for label, embedding in native_embeddings.items():
            db_id = speaker_mapping.get(label)
            if db_id is None:
                continue

            speaker = speaker_by_id.get(db_id)
            if not speaker:
                logger.warning(f"v4 staging: Speaker ID {db_id} not found in DB")
                continue

            emb_list = embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
            speaker_uuid = str(speaker.uuid)
            current_file_speaker_uuids.add(speaker_uuid)

            profile_uuid: str | None = None
            if speaker.profile_id and speaker.profile:
                profile_uuid = str(speaker.profile.uuid)
                touched_profile_ids.add(speaker.profile_id)

            bulk_payload.append(
                {
                    "speaker_id": int(speaker.id),
                    "speaker_uuid": speaker_uuid,
                    "user_id": ctx.user_id,
                    "organization_id": ctx.organization_id,
                    "name": speaker.display_name or speaker.name,
                    "embedding": emb_list,
                    "profile_id": speaker.profile_id,
                    "profile_uuid": profile_uuid,
                    "media_file_id": ctx.file_id,
                    "segment_count": 1,
                    "display_name": speaker.display_name,
                }
            )

    stored_count = 0
    if bulk_payload:
        try:
            response = bulk_add_speaker_embeddings_v4(bulk_payload)
            if response is not None and not response.get("errors"):
                stored_count = len(bulk_payload)
            elif response is not None:
                # Bulk partially succeeded — count items without top-level errors.
                stored_count = sum(
                    1
                    for item in response.get("items", [])
                    if not item.get("index", {}).get("error")
                )
        except Exception as e:
            logger.warning(f"v4 staging: bulk embedding upsert failed: {e}")

    # Phase 2: Update consolidated profile embeddings in v4
    profile_update_count = 0
    if touched_profile_ids:
        profile_update_count = _update_v4_profile_embeddings(
            touched_profile_ids,
            native_embeddings,
            speaker_mapping,
            current_file_speaker_uuids,
        )

    logger.info(
        f"v4 staging: stored {stored_count} speakers + "
        f"{profile_update_count} profile updates (256-dim) "
        f"for file {ctx.file_id}"
    )


def _should_use_native_embeddings(result: dict) -> bool:
    """Determine whether to use native PyAnnote centroids or traditional embedding model.

    Decision logic:
    1. Check USE_NATIVE_SPEAKER_EMBEDDINGS env var (default true)
    2. Check native_speaker_embeddings exist in result
    3. Auto-detect index dimension compatibility:
       - Fresh install (no index): use native (creates 256-dim index)
       - v4 index (256-dim): compatible with native centroids
       - v3 index (512-dim): incompatible, fall back to traditional

    Returns:
        True if native embeddings should be used.
    """
    use_native = os.getenv("USE_NATIVE_SPEAKER_EMBEDDINGS", "true").lower() == "true"
    if not use_native:
        logger.info("Native speaker embeddings disabled by USE_NATIVE_SPEAKER_EMBEDDINGS=false")
        return False

    native_embeddings = result.get("native_speaker_embeddings", {})
    if not native_embeddings:
        logger.info("No native speaker embeddings in result, falling back to traditional path")
        return False

    # Auto-detect index dimension compatibility
    try:
        from app.services.embedding_mode_service import EmbeddingModeService

        index_dim = EmbeddingModeService.get_embedding_dimension()

        # Get centroid dimension from first embedding
        first_emb = next(iter(native_embeddings.values()))
        centroid_dim = first_emb.shape[-1] if hasattr(first_emb, "shape") else len(first_emb)

        if index_dim == centroid_dim:
            logger.info(
                f"Using native speaker embeddings: index dim ({index_dim}) matches "
                f"centroid dim ({centroid_dim})"
            )
            return True

        # Check if this is a fresh install (v4 mode = 256-dim, matching centroids)
        mode = EmbeddingModeService.detect_mode()
        if mode == "v4" and centroid_dim == 256:
            logger.info(
                "Using native speaker embeddings: v4 mode detected, "
                f"centroid dim={centroid_dim} compatible"
            )
            return True

        logger.warning(
            f"Index dimension ({index_dim}) does not match centroid dimension "
            f"({centroid_dim}). Falling back to traditional SpeakerEmbeddingService "
            f"for backward compatibility with existing v3 embeddings."
        )
        return False

    except Exception as e:
        logger.warning(
            f"Error checking embedding dimension compatibility: {e}. "
            "Falling back to traditional path."
        )
        return False


def _run_speaker_embeddings_with_retry(
    ctx: TranscriptionContext,
    result: dict,
    audio_file_path: str,
    processed_segments: list,
    speaker_mapping: dict,
) -> None:
    """Run speaker embedding processing with up to 2 retries on failure."""
    use_native = _should_use_native_embeddings(result)
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            if use_native:
                logger.info(
                    "Using native speaker embeddings (PyAnnote centroids, no separate model)"
                )
                _process_speaker_embeddings_native(
                    ctx,
                    result["native_speaker_embeddings"],
                    processed_segments,
                    speaker_mapping,
                )
            else:
                logger.info("Using traditional SpeakerEmbeddingService for speaker embeddings")
                _process_speaker_embeddings(
                    ctx, audio_file_path, processed_segments, speaker_mapping
                )
            break  # Success
        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"Speaker embedding attempt {attempt + 1} failed: {e}, retrying...")
                time.sleep(1)
            else:
                logger.error(f"Speaker embedding failed after {max_retries + 1} attempts: {e}")
