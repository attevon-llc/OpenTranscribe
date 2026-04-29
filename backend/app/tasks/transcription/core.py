import contextlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import CeleryQueues
from app.core.constants import GPUPriority
from app.core.constants import get_speaker_index_v4
from app.db.session_utils import get_refreshed_object
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services.minio_service import download_file
from app.services.opensearch_service import index_transcript
from app.services.speaker_matching_service import SpeakerMatchingService
from app.transcription.config import LIGHTWEIGHT_MODELS
from app.utils import benchmark_timing
from app.utils.error_classification import categorize_error
from app.utils.task_utils import create_task_record
from app.utils.task_utils import update_media_file_status
from app.utils.task_utils import update_task_status

from .audio_processor import get_audio_file_extension
from .audio_processor import prepare_audio_for_transcription
from .metadata_extractor import extract_media_metadata
from .metadata_extractor import update_media_file_metadata
from .notifications import send_completion_notification
from .notifications import send_error_notification
from .notifications import send_processing_notification
from .notifications import send_progress_notification
from .speaker_processor import create_speaker_mapping
from .speaker_processor import extract_unique_speakers
from .speaker_processor import mark_overlapping_segments
from .speaker_processor import process_segments_with_speakers
from .storage import generate_full_transcript
from .storage import get_unique_speaker_names
from .storage import save_transcript_segments
from .storage import update_media_file_transcription_status

logger = logging.getLogger(__name__)

_VALID_LOCAL_MODELS = frozenset(
    {
        "tiny",
        "tiny.en",
        "base",
        "base.en",
        "small",
        "small.en",
        "medium",
        "medium.en",
        "large-v1",
        "large-v2",
        "large-v3",
        "large-v3-turbo",
    }
)


class _ModelNotAvailableError(Exception):
    """Raised when a requested model is not available on this worker."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        super().__init__(f"Model '{model_name}' not available on this worker")


def _validate_model_override(model_name: str) -> str | None:
    """Validate a per-task model override.

    Returns:
        'cpu' if model should run on CPU, 'gpu' if it matches the admin model,
        or None if the model is rejected.
    """
    from app.transcription.config import TranscriptionConfig

    if model_name not in _VALID_LOCAL_MODELS:
        logger.warning("Unknown model '%s', rejecting override", model_name)
        return None

    if model_name in LIGHTWEIGHT_MODELS:
        return "cpu"

    if model_name == TranscriptionConfig._pinned_model_name:
        return "gpu"

    logger.warning(
        "Model '%s' is valid but not loaded (admin model is '%s'). "
        "Only lightweight models or the admin model are supported.",
        model_name,
        TranscriptionConfig._pinned_model_name,
    )
    return None


# Deprecation warning for legacy TRANSCRIPTION_ENGINE env var
_engine = os.getenv("TRANSCRIPTION_ENGINE", "")
if _engine and _engine.lower() != "native":
    logger.warning(
        f"TRANSCRIPTION_ENGINE={_engine} is deprecated and ignored. "
        "The unified pipeline is now used for all transcription."
    )

# Deprecation warning for legacy ENABLE_ALIGNMENT env var
_align = os.getenv("ENABLE_ALIGNMENT", "")
if _align:
    logger.warning(
        "ENABLE_ALIGNMENT is deprecated and ignored. "
        "Word-level timestamps are now provided natively by faster-whisper "
        "for all 100+ languages without a separate alignment model."
    )


def _get_user_language_settings(db, user_id: int) -> dict:
    """
    Retrieve user's language settings from the database.

    Args:
        db: Database session
        user_id: ID of the user

    Returns:
        Dict with source_language and translate_to_english keys
    """
    from app import models
    from app.core.constants import DEFAULT_SOURCE_LANGUAGE

    user_settings = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key.in_(
                [
                    "transcription_source_language",
                    "transcription_translate_to_english",
                ]
            ),
        )
        .all()
    )

    settings_map = {s.setting_key: s.setting_value for s in user_settings}

    return {
        "source_language": settings_map.get(
            "transcription_source_language", DEFAULT_SOURCE_LANGUAGE
        ),
        "translate_to_english": settings_map.get(
            "transcription_translate_to_english", "false"
        ).lower()
        == "true",
    }


def _get_user_transcription_settings(db, user_id: int) -> dict:
    """Retrieve user's transcription tuning settings from the database."""
    from app import models
    from app.core.constants import DEFAULT_HALLUCINATION_SILENCE_THRESHOLD
    from app.core.constants import DEFAULT_REPETITION_PENALTY
    from app.core.constants import DEFAULT_VAD_MIN_SILENCE_MS
    from app.core.constants import DEFAULT_VAD_MIN_SPEECH_MS
    from app.core.constants import DEFAULT_VAD_SPEECH_PAD_MS
    from app.core.constants import DEFAULT_VAD_THRESHOLD

    setting_keys = [
        "transcription_vad_threshold",
        "transcription_vad_min_silence_ms",
        "transcription_vad_min_speech_ms",
        "transcription_vad_speech_pad_ms",
        "transcription_hallucination_silence_threshold",
        "transcription_repetition_penalty",
        "transcription_min_speakers",
        "transcription_max_speakers",
        "transcription_diarization_source",
    ]
    user_settings = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key.in_(setting_keys),
        )
        .all()
    )
    settings_map = {s.setting_key: s.setting_value for s in user_settings}

    hal_raw = settings_map.get("transcription_hallucination_silence_threshold", "")
    hal_value = float(hal_raw) if hal_raw else DEFAULT_HALLUCINATION_SILENCE_THRESHOLD

    return {
        "vad_threshold": float(
            settings_map.get("transcription_vad_threshold", str(DEFAULT_VAD_THRESHOLD))
        ),
        "vad_min_silence_ms": int(
            settings_map.get("transcription_vad_min_silence_ms", str(DEFAULT_VAD_MIN_SILENCE_MS))
        ),
        "vad_min_speech_ms": int(
            settings_map.get("transcription_vad_min_speech_ms", str(DEFAULT_VAD_MIN_SPEECH_MS))
        ),
        "vad_speech_pad_ms": int(
            settings_map.get("transcription_vad_speech_pad_ms", str(DEFAULT_VAD_SPEECH_PAD_MS))
        ),
        "hallucination_silence_threshold": hal_value,
        "repetition_penalty": float(
            settings_map.get("transcription_repetition_penalty", str(DEFAULT_REPETITION_PENALTY))
        ),
        "min_speakers": int(
            settings_map.get("transcription_min_speakers", str(settings.MIN_SPEAKERS))
        ),
        "max_speakers": int(
            settings_map.get("transcription_max_speakers", str(settings.MAX_SPEAKERS))
        ),
        "diarization_source": settings_map.get("transcription_diarization_source", "provider"),
        "disable_diarization": settings_map.get("transcription_diarization_source", "provider")
        == "off",
    }


@dataclass
class TranscriptionContext:
    """Context holder for transcription task state."""

    task_id: str
    file_id: int
    file_uuid: str
    user_id: int
    file_path: str
    file_name: str
    content_type: str


def _get_media_file_context(file_uuid: str, task_id: str) -> TranscriptionContext | None:
    """Get media file and create transcription context."""
    from app.utils.uuid_helpers import get_file_by_uuid

    with session_scope() as db:
        media_file = get_file_by_uuid(db, file_uuid)
        if not media_file:
            logger.error(f"Media file with UUID {file_uuid} not found")
            return None

        ctx = TranscriptionContext(
            task_id=task_id,
            file_id=int(media_file.id),
            file_uuid=file_uuid,
            user_id=int(media_file.user_id),
            file_path=str(media_file.storage_path),
            file_name=str(media_file.filename),
            content_type=str(media_file.content_type),
        )
        update_media_file_status(db, ctx.file_id, FileStatus.PROCESSING)
        return ctx


def _handle_transcription_failure(
    ctx: TranscriptionContext, task_id: str, error_msg: str, error_type: str
) -> dict:
    """Handle transcription failure by updating status and sending notification."""
    with session_scope() as db:
        update_task_status(db, task_id, "failed", error_message=error_msg, completed=True)
        update_media_file_status(db, ctx.file_id, FileStatus.ERROR)
        media_file = get_refreshed_object(db, MediaFile, ctx.file_id)
        if media_file:
            media_file.last_error_message = error_msg
            media_file.error_category = categorize_error(error_msg).value
            db.commit()

    send_error_notification(ctx.user_id, ctx.file_id, error_msg)
    return {"status": "error", "message": error_msg, "error_type": error_type}


def _validate_transcription_result(
    result: dict, ctx: TranscriptionContext, task_id: str
) -> dict | None:
    """Validate transcription result has valid content. Returns error dict if invalid, None if valid."""
    if not result or not result.get("segments") or len(result["segments"]) == 0:
        error_msg = (
            "No audio content could be detected in this file. "
            "The file may be corrupted, contain only silence, or be in an unsupported format. "
            "Please check the file and try uploading again."
        )
        logger.warning(f"No valid audio content found in file {ctx.file_id}: {ctx.file_name}")
        return _handle_transcription_failure(ctx, task_id, error_msg, "no_valid_audio")

    # Check if segments contain actual transcribable content
    has_content = any(segment.get("text", "").strip() for segment in result["segments"])
    if not has_content:
        error_msg = (
            "No speech could be detected in this file. "
            "The file may contain only music, background noise, or silence. "
            "Please verify the file contains clear speech and try again."
        )
        logger.warning(f"No speech content found in file {ctx.file_id}: {ctx.file_name}")
        return _handle_transcription_failure(ctx, task_id, error_msg, "no_speech_content")

    return None


def _get_user_friendly_error_message(error_message: str) -> str:
    """Convert technical error to user-friendly message."""
    error_lower = error_message.lower()

    if "libcudnn" in error_lower:
        return (
            "Audio processing failed due to a system library compatibility issue. "
            "The transcription service requires updated dependencies. "
            "Please contact support for assistance."
        )
    if "cuda" in error_lower and "out of memory" in error_lower:
        return (
            "GPU out of memory error. The audio file may be too large for available GPU resources. "
            "Please try with a shorter audio file or contact support."
        )
    if "cuda" in error_lower or "gpu" in error_lower:
        return (
            "GPU processing error occurred during transcription. "
            "The system may need reconfiguration. "
            "Please try again or contact support if the issue persists."
        )
    if "model" in error_lower and ("download" in error_lower or "load" in error_lower):
        return (
            "Failed to download or load AI models. "
            "Please check your internet connection and try again. "
            "If the problem persists, contact support."
        )
    return error_message


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

    matching_start = time.perf_counter()
    with session_scope() as db:
        # Compute accessible profiles for cross-user matching via shared collections
        from app.services.permission_service import PermissionService

        accessible_ids = PermissionService.get_accessible_profile_ids(db, ctx.user_id)

        matching_service = SpeakerMatchingService(db, embedding_service)
        logger.info(f"Starting speaker matching for {len(speaker_mapping)} speakers")
        speaker_results = matching_service.process_speaker_segments(
            audio_file_path,
            ctx.file_id,
            ctx.user_id,
            processed_segments,
            speaker_mapping,
            accessible_profile_ids=accessible_ids,
        )
        matching_elapsed = time.perf_counter() - matching_start
        logger.info(
            f"TIMING: process_speaker_segments completed in {matching_elapsed:.3f}s - "
            f"got {len(speaker_results) if speaker_results else 0} results"
        )
        update_task_status(db, ctx.task_id, "in_progress", progress=0.82)

    total_elapsed = time.perf_counter() - total_start
    logger.info(
        f"TIMING: _process_speaker_embeddings TOTAL completed in {total_elapsed:.3f}s - "
        f"{len(speaker_results) if speaker_results else 0} speakers processed"
    )
    # Note: We do NOT cleanup the embedding service here - keep it warm for next transcription


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


def _collect_v4_profile_embeddings(
    profile_id: int,
    native_embeddings: dict,
    speaker_mapping: dict[str, int],
    current_file_speaker_uuids: set[str],
    db,
) -> list:
    """Collect 256-dim embeddings for a profile from current file and existing v4 docs.

    Args:
        profile_id: Profile to collect embeddings for.
        native_embeddings: Current file's native centroid dict.
        speaker_mapping: Speaker label -> DB ID mapping.
        current_file_speaker_uuids: UUIDs of speakers from the current file (to avoid double-counting).
        db: Active database session.

    Returns:
        List of numpy arrays (256-dim embeddings).
    """
    import numpy as np

    from app.models.media import Speaker

    v4_embeddings = []

    # Embeddings from current file's speakers assigned to this profile
    profile_speakers = db.query(Speaker).filter(Speaker.profile_id == profile_id).all()
    for ps in profile_speakers:
        for label, db_id in speaker_mapping.items():
            if db_id == ps.id and label in native_embeddings:
                emb = native_embeddings[label]
                v4_embeddings.append(np.array(emb) if not isinstance(emb, np.ndarray) else emb)
                break

    # Existing v4 speaker docs for this profile from other files
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
                v4_embeddings.append(np.array(existing_emb))
    except Exception as e:
        logger.debug(f"v4 staging: Could not fetch existing v4 docs for profile {profile_id}: {e}")

    return v4_embeddings


def _update_v4_profile_embeddings(
    touched_profile_ids: set[int],
    native_embeddings: dict,
    speaker_mapping: dict[str, int],
    current_file_speaker_uuids: set[str],
) -> int:
    """Update consolidated profile embeddings in v4 for touched profiles.

    Returns:
        Number of profiles successfully updated.
    """
    import numpy as np

    from app.models.media import SpeakerProfile
    from app.services.opensearch_service import store_profile_embedding_v4

    update_count = 0
    with session_scope() as db:
        for profile_id in touched_profile_ids:
            try:
                profile = db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()
                if not profile:
                    logger.warning(f"v4 staging: Profile {profile_id} not found")
                    continue

                v4_embeddings = _collect_v4_profile_embeddings(
                    profile_id,
                    native_embeddings,
                    speaker_mapping,
                    current_file_speaker_uuids,
                    db,
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
                    profile_uuid=str(profile.uuid),
                    profile_name=str(profile.name),
                    embedding=avg_embedding,
                    speaker_count=len(v4_embeddings),
                    user_id=int(profile.user_id),
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


def _index_transcript_in_search(ctx: TranscriptionContext, processed_segments: list) -> None:
    """Index transcript in OpenSearch with chunk-level embeddings and legacy whole-doc."""
    full_transcript = generate_full_transcript(processed_segments)
    speaker_names = get_unique_speaker_names(processed_segments)

    with session_scope() as db:
        media_file = get_refreshed_object(db, MediaFile, ctx.file_id)
        file_title = (
            (media_file.title or media_file.filename) if media_file else f"File {ctx.file_id}"
        )
        file_uuid = media_file.uuid if media_file else None

    if not file_uuid:
        logger.warning(f"Could not index transcript: file_uuid not found for file_id {ctx.file_id}")
        return

    # Legacy whole-doc index (backward compatibility)
    index_transcript(
        ctx.file_id, file_uuid, ctx.user_id, full_transcript, speaker_names, file_title
    )

    # Dispatch chunk-level search indexing as a separate tracked Celery task
    try:
        from app.tasks.search_indexing_task import index_transcript_search_task

        index_transcript_search_task.delay(
            file_id=ctx.file_id,
            file_uuid=str(file_uuid),
            user_id=ctx.user_id,
        )
        logger.info(f"Dispatched search indexing task for file {file_uuid}")
    except Exception as e:
        logger.warning(f"Failed to dispatch search indexing task for file {file_uuid}: {e}")


def _resolve_language_settings(
    ctx: TranscriptionContext,
    source_language: str | None,
    translate_to_english: bool | None,
) -> tuple[str, bool]:
    """Resolve language settings from explicit args or user DB settings."""
    if source_language is not None and translate_to_english is not None:
        return source_language, translate_to_english

    with session_scope() as db:
        user_lang_settings = _get_user_language_settings(db, ctx.user_id)
        resolved_lang = source_language or user_lang_settings["source_language"]
        resolved_translate = (
            translate_to_english
            if translate_to_english is not None
            else user_lang_settings["translate_to_english"]
        )

    return resolved_lang, resolved_translate


def _run_transcription_pipeline(
    ctx: TranscriptionContext,
    audio_file_path: str,
    min_speakers: int | None,
    max_speakers: int | None,
    num_speakers: int | None,
    source_language: str | None = None,
    translate_to_english: bool | None = None,
    disable_diarization: bool = False,
    whisper_model: str | None = None,
) -> dict:
    """Run the unified transcription pipeline."""
    from app.transcription import TranscriptionConfig
    from app.transcription import TranscriptionPipeline

    source_language, translate_to_english = _resolve_language_settings(
        ctx, source_language, translate_to_english
    )

    logger.info(
        f"Language settings for file {ctx.file_id}: "
        f"source_language={source_language}, translate_to_english={translate_to_english}"
    )

    # Get user's transcription tuning settings from DB
    with session_scope() as db:
        user_settings = _get_user_transcription_settings(db, ctx.user_id)

    # Build overrides dict — local model is admin-controlled via WHISPER_MODEL env var
    overrides: dict = dict(
        source_language=source_language,
        translate_to_english=translate_to_english,
        min_speakers=min_speakers if min_speakers is not None else user_settings["min_speakers"],
        max_speakers=max_speakers if max_speakers is not None else user_settings["max_speakers"],
        num_speakers=num_speakers if num_speakers is not None else settings.NUM_SPEAKERS,
        hf_token=settings.HUGGINGFACE_TOKEN,
        vad_threshold=user_settings["vad_threshold"],
        vad_min_silence_ms=user_settings["vad_min_silence_ms"],
        vad_min_speech_ms=user_settings["vad_min_speech_ms"],
        vad_speech_pad_ms=user_settings["vad_speech_pad_ms"],
        hallucination_silence_threshold=user_settings["hallucination_silence_threshold"],
        repetition_penalty=user_settings["repetition_penalty"],
        enable_diarization=not disable_diarization,
    )

    # Apply per-task model override if provided.
    # Lightweight models (base, tiny) are routed to CPU by dispatch.py and never
    # reach this GPU code path. Only the admin-pinned model is valid here.
    if whisper_model:
        if whisper_model in LIGHTWEIGHT_MODELS:
            logger.warning(
                "Lightweight model '%s' reached GPU task — should have been routed "
                "to CPU. Using admin-pinned model instead.",
                whisper_model,
            )
        elif whisper_model == TranscriptionConfig._pinned_model_name:
            # Explicitly requesting the admin model — no-op, already in use
            pass
        else:
            logger.warning(
                "Model override '%s' rejected — only the admin-pinned model ('%s') "
                "or lightweight models (routed to CPU) are supported.",
                whisper_model,
                TranscriptionConfig._pinned_model_name,
            )

    config = TranscriptionConfig.from_environment(**overrides)

    with session_scope() as db:
        update_task_status(db, ctx.task_id, "in_progress", progress=0.4)

    send_progress_notification(ctx.user_id, ctx.file_id, 0.4, "Running AI transcription")

    def progress_callback(progress, message):
        with session_scope() as db:
            update_task_status(db, ctx.task_id, "in_progress", progress=progress)
        send_progress_notification(ctx.user_id, ctx.file_id, progress, message)

    pipeline = TranscriptionPipeline(config)
    raw_result = pipeline.process(
        audio_file_path, progress_callback=progress_callback, task_id=ctx.task_id
    )
    # Annotate the raw WhisperX result with provider/model metadata so that
    # _process_transcription_result can persist it to media_file.asr_provider /
    # media_file.asr_model.  Without this the local pipeline leaves those columns NULL.
    if isinstance(raw_result, dict):
        raw_result.setdefault("asr_provider", "local")
        raw_result.setdefault(
            "asr_model",
            config.model_name if hasattr(config, "model_name") else settings.WHISPER_MODEL,
        )
    return raw_result


def _run_engine_pipeline(
    ctx: TranscriptionContext,
    local_wav_path: str,
    preprocess_context: dict,
) -> dict:
    """Engine split-stage fast path: skips MinIO download via shared-volume WAV.

    Uses Engine.run_gpu_stage() + Engine.run_cpu_finalize() so that transcription
    and diarization run in the GPU stage while speaker assignment runs in the CPU
    finalize stage — identical output shape to _run_transcription_pipeline().

    Args:
        ctx: Transcription task context (task_id, file_id, user_id, …).
        local_wav_path: Path to the pre-decoded WAV on the shared volume written
            by the preprocess task.  Caller must verify the file exists.
        preprocess_context: Raw dict forwarded from the preprocess task, used to
            extract per-task overrides (speakers, language, model, …).

    Returns:
        dict compatible with _process_and_save_critical() — same shape as
        _run_transcription_pipeline() with ``asr_provider`` and ``asr_model`` set.
    """
    from app.transcription import Engine
    from app.transcription import EngineConfig
    from app.transcription.engine.job import PreprocessResult

    min_speakers = preprocess_context.get("min_speakers")
    max_speakers = preprocess_context.get("max_speakers")
    num_speakers = preprocess_context.get("num_speakers")
    source_language = preprocess_context.get("source_language")
    translate_to_english = preprocess_context.get("translate_to_english")
    disable_diarization = preprocess_context.get("diarization_source") == "off"
    whisper_model = preprocess_context.get("whisper_model")

    source_language, translate_to_english = _resolve_language_settings(
        ctx, source_language, translate_to_english
    )

    logger.info(
        "Engine fast path: file=%d lang=%s translate=%s wav=%s",
        ctx.file_id,
        source_language,
        translate_to_english,
        local_wav_path,
    )

    with session_scope() as db:
        user_settings = _get_user_transcription_settings(db, ctx.user_id)

    overrides: dict = dict(
        source_language=source_language,
        translate_to_english=translate_to_english,
        min_speakers=min_speakers if min_speakers is not None else user_settings["min_speakers"],
        max_speakers=max_speakers if max_speakers is not None else user_settings["max_speakers"],
        num_speakers=num_speakers if num_speakers is not None else settings.NUM_SPEAKERS,
        hf_token=settings.HUGGINGFACE_TOKEN,
        vad_threshold=user_settings["vad_threshold"],
        vad_min_silence_ms=user_settings["vad_min_silence_ms"],
        vad_min_speech_ms=user_settings["vad_min_speech_ms"],
        vad_speech_pad_ms=user_settings["vad_speech_pad_ms"],
        hallucination_silence_threshold=user_settings["hallucination_silence_threshold"],
        repetition_penalty=user_settings["repetition_penalty"],
        enable_diarization=not disable_diarization,
    )

    # Honour admin-pinned model (same validation as _run_transcription_pipeline)
    from app.transcription import TranscriptionConfig as _TranscriptionConfig

    if whisper_model:
        if whisper_model in LIGHTWEIGHT_MODELS:
            logger.warning(
                "Lightweight model '%s' reached engine fast path — should have been "
                "routed to CPU. Using admin-pinned model instead.",
                whisper_model,
            )
        elif whisper_model != _TranscriptionConfig._pinned_model_name:
            logger.warning(
                "Model override '%s' rejected in engine fast path — only the "
                "admin-pinned model ('%s') is supported.",
                whisper_model,
                _TranscriptionConfig._pinned_model_name,
            )

    config = _TranscriptionConfig.from_environment(**overrides)
    engine_config = EngineConfig.from_environment(**overrides)

    engine = Engine(engine_config)

    pre = PreprocessResult(
        task_id=ctx.task_id,
        file_id=ctx.file_id,
        user_id=ctx.user_id,
        local_wav_path=local_wav_path,
        minio_temp_object="",
        audio_duration_s=0.0,
        audio_sample_rate=16000,
        audio_channels=1,
        audio_size_bytes=0,
        vad_regions=None,
        config_snapshot=engine_config.to_snapshot(),
    )

    with session_scope() as db:
        update_task_status(db, ctx.task_id, "in_progress", progress=0.40)

    send_progress_notification(ctx.user_id, ctx.file_id, 0.40, "Running AI transcription")

    def _progress(progress: float, message: str) -> None:
        with session_scope() as db:
            update_task_status(db, ctx.task_id, "in_progress", progress=progress)
        send_progress_notification(ctx.user_id, ctx.file_id, progress, message)

    raw = engine.run_gpu_stage(pre, progress_callback=_progress)
    job_result = engine.run_cpu_finalize(raw)

    raw_dict = job_result.to_pipeline_dict()
    raw_dict.setdefault("asr_provider", "local")
    raw_dict.setdefault("asr_model", config.model_name)
    return raw_dict


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


def _run_post_gpu_background(
    ctx: TranscriptionContext,
    result: dict,
    audio_file_path: str,
    processed_segments: list,
    speaker_mapping: dict,
    downstream_tasks: list[str] | None,
) -> None:
    """Run CPU-bound post-processing in a background thread.

    This includes speaker embeddings, search indexing, and downstream task
    dispatch. Runs off the GPU worker thread so the next GPU task can start
    immediately.

    All errors are caught and logged — failures here do not affect the
    transcription result (segments are already saved to DB).
    """
    import time

    bg_start = time.perf_counter()
    logger.info(f"Background post-processing started for file {ctx.file_id}")

    try:
        diarization_disabled = result.get("diarization_disabled", False)

        # Speaker embeddings (skip when diarization disabled — single speaker)
        if not diarization_disabled:
            # Choose path based on ASR provider
            is_cloud_asr = result.get("asr_provider") and result.get("asr_provider") != "local"

            if is_cloud_asr:
                # Cloud ASR: no native embeddings available, dispatch GPU task
                send_progress_notification(
                    ctx.user_id, ctx.file_id, 0.78, "Dispatching speaker embedding extraction"
                )
                try:
                    from app.tasks.speaker_embedding_task import extract_speaker_embeddings_task

                    extract_speaker_embeddings_task.apply_async(
                        args=[str(ctx.file_uuid), speaker_mapping],
                        queue=CeleryQueues.GPU,
                    )
                    logger.info(
                        f"Dispatched speaker embedding extraction to GPU queue for "
                        f"cloud-transcribed file {ctx.file_id}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to dispatch speaker embedding task: {e}")
            else:
                # Local ASR: embeddings available inline or via native centroids
                send_progress_notification(
                    ctx.user_id, ctx.file_id, 0.78, "Processing speaker identification"
                )
                _run_speaker_embeddings_with_retry(
                    ctx, result, audio_file_path, processed_segments, speaker_mapping
                )

                # Store native centroids in v4 staging index (fire-and-forget)
                native_embeddings_for_v4 = result.get("native_speaker_embeddings")
                if native_embeddings_for_v4 and not _should_use_native_embeddings(result):
                    try:
                        _store_native_centroids_in_v4_staging(
                            ctx, native_embeddings_for_v4, speaker_mapping
                        )
                    except Exception as e:
                        logger.warning(f"v4 staging: Error (non-fatal): {e}")
        else:
            logger.info(
                f"Skipping speaker embeddings for file {ctx.file_id} (diarization disabled)"
            )

        with session_scope() as db:
            update_task_status(db, ctx.task_id, "in_progress", progress=0.85)

        # Index in search (dispatches as separate Celery task)
        send_progress_notification(ctx.user_id, ctx.file_id, 0.85, "Dispatching search indexing")
        try:
            _index_transcript_in_search(ctx, processed_segments)
        except Exception as e:
            logger.warning(f"Error dispatching search indexing: {e}")

        # Finalize
        send_progress_notification(ctx.user_id, ctx.file_id, 0.95, "Finalizing transcription")
        with session_scope() as db:
            update_task_status(db, ctx.task_id, "completed", progress=1.0, completed=True)
            # Note: media_file.status is already set to COMPLETED by
            # update_media_file_transcription_status in the critical path

        send_completion_notification(ctx.user_id, ctx.file_id)

        logger.info(
            f"Transcription completed successfully for file {ctx.file_id}, "
            "triggering automatic summarization"
        )
        trigger_automatic_summarization(ctx.file_id, ctx.file_uuid, tasks_to_run=downstream_tasks)

        # Dispatch speaker attribute detection (fire-and-forget, CPU queue)
        speaker_llm_explicit = downstream_tasks is not None and "speaker_llm" in downstream_tasks
        if not speaker_llm_explicit:
            try:
                from app.tasks.speaker_attribute_task import _is_speaker_attribute_detection_enabled
                from app.tasks.speaker_attribute_task import detect_speaker_attributes_task

                if _is_speaker_attribute_detection_enabled(ctx.user_id):
                    detect_speaker_attributes_task.delay(str(ctx.file_uuid), ctx.user_id)
                    logger.info(f"Dispatched speaker attribute detection for {ctx.file_uuid}")
            except Exception as e:
                logger.warning(f"Failed to dispatch speaker attribute detection: {e}")

        # Speaker clustering
        if not downstream_tasks or "speaker_clustering" not in downstream_tasks:
            try:
                from app.tasks.speaker_clustering import cluster_speakers_for_file

                cluster_speakers_for_file.delay(str(ctx.file_uuid), ctx.user_id)
                logger.info(f"Dispatched speaker clustering for {ctx.file_uuid}")
            except Exception as e:
                logger.warning(f"Failed to dispatch speaker clustering: {e}")

    except Exception as e:
        logger.error(f"Background post-processing error for file {ctx.file_id}: {e}")
        # Try to mark task as failed if background processing crashes
        try:
            with session_scope() as db:
                update_task_status(
                    db,
                    ctx.task_id,
                    "failed",
                    error_message=f"Post-processing error: {e}",
                    completed=True,
                )
        except Exception as status_err:
            logger.warning(f"Failed to update task status after bg error: {status_err}")

    elapsed = time.perf_counter() - bg_start
    logger.info(
        f"TIMING: background post-processing completed in {elapsed:.3f}s for file {ctx.file_id}"
    )


def _process_transcription_result(
    ctx: TranscriptionContext,
    result: dict,
    audio_file_path: str,
    downstream_tasks: list[str] | None = None,
) -> dict:
    """Process successful transcription result including speakers, indexing, and finalization.

    Critical path (blocks GPU worker):
      - Extract speakers, create mappings, process segments
      - Save transcript segments to database
      - Release GPU memory

    Background thread (GPU worker freed for next task):
      - Speaker embedding matching (OpenSearch, CPU-bound)
      - Search indexing dispatch
      - Downstream task dispatch (summarization, clustering, etc.)
    """
    import threading
    import time

    from app.utils.hardware_detection import detect_hardware

    post_start = time.perf_counter()

    # --- Critical path: must complete before GPU worker returns ---

    # Resegment at speaker boundaries and merge adjacent same-speaker segments
    from app.utils.segment_postprocess import merge_consecutive_segments
    from app.utils.segment_postprocess import resegment_by_speaker

    send_progress_notification(ctx.user_id, ctx.file_id, 0.68, "Processing speaker segments")
    step_start = time.perf_counter()
    result["segments"] = merge_consecutive_segments(resegment_by_speaker(result["segments"]))
    logger.info(f"TIMING: resegment+merge completed in {time.perf_counter() - step_start:.3f}s")

    step_start = time.perf_counter()
    unique_speakers = extract_unique_speakers(result["segments"])
    logger.info(
        f"TIMING: extract_unique_speakers completed in {time.perf_counter() - step_start:.3f}s"
    )

    step_start = time.perf_counter()
    with session_scope() as db:
        speaker_mapping = create_speaker_mapping(db, ctx.user_id, ctx.file_id, unique_speakers)
        update_task_status(db, ctx.task_id, "in_progress", progress=0.72)
    logger.info(
        f"TIMING: create_speaker_mapping completed in {time.perf_counter() - step_start:.3f}s"
    )

    send_progress_notification(ctx.user_id, ctx.file_id, 0.72, "Organizing transcript segments")
    step_start = time.perf_counter()
    processed_segments = process_segments_with_speakers(result["segments"], speaker_mapping)
    logger.info(
        f"TIMING: process_segments_with_speakers completed in {time.perf_counter() - step_start:.3f}s - {len(processed_segments)} segments"
    )

    # Mark overlapping segments if overlap info is available and detection is enabled
    enable_overlap = os.getenv("ENABLE_OVERLAP_DETECTION", "true").lower() == "true"
    overlap_info = result.get("overlap_info", {})
    overlap_regions = overlap_info.get("regions", [])
    if enable_overlap and overlap_regions:
        step_start = time.perf_counter()
        logger.info(f"Marking {len(overlap_regions)} overlap regions for file {ctx.file_id}")
        processed_segments = mark_overlapping_segments(processed_segments, overlap_regions)
    elif not enable_overlap and overlap_regions:
        logger.info("Overlap marking disabled by ENABLE_OVERLAP_DETECTION=false")

    # Clean garbage words
    step_start = time.perf_counter()
    with session_scope() as db:
        from app.services import system_settings_service

        garbage_config = system_settings_service.get_garbage_cleanup_config(db)

    if garbage_config["garbage_cleanup_enabled"]:
        processed_segments, garbage_count = clean_garbage_words(
            processed_segments, garbage_config["max_word_length"]
        )
        if garbage_count > 0:
            logger.info(
                f"Cleaned {garbage_count} garbage word(s) from file {ctx.file_id} "
                f"(threshold: {garbage_config['max_word_length']} chars)"
            )
    logger.info(f"TIMING: garbage cleanup completed in {time.perf_counter() - step_start:.3f}s")

    with session_scope() as db:
        update_task_status(db, ctx.task_id, "in_progress", progress=0.75)

    # Save to database (must complete before returning)
    send_progress_notification(ctx.user_id, ctx.file_id, 0.75, "Saving transcript to database")
    step_start = time.perf_counter()
    whisper_model = os.getenv("WHISPER_MODEL", "large-v3-turbo")
    diarization_disabled = result.get("diarization_disabled", False)
    diarization_model = None if diarization_disabled else "pyannote/speaker-diarization-community-1"
    try:
        from app.services.embedding_mode_service import EmbeddingModeService

        embedding_mode = EmbeddingModeService.get_current_mode()
    except Exception:
        embedding_mode = None

    with session_scope() as db:
        save_transcript_segments(db, ctx.file_id, processed_segments)
        update_media_file_transcription_status(
            db,
            ctx.file_id,
            processed_segments,
            result.get("language", "en"),
            whisper_model=whisper_model,
            diarization_model=diarization_model,
            embedding_mode=embedding_mode,
            asr_provider=result.get("asr_provider"),
            asr_model=result.get("asr_model"),
            diarization_disabled=diarization_disabled,
        )
        update_task_status(db, ctx.task_id, "in_progress", progress=0.78)

    # Release GPU memory so next task can start loading models
    hardware_config = detect_hardware()
    hardware_config.optimize_memory_usage()

    critical_elapsed = time.perf_counter() - post_start
    logger.info(
        f"TIMING: critical post-processing completed in {critical_elapsed:.3f}s "
        f"for file {ctx.file_id} (GPU worker now free)"
    )

    # --- Background: speaker embeddings, indexing, dispatch ---
    # Run in a daemon thread so the GPU worker returns immediately.
    bg_thread = threading.Thread(
        target=_run_post_gpu_background,
        args=(
            ctx,
            result,
            audio_file_path,
            processed_segments,
            speaker_mapping,
            downstream_tasks,
        ),
        name=f"post-gpu-{ctx.file_id}",
        daemon=True,
    )
    bg_thread.start()
    logger.info(
        f"Started background post-processing thread for file {ctx.file_id}, GPU worker returning"
    )

    return {"status": "success", "file_id": ctx.file_id, "segments": len(processed_segments)}


def clean_garbage_words(segments: list, max_word_length: int = 50) -> tuple[list, int]:
    """
    Clean garbage words from transcript segments.

    Garbage words are very long continuous strings (no spaces) that typically result from
    WhisperX misinterpreting background noise (fans, static, rumbling) as speech.

    Args:
        segments: List of transcript segments with 'text' field
        max_word_length: Maximum word length threshold (words longer are replaced)

    Returns:
        Tuple of (cleaned segments, count of garbage words replaced)
    """
    garbage_count = 0
    cleaned_segments = []

    for segment in segments:
        text = segment.get("text", "")
        words = text.split()
        cleaned_words = []

        for word in words:
            # Check if word exceeds max length and has no spaces
            # (spaces would indicate it's not a single garbage word)
            if len(word) > max_word_length and " " not in word:
                cleaned_words.append("[background noise]")
                garbage_count += 1
                logger.debug(f"Replaced garbage word ({len(word)} chars): {word[:30]}...")
            else:
                cleaned_words.append(word)

        # Create a copy of the segment with cleaned text
        cleaned_segment = segment.copy()
        cleaned_segment["text"] = " ".join(cleaned_words)
        cleaned_segments.append(cleaned_segment)

    return cleaned_segments, garbage_count


def _dispatch_automatic_summary(
    file_id: int, file_uuid: str, collection_prompt_uuid: str | None
) -> None:
    """Check summary disable settings and dispatch if enabled."""
    from app.tasks.summarization import send_summary_notification
    from app.tasks.summarization import summarize_transcript_task
    from app.utils.summary_settings import get_summary_disable_reason

    with session_scope() as db:
        media_file = db.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
        if not media_file:
            logger.warning(f"File {file_uuid} not found for summary dispatch")
            return

        if str(media_file.summary_status) == "disabled":
            logger.info(f"Summary disabled for file {file_id} (per-file flag)")
            send_summary_notification(
                int(media_file.user_id),
                file_id,
                "disabled",
                "AI summary generation is disabled for this file",
                0,
            )
            return

        disable_reason = get_summary_disable_reason(db, int(media_file.user_id))
        if disable_reason:
            media_file.summary_status = "disabled"  # type: ignore[assignment]
            db.commit()
            reason_msg = (
                "AI summary generation has been disabled by the system administrator"
                if disable_reason == "system"
                else "AI summary auto-generation is disabled in your settings"
            )
            logger.info(f"Summary disabled for file {file_id} (reason: {disable_reason})")
            send_summary_notification(
                int(media_file.user_id),
                file_id,
                "disabled",
                reason_msg,
                0,
            )
            return

    summary_task = summarize_transcript_task.delay(
        file_uuid=file_uuid,
        prompt_uuid=collection_prompt_uuid,
    )
    logger.info(f"Automatic summarization task {summary_task.id} started for file {file_id}")


def _get_collection_prompt_uuid(file_id: int) -> str | None:
    """Look up the default summary prompt UUID from the file's first collection (by added_at)."""
    try:
        from app.db.session_utils import session_scope
        from app.models.media import Collection
        from app.models.media import CollectionMember
        from app.models.prompt import SummaryPrompt

        with session_scope() as db:
            result = (
                db.query(SummaryPrompt.uuid)
                .join(Collection, Collection.default_summary_prompt_id == SummaryPrompt.id)
                .join(CollectionMember, CollectionMember.collection_id == Collection.id)
                .filter(
                    CollectionMember.media_file_id == file_id,
                    SummaryPrompt.is_active,
                )
                .order_by(CollectionMember.added_at.asc())
                .first()
            )

            if result:
                logger.info(f"File {file_id} using collection default prompt: {result[0]}")
                return str(result[0])

        return None
    except Exception as e:
        logger.warning(f"Failed to get collection prompt for file {file_id}: {e}")
        return None


# Import for automatic summarization, speaker identification, and analytics
def trigger_automatic_summarization(
    file_id: int, file_uuid: str, tasks_to_run: list[str] | None = None
):
    """Trigger automatic summarization, speaker identification, and analytics after transcription completes.

    Note: In the default (full) flow, LLM speaker identification is dispatched by
    detect_speaker_attributes_task after gender detection completes, ensuring gender
    data is available to the LLM. When tasks_to_run explicitly includes 'speaker_llm',
    it is dispatched directly for selective reprocessing.

    Args:
        file_id: Internal file ID
        file_uuid: File UUID string
        tasks_to_run: Optional list of specific stages to run. None = run all tasks.
            Valid values: 'analytics', 'speaker_llm', 'summarization',
            'topic_extraction', 'search_indexing', 'speaker_clustering'
    """
    try:
        # Analytics computation
        if tasks_to_run is None or "analytics" in tasks_to_run:
            from app.tasks.analytics import analyze_transcript_task

            analytics_task = analyze_transcript_task.delay(file_uuid=file_uuid)
            logger.info(
                f"Automatic analytics computation task {analytics_task.id} started for file {file_id}"
            )

        # Speaker LLM identification: always chained from detect_speaker_attributes_task
        # (dispatched by _dispatch_speaker_attributes in postprocess). Gender detection
        # runs first, then chains to LLM speaker ID to ensure gender/age context is
        # available. No direct dispatch needed here — would cause double dispatch.

        # Note: search_indexing is dispatched in _process_transcription_result (always
        # runs during transcription). No need to dispatch it here to avoid double dispatch.

        # Look up collection default prompt for this file
        collection_prompt_uuid = _get_collection_prompt_uuid(file_id)

        # Summarization
        if tasks_to_run is None or "summarization" in tasks_to_run:
            _dispatch_automatic_summary(file_id, file_uuid, collection_prompt_uuid)

        # Topic extraction
        if tasks_to_run is None or "topic_extraction" in tasks_to_run:
            from app.tasks.topic_extraction import extract_topics_task

            topic_task = extract_topics_task.delay(file_uuid=file_uuid, force_regenerate=False)
            logger.info(
                f"Automatic topic extraction task {topic_task.id} started for file {file_id}"
            )

        # Speaker clustering (selective reprocessing only)
        _clustering_stages = {"speaker_clustering"}
        if tasks_to_run is not None and _clustering_stages & set(tasks_to_run):
            try:
                from app.db.session_utils import session_scope
                from app.models.media import MediaFile

                with session_scope() as _db:
                    _mf = _db.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
                    if not _mf:
                        logger.warning(f"File {file_uuid} not found for clustering dispatch")
                    else:
                        _uid = int(_mf.user_id)

                        if "speaker_clustering" in tasks_to_run:
                            try:
                                from app.tasks.speaker_clustering import cluster_speakers_for_file

                                cluster_speakers_for_file.delay(file_uuid, _uid)
                                logger.info(
                                    f"Selective speaker clustering dispatched for file {file_id}"
                                )
                            except Exception as sc_err:
                                logger.warning(f"Failed to dispatch speaker clustering: {sc_err}")

            except Exception as e:
                logger.warning(f"Failed to look up file for clustering dispatch: {e}")
    except Exception as e:
        logger.warning(f"Failed to start automatic tasks for file {file_id}: {e}")


def _extract_metadata_if_available(temp_file_path: str, ctx: TranscriptionContext) -> None:
    """Extract and save media metadata from file."""
    extracted_metadata = extract_media_metadata(temp_file_path)
    if not extracted_metadata:
        return

    with session_scope() as db:
        media_file = get_refreshed_object(db, MediaFile, ctx.file_id)
        if media_file:
            update_media_file_metadata(
                media_file, extracted_metadata, ctx.content_type, temp_file_path
            )
            db.commit()


def _download_and_extract_metadata(
    storage_path: str, temp_file_path: str, ctx: TranscriptionContext
) -> None:
    """Download file from MinIO and extract metadata (for video presigned URL path)."""
    try:
        from app.services.minio_service import download_file_to_path

        download_file_to_path(storage_path, temp_file_path)
        _extract_metadata_if_available(temp_file_path, ctx)
    except Exception as e:
        logger.warning(f"Metadata extraction failed for file {ctx.file_id}: {e}")


def _convert_asr_result_to_segments(result, media_file_id: int) -> list[dict]:
    """
    Convert an ASRResult from a cloud provider to the segment dict format used by storage.

    Args:
        result: ASRResult instance from a cloud ASR provider
        media_file_id: ID of the media file (unused here, kept for signature clarity)

    Returns:
        List of segment dicts matching the format expected by save_transcript_segments
        and process_segments_with_speakers.

    Notes:
        - Handles ``segment.words`` being None or an empty list (returns ``words: []``).
        - Handles ``segment.speaker`` being None (non-diarized providers).
        - Handles ``segment.confidence`` being None (mapped to None in output dict,
          which ``save_transcript_segments`` accepts via ``.get("confidence")``).
        - Each word dict uses the key ``"score"`` (not ``"confidence"``) to match
          the WhisperX output convention expected by ``process_segments_with_speakers``.
        - An empty ``result.segments`` list produces an empty return value, which is
          then caught by ``_validate_transcription_result`` in the calling pipeline.
    """
    segments = []
    for seg in result.segments:
        words = []
        for w in seg.words or []:
            words.append(
                {
                    "word": w.word,
                    "start": w.start,
                    "end": w.end,
                    "score": w.confidence if w.confidence is not None else 1.0,
                }
            )
        segments.append(
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "speaker": seg.speaker,  # already SPEAKER_XX format or None
                "confidence": seg.confidence,  # may be None — safe for .get("confidence")
                "words": words,
            }
        )
    return segments


def _run_parallel_cloud_asr_and_diarization(
    ctx: TranscriptionContext,
    audio_file_path: str,
    asr_config,
    asr_provider,
    progress_callback,
    min_speakers: int = 1,
    max_speakers: int = 20,
    num_speakers: int | None = None,
):
    """Run cloud ASR and pyannote.ai diarization in parallel, then merge.

    Both are I/O-bound HTTP calls, so ThreadPoolExecutor is the right pattern
    (consistent with migration_pipeline.py, speaker_attribute_task.py, llm_service.py).
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import as_completed

    from app.services.diarization.factory import DiarizationProviderFactory
    from app.services.diarization.types import DiarizeConfig
    from app.utils.diarization_merge import merge_cloud_diarization

    # Create diarization provider for this user
    with session_scope() as db:
        diarize_provider = DiarizationProviderFactory.create_for_user(ctx.user_id, db)

    if diarize_provider is None:
        logger.warning(
            "diarization_source=pyannote but no provider configured for user %d, "
            "falling back to ASR-only",
            ctx.user_id,
        )
        return asr_provider.transcribe(audio_file_path, asr_config, progress_callback)

    diarize_config = DiarizeConfig(
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        num_speakers=num_speakers,
    )

    logger.info(
        "Starting parallel cloud ASR (%s) + diarization (%s) for file %d",
        asr_provider.provider_name,
        diarize_provider.provider_name,
        ctx.file_id,
    )

    asr_result = None
    diarize_result = None
    asr_error = None
    diarize_error = None

    def run_asr():
        return asr_provider.transcribe(audio_file_path, asr_config, progress_callback)

    def run_diarize():
        return diarize_provider.diarize(audio_file_path, diarize_config)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="cloud-parallel") as pool:
        asr_future = pool.submit(run_asr)
        diarize_future = pool.submit(run_diarize)

        for future in as_completed([asr_future, diarize_future]):
            with contextlib.suppress(Exception):
                future.result()  # Errors collected below

    # Collect results
    try:
        asr_result = asr_future.result()
    except Exception as e:
        asr_error = e
        logger.error("Parallel cloud ASR failed: %s", e)

    try:
        diarize_result = diarize_future.result()
    except Exception as e:
        diarize_error = e
        logger.error("Parallel cloud diarization failed: %s", e)

    # ASR failure is fatal — can't proceed without transcript
    if asr_error:
        raise RuntimeError(f"Cloud ASR transcription failed: {asr_error}") from asr_error

    # Diarization failure is non-fatal — return ASR result without speakers
    if diarize_error or diarize_result is None:
        logger.warning("Cloud diarization failed, proceeding without speakers: %s", diarize_error)
        return asr_result

    # Both succeeded — merge diarization results onto ASR transcript
    assert asr_result is not None  # guaranteed by asr_error check above
    merged = merge_cloud_diarization(asr_result, diarize_result)
    logger.info(
        "Parallel cloud pipeline complete: ASR=%s, diarization=%s, file=%d",
        asr_provider.provider_name,
        diarize_provider.provider_name,
        ctx.file_id,
    )
    return merged


def _run_cloud_asr_pipeline(
    ctx: TranscriptionContext,
    audio_file_path: str,
    min_speakers: int | None,
    max_speakers: int | None,
    num_speakers: int | None,
    provider=None,
    diarization_source: str = "provider",
) -> dict:
    """
    Run the cloud ASR transcription pipeline for a non-local provider.

    Args:
        ctx: Transcription context
        audio_file_path: Path to the prepared audio file
        min_speakers: Optional minimum speakers hint
        max_speakers: Optional maximum speakers hint
        num_speakers: Optional fixed speaker count
        provider: Already-instantiated ASR provider (avoids a redundant DB lookup).
            If None, a new provider is created from DB config (fallback path).
        diarization_source: Where to run diarization ('provider', 'local', 'pyannote', 'off').

    Returns:
        Result dict with 'segments' and 'language' keys matching the WhisperX format
    """
    from app.services.asr.factory import ASRProviderFactory
    from app.services.asr.types import ASRConfig

    with session_scope() as db:
        # Re-use the already-created provider if supplied; otherwise create a fresh one.
        # This prevents a redundant DB round-trip when called from _process_file_in_temp_dir.
        if provider is None:
            provider = ASRProviderFactory.create_for_user(ctx.user_id, db)
        user_lang_settings = _get_user_language_settings(db, ctx.user_id)

        # Load active custom vocabulary terms for this user
        from app.models.custom_vocabulary import CustomVocabulary

        vocab_terms: list[str] = [
            row.term
            for row in db.query(CustomVocabulary.term)
            .filter(
                (CustomVocabulary.user_id == ctx.user_id) | CustomVocabulary.user_id.is_(None),
                CustomVocabulary.is_active.is_(True),
            )
            .all()
        ]

    logger.info(
        f"Running cloud ASR pipeline with provider '{provider.provider_name}' "
        f"for file {ctx.file_id}"
        + (f" ({len(vocab_terms)} vocabulary terms)" if vocab_terms else "")
    )

    send_progress_notification(ctx.user_id, ctx.file_id, 0.4, "Running cloud ASR transcription")

    # Only request diarization when the provider actually supports it; passing
    # enable_diarization=True to a provider that doesn't support it (e.g. OpenAI
    # whisper-1) wastes the parameter and can confuse the response parser.
    # Guard translation: only enable if provider supports it
    translate_requested = user_lang_settings["translate_to_english"]
    translate_enabled = translate_requested and provider.supports_translation()
    if translate_requested and not provider.supports_translation():
        logger.warning(
            "Provider %s does not support translation — proceeding without translation",
            provider.provider_name,
        )

    # Read user's speaker settings from DB (task param > user DB > env var)
    with session_scope() as db:
        user_settings = _get_user_transcription_settings(db, ctx.user_id)

    config = ASRConfig(
        language=user_lang_settings["source_language"],
        min_speakers=min_speakers if min_speakers is not None else user_settings["min_speakers"],
        max_speakers=max_speakers if max_speakers is not None else user_settings["max_speakers"],
        num_speakers=num_speakers if num_speakers is not None else settings.NUM_SPEAKERS,
        enable_diarization=(diarization_source == "provider" and provider.supports_diarization()),
        translate_to_english=translate_enabled,
        vocabulary=vocab_terms if vocab_terms else None,
    )

    def cloud_progress_callback(progress: float, message: str) -> None:
        with session_scope() as db:
            update_task_status(db, ctx.task_id, "in_progress", progress=progress)
        send_progress_notification(ctx.user_id, ctx.file_id, progress, message)

    with session_scope() as db:
        update_task_status(db, ctx.task_id, "in_progress", progress=0.4)

    # Parallel cloud execution: ASR + pyannote.ai diarization simultaneously
    if diarization_source == "pyannote":
        asr_result = _run_parallel_cloud_asr_and_diarization(
            ctx,
            audio_file_path,
            config,
            provider,
            cloud_progress_callback,
            min_speakers=min_speakers
            if min_speakers is not None
            else user_settings["min_speakers"],
            max_speakers=max_speakers
            if max_speakers is not None
            else user_settings["max_speakers"],
            num_speakers=num_speakers if num_speakers is not None else settings.NUM_SPEAKERS,
        )
    else:
        asr_result = provider.transcribe(audio_file_path, config, cloud_progress_callback)

    # Convert ASRResult to the dict format the rest of the pipeline expects
    raw_segments = _convert_asr_result_to_segments(asr_result, ctx.file_id)

    seg_speakers: set[str] = {s["speaker"] for s in raw_segments if s.get("speaker")}
    logger.info(
        "Cloud ASR pipeline: %d raw segments, %d speakers (%s), diarization_source=%s for file %d",
        len(raw_segments),
        len(seg_speakers),
        sorted(seg_speakers) if seg_speakers else "none",
        diarization_source,
        ctx.file_id,
    )

    return {
        "segments": raw_segments,
        "language": asr_result.language,
        "asr_provider": asr_result.provider_name,
        "asr_model": asr_result.model_name,
        "diarization_source": diarization_source,
    }


def _process_file_in_temp_dir(
    ctx: TranscriptionContext,
    temp_dir: str,
    file_data,
    file_ext: str,
    min_speakers: int | None,
    max_speakers: int | None,
    num_speakers: int | None,
    downstream_tasks: list[str] | None = None,
    minio_url: str | None = None,
) -> dict:
    """Process the transcription pipeline within a temporary directory."""
    import threading

    with session_scope() as db:
        update_task_status(db, ctx.task_id, "in_progress", progress=0.25)

    # Create progress callback for audio extraction phase (25% to 38% of overall progress)
    def audio_extraction_progress_callback(stage_progress: float, message: str) -> None:
        overall_progress = 0.25 + (stage_progress * 0.13)
        send_progress_notification(ctx.user_id, ctx.file_id, overall_progress, message)

    if minio_url:
        # Video file: FFmpeg reads directly from MinIO presigned URL
        # No need to download full video — just extract the audio stream
        send_progress_notification(ctx.user_id, ctx.file_id, 0.25, "Extracting audio from video")
        temp_audio_path = os.path.join(temp_dir, "audio.wav")
        from .audio_processor import extract_audio_from_video

        extract_audio_from_video(minio_url, temp_audio_path, audio_extraction_progress_callback)
        audio_file_path = temp_audio_path

        # Download file in background for metadata extraction only
        temp_file_path = os.path.join(temp_dir, f"input{file_ext}")
        metadata_thread = threading.Thread(
            target=_download_and_extract_metadata,
            args=(ctx.file_path, temp_file_path, ctx),
            name=f"metadata-{ctx.file_id}",
            daemon=True,
        )
        metadata_thread.start()
    else:
        # Audio file: use downloaded data
        temp_file_path = os.path.join(temp_dir, f"input{file_ext}")
        with open(temp_file_path, "wb") as f:
            f.write(file_data.read())

        # Run metadata extraction in background thread
        metadata_thread = threading.Thread(
            target=_extract_metadata_if_available,
            args=(temp_file_path, ctx),
            name=f"metadata-{ctx.file_id}",
            daemon=True,
        )
        metadata_thread.start()

        send_progress_notification(ctx.user_id, ctx.file_id, 0.25, "Starting audio preparation")
        audio_file_path = prepare_audio_for_transcription(
            temp_file_path,
            ctx.content_type,
            temp_dir,
            progress_callback=audio_extraction_progress_callback,
        )

    # Ensure metadata extraction completed (typically finishes well before audio extraction)
    metadata_thread.join(timeout=30)

    # Check whether the user has a cloud ASR provider configured.
    # If so, route to the cloud pipeline; otherwise fall through to the local WhisperX pipeline.
    # The provider instance is passed directly into _run_cloud_asr_pipeline to avoid a
    # redundant DB round-trip (factory.create_for_user hits the DB every call).
    #
    # Only fall back to local for factory/config-loading errors (e.g. DB connectivity or
    # missing env vars at startup).  Errors from the provider's transcribe() call itself
    # (network failures, quota exceeded, bad API key) are re-raised so the outer handler
    # marks the file as FAILED with a clear error message rather than silently re-running
    # on local GPU (which can be very confusing in production).
    try:
        from app.services.asr.factory import ASRProviderFactory

        with session_scope() as db:
            provider = ASRProviderFactory.create_for_user(ctx.user_id, db)
    except Exception as factory_exc:
        logger.warning(
            "Failed to instantiate ASR provider for file %d (user %d), "
            "falling back to local pipeline: %s",
            ctx.file_id,
            ctx.user_id,
            factory_exc,
        )
        provider = None  # type: ignore[assignment]

    if provider is not None and provider.provider_name != "local":
        # Cloud pipeline — errors propagate so the task is marked FAILED, not silently
        # re-attempted on local GPU.
        result = _run_cloud_asr_pipeline(
            ctx,
            audio_file_path,
            min_speakers,
            max_speakers,
            num_speakers,
            provider=provider,
        )
        # Validate transcription result
        validation_error = _validate_transcription_result(result, ctx, ctx.task_id)
        if validation_error:
            return validation_error
        return _process_transcription_result(ctx, result, audio_file_path, downstream_tasks)

    # Local pipeline
    result = _run_transcription_pipeline(
        ctx, audio_file_path, min_speakers, max_speakers, num_speakers
    )

    # Validate transcription result
    validation_error = _validate_transcription_result(result, ctx, ctx.task_id)
    if validation_error:
        return validation_error

    # Process successful result
    return _process_transcription_result(ctx, result, audio_file_path, downstream_tasks)


def _handle_outer_exception(
    ctx: TranscriptionContext | None, task_id: str, error: Exception
) -> dict:
    """Handle top-level exception in transcription task."""
    file_id = ctx.file_id if ctx else None
    user_id = ctx.user_id if ctx else None
    error_msg = str(error)

    logger.error(f"Error processing file {file_id}: {error_msg}")

    try:
        with session_scope() as db:
            if file_id:
                update_media_file_status(db, file_id, FileStatus.ERROR)
                media_file = get_refreshed_object(db, MediaFile, file_id)
                if media_file:
                    media_file.error_category = categorize_error(error_msg).value
                    db.commit()
            update_task_status(db, task_id, "failed", error_message=error_msg, completed=True)

        if user_id and file_id:
            send_error_notification(user_id, file_id, error_msg)
    except Exception as update_err:
        logger.error(f"Error updating task status: {update_err}")

    return {"status": "error", "message": error_msg}


@celery_app.task(bind=True, name="transcription.process_file", priority=GPUPriority.USER_IMPORT)
def transcribe_audio_task(
    self,
    file_uuid: str,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    downstream_tasks: list[str] | None = None,
):
    """Process an audio/video file for transcription and speaker diarization.

    Uses the unified faster-whisper + PyAnnote v4 pipeline with
    user-configurable VAD and accuracy settings.

    Args:
        file_uuid: UUID of the MediaFile to transcribe.
        min_speakers: Minimum speakers for diarization (falls back to settings).
        max_speakers: Maximum speakers for diarization (falls back to settings).
        num_speakers: Fixed speaker count for diarization (falls back to settings).
        downstream_tasks: Optional list of specific post-transcription stages to run.
            None = run all tasks. Valid values: 'analytics', 'speaker_llm',
            'summarization', 'topic_extraction', 'search_indexing'.
    """
    task_id = self.request.id
    ctx = None

    try:
        # Get file information and create context
        ctx = _get_media_file_context(file_uuid, task_id)
        if not ctx:
            return {"status": "error", "message": f"Media file with UUID {file_uuid} not found"}

        # Send processing notification
        send_processing_notification(ctx.user_id, ctx.file_id)

        # Create and initialize task record
        with session_scope() as db:
            create_task_record(db, task_id, ctx.user_id, ctx.file_id, "transcription")
            update_task_status(db, task_id, "in_progress", progress=0.1)

        file_ext = get_audio_file_extension(ctx.content_type, ctx.file_name)
        is_video = ctx.content_type.startswith("video/")

        # For video files: stream audio directly from MinIO via presigned URL
        # (avoids downloading full video through Python — FFmpeg reads MinIO directly)
        # For audio files: download normally (small, fast)
        if is_video:
            from app.services.minio_service import get_file_url

            logger.info(f"Generating presigned URL for direct FFmpeg access: {ctx.file_path}")
            minio_url = get_file_url(ctx.file_path, expires=3600)
            file_data = None
        else:
            logger.info(f"Downloading audio file {ctx.file_path}")
            file_data, _, _ = download_file(ctx.file_path)
            minio_url = None

        # Process in temporary directory
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                return _process_file_in_temp_dir(
                    ctx,
                    temp_dir,
                    file_data,
                    file_ext,
                    min_speakers,
                    max_speakers,
                    num_speakers,
                    downstream_tasks,
                    minio_url=minio_url,
                )
        except PermissionError as e:
            logger.error(f"PyAnnote model access error: {str(e)}")
            return _handle_transcription_failure(ctx, task_id, str(e), "gated_model_access")
        except Exception as e:
            logger.error(f"Error in transcription processing: {str(e)}")
            error_message = _get_user_friendly_error_message(str(e))
            return _handle_transcription_failure(ctx, task_id, error_message, "processing_error")

    except Exception as e:
        return _handle_outer_exception(ctx, task_id, e)


# ---------------------------------------------------------------------------
# Pipeline chain: GPU-only task (Stage 2 of 3)
# Receives preprocess result from CPU task, runs Whisper + PyAnnote on GPU,
# saves segments to DB, returns context for CPU postprocess task.
# ---------------------------------------------------------------------------


def _process_and_save_critical(
    ctx: TranscriptionContext,
    result: dict,
    preprocess_context: dict,
) -> dict:
    """Process speakers, save transcript to DB, release GPU. Returns chain context."""
    from app.utils.hardware_detection import detect_hardware

    post_start = time.perf_counter()

    # Resegment at speaker boundaries and merge adjacent same-speaker segments
    from app.utils.segment_postprocess import merge_consecutive_segments
    from app.utils.segment_postprocess import resegment_by_speaker

    send_progress_notification(ctx.user_id, ctx.file_id, 0.68, "Processing speaker segments")
    pre_merge_count = len(result["segments"])
    result["segments"] = merge_consecutive_segments(resegment_by_speaker(result["segments"]))
    post_merge_speakers = {s.get("speaker") for s in result["segments"] if s.get("speaker")}
    logger.info(
        "Segment processing: %d pre-merge → %d post-merge, speakers: %s (file %d)",
        pre_merge_count,
        len(result["segments"]),
        sorted(post_merge_speakers) if post_merge_speakers else "none",
        ctx.file_id,
    )
    unique_speakers = extract_unique_speakers(result["segments"])

    with session_scope() as db:
        speaker_mapping = create_speaker_mapping(db, ctx.user_id, ctx.file_id, unique_speakers)
        update_task_status(db, ctx.task_id, "in_progress", progress=0.72)

    processed_segments = process_segments_with_speakers(result["segments"], speaker_mapping)

    # Mark overlapping segments
    enable_overlap = os.getenv("ENABLE_OVERLAP_DETECTION", "true").lower() == "true"
    overlap_info = result.get("overlap_info", {})
    overlap_regions = overlap_info.get("regions", [])
    if enable_overlap and overlap_regions:
        processed_segments = mark_overlapping_segments(processed_segments, overlap_regions)

    # Garbage cleanup
    with session_scope() as db:
        from app.services import system_settings_service

        garbage_config = system_settings_service.get_garbage_cleanup_config(db)
    if garbage_config["garbage_cleanup_enabled"]:
        processed_segments, _ = clean_garbage_words(
            processed_segments, garbage_config["max_word_length"]
        )

    # Save to database
    send_progress_notification(ctx.user_id, ctx.file_id, 0.75, "Saving transcript to database")
    whisper_model = os.getenv("WHISPER_MODEL", "large-v3-turbo")
    diarization_disabled = result.get("diarization_disabled", False)
    diarization_model = None if diarization_disabled else "pyannote/speaker-diarization-community-1"
    try:
        from app.services.embedding_mode_service import EmbeddingModeService

        embedding_mode = EmbeddingModeService.get_current_mode()
    except Exception:
        embedding_mode = None

    with session_scope() as db:
        save_transcript_segments(db, ctx.file_id, processed_segments)
        update_media_file_transcription_status(
            db,
            ctx.file_id,
            processed_segments,
            result.get("language", "en"),
            whisper_model=whisper_model,
            diarization_model=diarization_model,
            embedding_mode=embedding_mode,
            asr_provider=result.get("asr_provider"),
            asr_model=result.get("asr_model"),
            diarization_disabled=diarization_disabled,
        )
        update_task_status(db, ctx.task_id, "in_progress", progress=0.78)

    # Release GPU memory
    hardware_config = detect_hardware()
    hardware_config.optimize_memory_usage()

    elapsed = time.perf_counter() - post_start
    logger.info(
        f"TIMING: critical path completed in {elapsed:.3f}s for file {ctx.file_id} "
        "(GPU worker now free)"
    )

    # Serialize native embeddings for JSON chain transfer
    native_embeddings_serialized = None
    use_native = False
    native_embs = result.get("native_speaker_embeddings")
    if native_embs:
        use_native = _should_use_native_embeddings(result)
        native_embeddings_serialized = {
            label: emb.tolist() if hasattr(emb, "tolist") else list(emb)
            for label, emb in native_embs.items()
        }

    return {
        "status": "success",
        "file_uuid": ctx.file_uuid,
        "file_id": ctx.file_id,
        "user_id": ctx.user_id,
        "task_id": ctx.task_id,
        "language": result.get("language", "en"),
        "segment_count": len(processed_segments),
        "speaker_mapping": speaker_mapping,
        "native_embeddings": native_embeddings_serialized,
        "use_native_embeddings": use_native,
        "asr_provider": result.get("asr_provider", "local"),
        "diarization_source": result.get("diarization_source", "provider"),
        "diarization_disabled": result.get("diarization_disabled", False),
        "downstream_tasks": preprocess_context.get("downstream_tasks"),
        "audio_temp_path": preprocess_context.get("audio_temp_path"),
    }


@celery_app.task(
    bind=True,
    name="transcription.gpu_transcribe",
    priority=GPUPriority.USER_IMPORT,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=1,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=30,
)
def transcribe_gpu_task(self, preprocess_context: dict) -> dict:
    """GPU-only: Whisper transcription + PyAnnote diarization + save to DB.

    Stage 2 of the 3-stage pipeline chain. Receives context from
    preprocess_for_transcription (CPU), runs AI models on GPU, saves
    segments to DB, returns context for finalize_transcription (CPU).
    """
    task_id = preprocess_context["task_id"]
    file_uuid = preprocess_context["file_uuid"]
    file_id = preprocess_context["file_id"]
    user_id = preprocess_context["user_id"]

    # Record GPU received timestamp + pickup markers for inter-stage gap
    # measurement. gpu_task_prerun and gpu_received fire at roughly the same
    # instant; we keep both so the Redis hash schema stays stable across the
    # legacy `gpu_received` tooling and the new `*_task_prerun` convention.
    benchmark_timing.mark(task_id, "gpu_received")
    benchmark_timing.mark(task_id, "gpu_task_prerun")
    benchmark_timing.mark_cold_start(task_id, "gpu")

    ctx = TranscriptionContext(
        task_id=task_id,
        file_id=file_id,
        file_uuid=file_uuid,
        user_id=user_id,
        file_path=preprocess_context["storage_path"],
        file_name=preprocess_context["file_name"],
        content_type=preprocess_context["content_type"],
    )

    try:
        from app.services.minio_service import download_temp_audio

        with session_scope() as db:
            update_task_status(db, task_id, "in_progress", progress=0.22)

        # ── Check for cloud ASR provider (needed in both code paths) ──────────
        try:
            from app.services.asr.factory import ASRProviderFactory

            with session_scope() as db:
                provider = ASRProviderFactory.create_for_user(user_id, db)
        except Exception:
            provider = None

        # Read diarization settings (needed in both code paths)
        diarization_source = preprocess_context.get("diarization_source", "provider")
        disable_diarization = diarization_source == "off"
        with session_scope() as db:
            media_file = get_refreshed_object(db, MediaFile, file_id)
            if media_file:
                media_file.diarization_disabled = disable_diarization
                db.commit()

        whisper_model = preprocess_context.get("whisper_model")

        # ── Phase 1b fast path: shared-volume WAV skips MinIO download ────────
        # When the preprocess task wrote a WAV to the shared volume we can feed it
        # directly to Engine.run_gpu_stage() without touching MinIO or creating a
        # temp dir.  Cloud ASR always needs the MinIO download, so we only use
        # this path for local ASR.
        local_wav_path = preprocess_context.get("local_wav_path", "")
        has_shared_wav = (
            bool(local_wav_path)
            and os.path.exists(local_wav_path)
            and (provider is None or provider.provider_name == "local")
        )

        if has_shared_wav:
            logger.info(
                "GPU task: using engine fast path (shared WAV) for file %d — skipping "
                "MinIO download",
                file_id,
            )
            with benchmark_timing.stage(task_id, "gpu_audio_load"):
                pass  # no download — file is already local; mark stage for timing parity

            send_progress_notification(user_id, file_id, 0.25, "Starting AI transcription")

            result = _run_engine_pipeline(ctx, local_wav_path, preprocess_context)

            # Annotate result with diarization flags for downstream
            if isinstance(result, dict):
                result["diarization_disabled"] = disable_diarization
                result["diarization_source"] = diarization_source

            # Validate result
            validation_error = _validate_transcription_result(result, ctx, task_id)
            if validation_error:
                return {
                    "status": "error",
                    "file_uuid": file_uuid,
                    "file_id": file_id,
                    "task_id": task_id,
                }

            # Process speakers, save to DB, release GPU
            gpu_result = _process_and_save_critical(ctx, result, preprocess_context)

            benchmark_timing.mark(task_id, "gpu_end")
            if isinstance(result, dict):
                benchmark_timing.set_context(
                    task_id,
                    {
                        "asr_provider": result.get("asr_provider", "local"),
                        "asr_model": result.get("asr_model"),
                    },
                )

            return gpu_result

        # ── Fallback / cloud path: download from MinIO ────────────────────────
        with tempfile.TemporaryDirectory() as temp_dir:
            # Download preprocessed audio from MinIO temp
            step_start = time.perf_counter()
            local_audio_path = os.path.join(temp_dir, "audio.wav")
            with benchmark_timing.stage(task_id, "gpu_audio_load"):
                download_temp_audio(file_uuid, local_audio_path)
            logger.info(
                f"TIMING: audio download from temp completed in "
                f"{time.perf_counter() - step_start:.3f}s"
            )

            send_progress_notification(user_id, file_id, 0.25, "Starting AI transcription")

            if provider is not None and provider.provider_name != "local":
                # Per-task model override is only for local ASR
                if whisper_model:
                    logger.info(
                        "whisper_model override '%s' ignored for cloud ASR provider",
                        whisper_model,
                    )
                result = _run_cloud_asr_pipeline(
                    ctx,
                    local_audio_path,
                    preprocess_context.get("min_speakers"),
                    preprocess_context.get("max_speakers"),
                    preprocess_context.get("num_speakers"),
                    provider=provider,
                    diarization_source=diarization_source,
                )
            else:
                result = _run_transcription_pipeline(
                    ctx,
                    local_audio_path,
                    preprocess_context.get("min_speakers"),
                    preprocess_context.get("max_speakers"),
                    preprocess_context.get("num_speakers"),
                    source_language=preprocess_context.get("source_language"),
                    translate_to_english=preprocess_context.get("translate_to_english"),
                    disable_diarization=disable_diarization,
                    whisper_model=whisper_model,
                )

            # Annotate result with diarization flags for downstream
            if isinstance(result, dict):
                result["diarization_disabled"] = disable_diarization
                result["diarization_source"] = diarization_source

            # Validate result
            validation_error = _validate_transcription_result(result, ctx, task_id)
            if validation_error:
                return {
                    "status": "error",
                    "file_uuid": file_uuid,
                    "file_id": file_id,
                    "task_id": task_id,
                }

            # Process speakers, save to DB, release GPU
            gpu_result = _process_and_save_critical(ctx, result, preprocess_context)

            # Record GPU end timestamp for inter-stage gap measurement.
            # Persist ASR provider + model context for the timing table.
            benchmark_timing.mark(task_id, "gpu_end")
            if isinstance(result, dict):
                benchmark_timing.set_context(
                    task_id,
                    {
                        "asr_provider": result.get("asr_provider", "local"),
                        "asr_model": result.get("asr_model"),
                    },
                )

            return gpu_result

    except Exception as e:
        logger.error(f"GPU transcription failed for file {file_uuid}: {e}")
        error_message = _get_user_friendly_error_message(str(e))
        _handle_transcription_failure(ctx, task_id, error_message, "gpu_processing_error")
        raise


# ---------------------------------------------------------------------------
# CPU lightweight transcription task (Stage 2 alternative)
# Runs base/tiny Whisper models on CPU — zero GPU impact.
# Diarization is disabled (PyAnnote requires CUDA).
# ---------------------------------------------------------------------------


def _run_cpu_transcription(
    ctx: TranscriptionContext,
    audio_file_path: str,
    source_language: str | None = None,
    translate_to_english: bool | None = None,
    whisper_model: str | None = None,
) -> dict:
    """Run lightweight Whisper transcription on CPU."""
    from app.transcription import TranscriptionConfig
    from app.transcription import TranscriptionPipeline

    source_language, translate_to_english = _resolve_language_settings(
        ctx, source_language, translate_to_english
    )

    # Get user's transcription tuning settings
    with session_scope() as db:
        user_settings = _get_user_transcription_settings(db, ctx.user_id)

    overrides = dict(
        source_language=source_language,
        translate_to_english=translate_to_english,
        min_speakers=1,
        max_speakers=1,
        hf_token=settings.HUGGINGFACE_TOKEN,
        vad_threshold=user_settings["vad_threshold"],
        vad_min_silence_ms=user_settings["vad_min_silence_ms"],
        vad_min_speech_ms=user_settings["vad_min_speech_ms"],
        vad_speech_pad_ms=user_settings["vad_speech_pad_ms"],
        hallucination_silence_threshold=user_settings["hallucination_silence_threshold"],
        repetition_penalty=user_settings["repetition_penalty"],
    )

    if whisper_model and whisper_model in LIGHTWEIGHT_MODELS:
        overrides["model_name"] = whisper_model

    config = TranscriptionConfig.for_cpu_lightweight(**overrides)

    with session_scope() as db:
        update_task_status(db, ctx.task_id, "in_progress", progress=0.4)

    send_progress_notification(ctx.user_id, ctx.file_id, 0.4, "Running fast CPU transcription")

    def progress_callback(progress, message):
        with session_scope() as db:
            update_task_status(db, ctx.task_id, "in_progress", progress=progress)
        send_progress_notification(ctx.user_id, ctx.file_id, progress, message)

    pipeline = TranscriptionPipeline(config)
    raw_result = pipeline.process(
        audio_file_path, progress_callback=progress_callback, task_id=ctx.task_id
    )

    if isinstance(raw_result, dict):
        raw_result.setdefault("asr_provider", "local")
        raw_result.setdefault("asr_model", config.model_name)
        raw_result["diarization_disabled"] = True

    return raw_result


@celery_app.task(
    bind=True,
    name="transcription.cpu_transcribe",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=1,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=30,
)
def transcribe_cpu_task(self, preprocess_context: dict) -> dict:
    """CPU-only: Lightweight Whisper transcription (base/tiny models).

    Stage 2 alternative for the 3-stage pipeline chain. Runs on the CPU worker
    instead of GPU, using a small Whisper model with int8 quantization.
    Diarization is skipped (PyAnnote requires CUDA).
    """
    task_id = preprocess_context["task_id"]
    file_uuid = preprocess_context["file_uuid"]
    file_id = preprocess_context["file_id"]
    user_id = preprocess_context["user_id"]

    benchmark_timing.mark(task_id, "gpu_received")  # re-use key for CPU fast path
    benchmark_timing.mark(task_id, "gpu_task_prerun")
    benchmark_timing.mark_cold_start(task_id, "cpu_transcribe")

    ctx = TranscriptionContext(
        task_id=task_id,
        file_id=file_id,
        file_uuid=file_uuid,
        user_id=user_id,
        file_path=preprocess_context["storage_path"],
        file_name=preprocess_context["file_name"],
        content_type=preprocess_context["content_type"],
    )

    try:
        from app.services.minio_service import download_temp_audio

        with session_scope() as db:
            update_task_status(db, task_id, "in_progress", progress=0.22)

        with tempfile.TemporaryDirectory() as temp_dir:
            # Download preprocessed audio from MinIO temp
            local_audio_path = os.path.join(temp_dir, "audio.wav")
            with benchmark_timing.stage(task_id, "gpu_audio_load"):
                download_temp_audio(file_uuid, local_audio_path)

            send_progress_notification(user_id, file_id, 0.25, "Starting fast CPU transcription")

            # Persist diarization_disabled=True for CPU path
            with session_scope() as db:
                media_file = get_refreshed_object(db, MediaFile, file_id)
                if media_file:
                    media_file.diarization_disabled = True
                    db.commit()

            whisper_model = preprocess_context.get("whisper_model")

            result = _run_cpu_transcription(
                ctx,
                local_audio_path,
                source_language=preprocess_context.get("source_language"),
                translate_to_english=preprocess_context.get("translate_to_english"),
                whisper_model=whisper_model,
            )

            # Validate result
            validation_error = _validate_transcription_result(result, ctx, task_id)
            if validation_error:
                return {
                    "status": "error",
                    "file_uuid": file_uuid,
                    "file_id": file_id,
                    "task_id": task_id,
                }

            # Process speakers, save to DB (same as GPU path)
            cpu_result = _process_and_save_critical(ctx, result, preprocess_context)
            benchmark_timing.mark(task_id, "gpu_end")
            return cpu_result

    except Exception as e:
        logger.error(f"CPU transcription failed for file {file_uuid}: {e}")
        error_message = _get_user_friendly_error_message(str(e))
        _handle_transcription_failure(ctx, task_id, error_message, "cpu_processing_error")
        raise
