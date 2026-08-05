"""Critical-path result processing: segments -> speakers -> database.

Boundary smoothing is applied at the ``finalize_segments()`` chokepoint
here (issue #193); see ``app/transcription/CLAUDE.md``.
"""

import logging
import os
import time

from app.db.session_utils import session_scope
from app.utils.task_utils import update_task_status

from .background import _run_post_gpu_background
from .context import TranscriptionContext
from .embeddings import _should_use_native_embeddings
from .notifications import send_progress_notification
from .speaker_processor import create_speaker_mapping
from .speaker_processor import extract_unique_speakers
from .speaker_processor import mark_overlapping_segments
from .speaker_processor import process_segments_with_speakers
from .storage import save_transcript_segments
from .storage import update_media_file_transcription_status

logger = logging.getLogger(__name__)


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

    # Boundary smoothing (issue #193) → resegment at speaker boundaries → merge same-speaker.
    # finalize_segments is the single chokepoint every transcription path routes through.
    from app.transcription.boundary_resolver import BoundarySmoothingConfig
    from app.utils.segment_postprocess import finalize_segments

    send_progress_notification(ctx.user_id, ctx.file_id, 0.68, "Processing speaker segments")
    step_start = time.perf_counter()
    with session_scope() as db:
        smoothing_cfg = BoundarySmoothingConfig.from_db_env(db)
    result["segments"] = finalize_segments(result["segments"], smoothing_cfg)
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


def _process_and_save_critical(
    ctx: TranscriptionContext,
    result: dict,
    preprocess_context: dict,
) -> dict:
    """Process speakers, save transcript to DB, release GPU. Returns chain context."""
    from app.utils.hardware_detection import detect_hardware

    post_start = time.perf_counter()

    # Boundary smoothing (issue #193) → resegment → merge, via the shared chokepoint.
    from app.transcription.boundary_resolver import BoundarySmoothingConfig
    from app.utils.segment_postprocess import finalize_segments

    send_progress_notification(ctx.user_id, ctx.file_id, 0.68, "Processing speaker segments")
    pre_merge_count = len(result["segments"])
    with session_scope() as db:
        smoothing_cfg = BoundarySmoothingConfig.from_db_env(db)
    result["segments"] = finalize_segments(result["segments"], smoothing_cfg)
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
