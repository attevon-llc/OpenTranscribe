"""Local (on-box) transcription pipelines and their configuration.

Covers the monolithic WhisperX pipeline, the Engine split-stage fast
path, and the transcribe-only stage used by the multi-GPU split.
"""

import logging
import os

from app.core.config import settings
from app.db.session_utils import session_scope
from app.transcription.config import LIGHTWEIGHT_MODELS
from app.utils.task_utils import update_task_status

from .context import TranscriptionContext
from .notifications import send_progress_notification
from .user_settings import _get_user_language_settings
from .user_settings import _get_user_transcription_settings

logger = logging.getLogger(__name__)


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
    with session_scope() as db:
        engine_config = EngineConfig.from_db_with_env_fallback(db)
    for k, v in overrides.items():
        if hasattr(engine_config, k):
            setattr(engine_config, k, v)
    engine_config._transcription_config = config

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

    # Both halves run on the GPU worker today. Log the split so the CPU-only tail is visible:
    # that tail is what T4 would move off the GPU slot, and moving it is only worth the extra
    # Redis hop if it is large next to the GPU stage it delays.
    logger.info(
        "TIMING: engine stages for file %s: %s",
        ctx.file_id,
        {k: round(v, 3) for k, v in sorted(job_result.stage_timings.items())},
    )

    raw_dict = job_result.to_pipeline_dict()
    raw_dict.setdefault("asr_provider", "local")
    raw_dict.setdefault("asr_model", config.model_name)
    return raw_dict


def _run_transcribe_only_stage(
    ctx: TranscriptionContext,
    local_wav_path: str,
    preprocess_context: dict,
) -> dict:
    """Engine Phase 4: Stage 2a — transcription only, no diarization.

    Builds the same EngineConfig / PreprocessResult as _run_engine_pipeline()
    but calls Engine.run_transcribe_only() instead of run_gpu_stage().
    Returns a serialized RawTranscriptResult for forwarding to diarize_gpu_task.

    Args:
        ctx: Transcription task context.
        local_wav_path: Shared-volume WAV path written by the preprocess task.
        preprocess_context: Raw dict from the preprocess task.

    Returns:
        Serialized RawTranscriptResult dict for diarize_gpu_task.
    """
    from app.transcription import Engine
    from app.transcription import EngineConfig
    from app.transcription import TranscriptionConfig as _TranscriptionConfig
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

    if (
        whisper_model
        and whisper_model not in LIGHTWEIGHT_MODELS
        and whisper_model != _TranscriptionConfig._pinned_model_name
    ):
        logger.warning(
            "Model override '%s' rejected in transcribe-only stage — using admin model '%s'.",
            whisper_model,
            _TranscriptionConfig._pinned_model_name,
        )

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

    transcript = engine.run_transcribe_only(pre, progress_callback=_progress)
    return transcript.serialize()
