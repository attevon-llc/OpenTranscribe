"""Cloud ASR provider pipeline and cloud diarization merging."""

import contextlib
import logging

from app.core.config import settings
from app.db.session_utils import session_scope
from app.utils.task_utils import update_task_status

from .context import TranscriptionContext
from .notifications import send_progress_notification
from .user_settings import _get_user_language_settings
from .user_settings import _get_user_transcription_settings

logger = logging.getLogger(__name__)


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
