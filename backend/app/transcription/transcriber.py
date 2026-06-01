"""Core transcription using faster-whisper BatchedInferencePipeline.

This is the key module: uses BatchedInferencePipeline.transcribe() with
word_timestamps=True to get batched speed (~76s for 3hr file) WITH
word-level timestamps (95% speaker accuracy). WhisperX hardcodes
word_timestamps off in its batched pipeline.
"""

import logging
import time

import numpy as np

from app.transcription.config import TranscriptionConfig

logger = logging.getLogger(__name__)

# Maximum plausible duration for a single word (seconds)
_MAX_WORD_DURATION = 5.0
# Words below this probability have unreliable cross-attention DTW timestamps
_LOW_CONFIDENCE_THRESHOLD = 0.3


def _validate_word_timestamps(words: list[dict], seg_start: float, seg_end: float) -> None:
    """Enforce monotonicity and plausible durations for word timestamps in-place.

    Cross-attention DTW can produce overlapping or implausibly long word
    timestamps. This pass ensures:
    1. Each word starts at or after the previous word ends (monotonicity)
    2. No single word spans more than _MAX_WORD_DURATION seconds
    3. All words remain within [seg_start, seg_end]
    """
    for i, word in enumerate(words):
        # Cap implausible durations first
        if word["end"] - word["start"] > _MAX_WORD_DURATION:
            word["end"] = min(word["start"] + _MAX_WORD_DURATION, seg_end)

        # Enforce monotonicity: word N starts at or after word N-1 ends
        if i > 0 and word["start"] < words[i - 1]["end"]:
            word["start"] = words[i - 1]["end"]
            # Re-check minimum duration after shift
            if word["end"] <= word["start"]:
                word["end"] = min(word["start"] + 0.01, seg_end)


def _is_crisperwhisper_model(model_name: str) -> bool:
    """Return True if the configured model resolves to a CrisperWhisper build.

    CrisperWhisper needs a verbatim-tokenizer normalization pass (see
    ``app.transcription.crisperwhisper_normalize``) that no other model requires.
    The check resolves the loadable repo id first so admin/env/DB short names
    (``crisperwhisper``), the PyTorch repo id, and the CT2 repo id all match, then
    falls back to a substring match on the raw name for robustness.

    Args:
        model_name: The configured/admin-set model name (short name or repo id).

    Returns:
        Whether the active model is a CrisperWhisper variant.
    """
    if not model_name:
        return False
    try:
        from app.services.asr.model_discovery import resolve_loadable_model_name

        resolved = resolve_loadable_model_name(model_name)
    except Exception:  # noqa: S110  # nosec B110 — fall back to raw name substring match
        resolved = model_name
    return "crisper" in resolved.lower() or "crisper" in model_name.lower()


def _resolve_task_and_language(
    model_name: str, source_language: str, translate_to_english: bool
) -> tuple[str, str | None]:
    """Resolve the faster-whisper ``task`` and ``language`` for a model's capabilities.

    Enforces the model's language capability as the final safety net, mirroring the
    capability-aware guards the frontend applies in the settings UI:

    * Translation is only requested when the model advertises ``supports_translation``;
      otherwise it falls back to ``transcribe`` (e.g. large-v3-turbo, CrisperWhisper).
    * For ``english_only`` models (CrisperWhisper, ``.en`` variants) a non-English source
      language is overridden to ``"en"`` so the model is never asked to decode a language
      it does not support. ``"auto"`` is left as auto-detect (``None``).

    Args:
        model_name: The loadable model name/repo id (used for catalog capability lookup).
        source_language: Configured source language ("auto" or an ISO code).
        translate_to_english: Whether the user requested translation to English.

    Returns:
        ``(task, language)`` where task is "translate" or "transcribe" and language is an
        ISO code or ``None`` for auto-detect.
    """
    from app.services.asr.factory import ASRProviderFactory

    caps = ASRProviderFactory.get_model_capabilities("local", model_name)

    if translate_to_english and caps["supports_translation"]:
        task = "translate"
    else:
        if translate_to_english:
            logger.warning(
                "Model %s does not support translation — falling back to transcribe",
                model_name,
            )
        task = "transcribe"

    language = source_language if source_language != "auto" else None

    if caps.get("language_support") == "english_only" and source_language not in ("auto", "en"):
        logger.warning(
            "Model %s is English-only — forcing language to 'en' (requested '%s' is not supported)",
            model_name,
            source_language,
        )
        language = "en"

    return task, language


def _interpolate_low_confidence_words(words: list[dict], seg_start: float, seg_end: float) -> None:
    """Interpolate timestamps for low-probability words from reliable neighbors.

    Words with probability below _LOW_CONFIDENCE_THRESHOLD have unreliable
    cross-attention DTW timestamps. Instead of trusting them, we interpolate
    from the nearest high-confidence neighbors, distributing time evenly
    across the low-confidence span.
    """
    if not words:
        return

    n = len(words)
    # Find runs of consecutive low-confidence words
    i = 0
    while i < n:
        if words[i]["probability"] >= _LOW_CONFIDENCE_THRESHOLD:
            i += 1
            continue

        # Found start of a low-confidence run
        run_start = i
        while i < n and words[i]["probability"] < _LOW_CONFIDENCE_THRESHOLD:
            i += 1
        run_end = i  # exclusive

        # Determine anchor timestamps from reliable neighbors
        left_time = words[run_start - 1]["end"] if run_start > 0 else seg_start
        right_time = words[run_end]["start"] if run_end < n else seg_end

        # Distribute time evenly across the low-confidence run
        run_len = run_end - run_start
        if right_time <= left_time:
            continue  # No room to interpolate
        step = (right_time - left_time) / run_len
        for j in range(run_start, run_end):
            offset = j - run_start
            words[j]["start"] = left_time + offset * step
            words[j]["end"] = left_time + (offset + 1) * step


class Transcriber:
    """Faster-whisper BatchedInferencePipeline with native word timestamps."""

    def __init__(self, config: TranscriptionConfig):
        self.config = config
        self._model = None
        self._pipeline = None

    @property
    def is_loaded(self) -> bool:
        return self._pipeline is not None

    def load_model(self) -> None:
        """Load the WhisperModel and wrap it in BatchedInferencePipeline."""
        from faster_whisper import BatchedInferencePipeline
        from faster_whisper import WhisperModel

        from app.services.asr.model_discovery import resolve_loadable_model_name

        step_start = time.perf_counter()

        # Map any short name / non-CT2 repo id to a CTranslate2 model id that WhisperModel
        # can load. This must happen at the load point (not only in _resolve_model_name) so
        # EVERY path resolves: admin/env/DB defaults AND an explicit model_name override or
        # a per-file reprocess (whisper_model). E.g. "crisperwhisper" or the PyTorch repo
        # "nyrahealth/CrisperWhisper" → "nyrahealth/faster_CrisperWhisper".
        model_id = resolve_loadable_model_name(self.config.model_name)

        logger.info(
            f"Loading faster-whisper model: {model_id} (requested: {self.config.model_name}), "
            f"device={self.config.device}, compute_type={self.config.compute_type}"
        )

        num_workers = self.config.concurrent_requests if self.config.concurrent_requests > 1 else 1
        self._model = WhisperModel(
            model_id,
            device=self.config.device,
            device_index=self.config.device_index,
            compute_type=self.config.compute_type,
            num_workers=num_workers,
        )
        self._pipeline = BatchedInferencePipeline(model=self._model)

        elapsed = time.perf_counter() - step_start
        logger.info(f"TIMING: transcriber model loaded in {elapsed:.3f}s")

    def transcribe(self, audio: np.ndarray) -> dict:
        """Batched transcription with word-level timestamps.

        Args:
            audio: Audio waveform as 16kHz mono float32 numpy array.

        Returns:
            Dict with keys:
                "segments": list of segment dicts, each with
                    "text", "start", "end", "words" (list of word dicts)
                "language": detected language code (str)
        """
        if not self.is_loaded:
            raise RuntimeError("Transcriber model not loaded. Call load_model() first.")

        step_start = time.perf_counter()

        task, language = _resolve_task_and_language(
            self.config.model_name,
            self.config.source_language,
            self.config.translate_to_english,
        )

        logger.info(
            f"Transcribing: task={task}, language={language or 'auto'}, "
            f"batch_size={self.config.batch_size}, beam_size={self.config.beam_size}"
        )

        assert self._pipeline is not None, "Pipeline not initialized"
        kwargs: dict = dict(
            batch_size=self.config.batch_size,
            word_timestamps=True,
            beam_size=self.config.beam_size,
            task=task,
            language=language,
            vad_filter=True,
            vad_parameters={
                "threshold": self.config.vad_threshold,
                "min_silence_duration_ms": self.config.vad_min_silence_ms,
                "min_speech_duration_ms": self.config.vad_min_speech_ms,
                "speech_pad_ms": self.config.vad_speech_pad_ms,
            },
            repetition_penalty=self.config.repetition_penalty,
        )
        if self.config.hallucination_silence_threshold is not None:
            kwargs["hallucination_silence_threshold"] = self.config.hallucination_silence_threshold

        segments_gen, info = self._pipeline.transcribe(audio, **kwargs)

        # Convert generator to list of dicts with timestamp validation
        audio_duration = len(audio) / 16000  # 16kHz sample rate
        segments = []
        total_words = 0
        for seg in segments_gen:
            seg_start = max(float(seg.start), 0.0)
            seg_end = min(float(seg.end), audio_duration)
            if seg_end <= seg_start:
                continue  # Skip invalid segments

            words = []
            if seg.words:
                for w in seg.words:
                    word_start = max(float(w.start), seg_start)
                    word_end = min(float(w.end), seg_end)
                    if word_end <= word_start:
                        word_end = min(word_start + 0.01, seg_end)
                    words.append(
                        {
                            "word": w.word,
                            "start": word_start,
                            "end": word_end,
                            "probability": float(w.probability),
                        }
                    )

                # Timestamp sanity: enforce monotonicity and cap implausible durations
                _validate_word_timestamps(words, seg_start, seg_end)

                # Interpolate timestamps for low-confidence words (probability < 0.3)
                # whose cross-attention DTW timestamps are unreliable
                _interpolate_low_confidence_words(words, seg_start, seg_end)

                total_words += len(words)

            segments.append(
                {
                    "text": seg.text.strip(),
                    "start": seg_start,
                    "end": seg_end,
                    "words": words,
                }
            )

        # CrisperWhisper's verbatim tokenizer emits comma-prefixed, punctuation-glued
        # word tokens with no internal spacing. Reshape them into the standard
        # leading-space whisper convention so downstream text rebuild, word counting,
        # garbage detection, diarization assignment and the issue-#193 boundary smoother
        # all work. Gated to CrisperWhisper only — turbo/large-v3 are untouched.
        if _is_crisperwhisper_model(self.config.model_name):
            from app.transcription.crisperwhisper_normalize import normalize_crisperwhisper

            segments = normalize_crisperwhisper(segments)
            total_words = sum(len(s["words"]) for s in segments)
            logger.info("Applied CrisperWhisper normalization to %d segments", len(segments))

        elapsed = time.perf_counter() - step_start
        logger.info(
            f"TIMING: transcription completed in {elapsed:.3f}s - "
            f"{len(segments)} segments, {total_words} words with timestamps, "
            f"language={info.language}"
        )

        return {"segments": segments, "language": info.language}

    def unload_model(self) -> None:
        """Release model memory.

        Explicitly releases torch allocator pool and runs GC so the
        Whisper→diarization handoff leaves as little VRAM resident as
        possible. See docs/diarization-vram-profile/whole-stack.md (A.6b).
        """
        import gc

        if self._pipeline is not None:
            del self._pipeline
            self._pipeline = None
        if self._model is not None:
            # CTranslate2 does not expose an unload API on faster-whisper's
            # WhisperModel wrapper, so rely on __del__ + allocator cleanup below.
            del self._model
            self._model = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.synchronize()
        except Exception as e:
            logger.debug(f"cuda cleanup skipped: {e}")
        logger.info("Transcriber model unloaded")
