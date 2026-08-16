"""Azure Cognitive Services Speech ASR provider.

Targets azure-cognitiveservices-speech >= 1.35.0.

Recognizer selection
--------------------
* **No diarization** — ``SpeechRecognizer`` with continuous recognition.
  Each ``recognized`` event carries an ``NBest[0]`` hypothesis with a
  ``Confidence`` score and per-word offsets/durations.

* **Diarization** — ``ConversationTranscriber`` is the only Azure SDK class
  that performs speaker diarization.  ``SpeechRecognizer`` does *not* return
  speaker IDs regardless of configuration; using it for diarization would
  silently produce transcripts with no speaker labels.

Both paths share the same threading-event termination pattern: the ``done``
event is set by ``session_stopped`` / ``transcribing_stopped`` or by the
``canceled`` callback so the recognizer always terminates. ``done.wait(timeout=7200)``'s
return value is checked: ``False`` means the wait genuinely timed out with no
completion signal ever received, and is raised as a sanitized ``RuntimeError``
(via the shared ``except Exception`` handler) instead of being treated as normal
completion and silently returning a truncated transcript.

Timestamp units
---------------
Azure reports ``offset`` and ``duration`` in 100-nanosecond ticks
(``REFERENCE_TIME`` units).  Dividing by 10 000 000 converts to seconds.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable

from .base import ASRProvider
from .types import ASRConfig
from .types import ASRResult
from .types import ASRSegment
from .types import ASRWord

logger = logging.getLogger(__name__)

# Azure REFERENCE_TIME ticks per second (100-ns units).
_TICKS_PER_SEC = 10_000_000


class AzureASRProvider(ASRProvider):
    def __init__(self, api_key: str, region: str = "eastus", model_name: str = "whisper"):
        self._api_key = api_key
        self._region = region
        self._model_name = model_name

    @property
    def provider_name(self) -> str:
        return "azure"

    def supports_diarization(self) -> bool:
        return True

    def supports_vocabulary(self) -> bool:
        return False

    def supports_translation(self) -> bool:
        return False

    def validate_connection(self) -> tuple[bool, str, float]:
        """Verify credentials by constructing a SpeechConfig (no network call beyond auth check)."""
        start = time.time()
        try:
            import azure.cognitiveservices.speech as sdk  # noqa: F401
        except ImportError:
            return (
                False,
                "azure-cognitiveservices-speech not installed. Run: pip install 'azure-cognitiveservices-speech>=1.35.0'",
                0.0,
            )
        try:
            sdk.SpeechConfig(subscription=self._api_key, region=self._region)
            ms = (time.time() - start) * 1000
            return True, f"Azure Speech validated (region: {self._region})", ms
        except Exception as e:
            ms = (time.time() - start) * 1000
            return False, self._sanitize_error(str(e), self._api_key), ms

    # ------------------------------------------------------------------
    # Public transcribe entrypoint
    # ------------------------------------------------------------------

    def transcribe(  # noqa: C901
        self,
        audio_path: str,
        config: ASRConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        try:
            import azure.cognitiveservices.speech as sdk
        except ImportError as err:
            raise RuntimeError(
                "azure-cognitiveservices-speech not installed. Run: pip install 'azure-cognitiveservices-speech>=1.35.0'"
            ) from err

        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        filename = os.path.basename(audio_path)
        t_start = time.time()
        logger.info(
            "Azure transcribe start: file=%s model=%s diarize=%s lang=%s",
            filename,
            self._model_name,
            config.enable_diarization,
            config.language,
        )

        if progress_callback:
            progress_callback(0.1, "Connecting to Azure Speech…")

        speech_cfg = sdk.SpeechConfig(subscription=self._api_key, region=self._region)
        lang = config.language if config.language != "auto" else "en-US"
        speech_cfg.speech_recognition_language = lang
        speech_cfg.output_format = sdk.OutputFormat.Detailed

        audio_cfg = sdk.audio.AudioConfig(filename=audio_path)

        if config.enable_diarization:
            segments, has_speakers = self._transcribe_with_diarization(
                sdk, speech_cfg, audio_cfg, filename, config, progress_callback
            )
        else:
            segments = self._transcribe_without_diarization(
                sdk, speech_cfg, audio_cfg, filename, progress_callback
            )
            has_speakers = False

        elapsed_ms = (time.time() - t_start) * 1000
        logger.info(
            "Azure transcribe complete: file=%s segments=%d duration_ms=%.0f",
            filename,
            len(segments),
            elapsed_ms,
        )

        if progress_callback:
            progress_callback(1.0, "Azure Speech transcription complete")

        return ASRResult(
            segments=segments,
            language=lang,
            has_speakers=has_speakers,
            provider_name="azure",
            model_name=self._model_name,
        )

    # ------------------------------------------------------------------
    # No-diarization path — SpeechRecognizer
    # ------------------------------------------------------------------

    def _transcribe_without_diarization(
        self,
        sdk,
        speech_cfg,
        audio_cfg,
        filename: str,
        progress_callback: Callable[[float, str], None] | None,
    ) -> list[ASRSegment]:
        segments: list[ASRSegment] = []
        cancellation_reason: list[str] = []
        done = threading.Event()

        def _on_recognized(evt) -> None:
            if evt.result.reason == sdk.ResultReason.RecognizedSpeech:
                try:
                    data = json.loads(evt.result.json)
                    best = data.get("NBest", [{}])[0]
                    offset_s = evt.result.offset / _TICKS_PER_SEC
                    dur_s = evt.result.duration / _TICKS_PER_SEC
                    words = [
                        ASRWord(
                            word=w.get("Word", ""),
                            start=w.get("Offset", 0) / _TICKS_PER_SEC,
                            end=(w.get("Offset", 0) + w.get("Duration", 0)) / _TICKS_PER_SEC,
                            confidence=w.get("Confidence", 1.0),
                        )
                        for w in best.get("Words", [])
                    ]
                    segments.append(
                        ASRSegment(
                            text=evt.result.text,
                            start=offset_s,
                            end=offset_s + dur_s,
                            confidence=best.get("Confidence"),
                            words=words,
                        )
                    )
                except Exception:
                    # Fall back to timing from the result object; end = start + duration.
                    offset_s = evt.result.offset / _TICKS_PER_SEC
                    dur_s = evt.result.duration / _TICKS_PER_SEC
                    segments.append(
                        ASRSegment(
                            text=evt.result.text,
                            start=offset_s,
                            end=offset_s + dur_s,
                        )
                    )

        def _on_canceled(evt) -> None:
            if evt.reason == sdk.CancellationReason.Error:
                err = self._sanitize_error(
                    f"Azure Speech canceled: {evt.error_code} {evt.error_details}",
                    self._api_key,
                )
                cancellation_reason.append(err)
                logger.error("Azure Speech cancellation error for file=%s: %s", filename, err)
            done.set()

        recognizer = sdk.SpeechRecognizer(speech_config=speech_cfg, audio_config=audio_cfg)
        recognizer.recognized.connect(_on_recognized)
        recognizer.session_stopped.connect(lambda _: done.set())
        recognizer.canceled.connect(_on_canceled)

        if progress_callback:
            progress_callback(0.2, "Transcribing with Azure Speech…")

        try:
            recognizer.start_continuous_recognition()
            if not done.wait(timeout=7200):
                raise TimeoutError(
                    "Azure Speech recognition timed out after 7200s with no "
                    "session_stopped/canceled signal"
                )
            recognizer.stop_continuous_recognition()
        except Exception as exc:
            sanitized = self._sanitize_error(str(exc), self._api_key)
            logger.error("Azure transcription failed for file=%s: %s", filename, sanitized)
            raise RuntimeError(f"Azure transcription failed: {sanitized}") from exc

        if cancellation_reason:
            raise RuntimeError(cancellation_reason[0])

        return segments

    # ------------------------------------------------------------------
    # Diarization path — ConversationTranscriber
    # ------------------------------------------------------------------

    def _transcribe_with_diarization(
        self,
        sdk,
        speech_cfg,
        audio_cfg,
        filename: str,
        config: ASRConfig,
        progress_callback: Callable[[float, str], None] | None,
    ) -> tuple[list[ASRSegment], bool]:
        """Use ConversationTranscriber for speaker-attributed transcription.

        ConversationTranscriber is the only Azure SDK component that returns
        per-utterance speaker IDs.  It exposes ``transcribed`` events (final
        results) that carry a ``speaker_id`` string (e.g. ``"Guest-1"``,
        ``"Guest-2"``).
        """
        segments: list[ASRSegment] = []
        cancellation_reason: list[str] = []
        done = threading.Event()

        def _on_transcribed(evt) -> None:
            """Handle a final transcription result with speaker ID."""
            result = evt.result
            if result.reason == sdk.ResultReason.RecognizedSpeech:
                try:
                    data = json.loads(result.json)
                    best = data.get("NBest", [{}])[0]
                    offset_s = result.offset / _TICKS_PER_SEC
                    dur_s = result.duration / _TICKS_PER_SEC
                    words = [
                        ASRWord(
                            word=w.get("Word", ""),
                            start=w.get("Offset", 0) / _TICKS_PER_SEC,
                            end=(w.get("Offset", 0) + w.get("Duration", 0)) / _TICKS_PER_SEC,
                            confidence=w.get("Confidence", 1.0),
                        )
                        for w in best.get("Words", [])
                    ]
                    speaker_id = getattr(result, "speaker_id", None)
                    segments.append(
                        ASRSegment(
                            text=result.text,
                            start=offset_s,
                            end=offset_s + dur_s,
                            speaker=self._normalize_speaker_label(speaker_id),
                            confidence=best.get("Confidence"),
                            words=words,
                        )
                    )
                except Exception:
                    offset_s = result.offset / _TICKS_PER_SEC
                    dur_s = result.duration / _TICKS_PER_SEC
                    speaker_id = getattr(result, "speaker_id", None)
                    segments.append(
                        ASRSegment(
                            text=result.text,
                            start=offset_s,
                            end=offset_s + dur_s,
                            speaker=self._normalize_speaker_label(speaker_id),
                        )
                    )

        def _on_canceled(evt) -> None:
            if evt.reason == sdk.CancellationReason.Error:
                err = self._sanitize_error(
                    f"Azure ConversationTranscriber canceled: {evt.error_code} {evt.error_details}",
                    self._api_key,
                )
                cancellation_reason.append(err)
                logger.error("Azure diarization cancellation error for file=%s: %s", filename, err)
            done.set()

        transcriber = sdk.transcription.ConversationTranscriber(
            speech_config=speech_cfg, audio_config=audio_cfg
        )
        transcriber.transcribed.connect(_on_transcribed)
        transcriber.session_stopped.connect(lambda _: done.set())
        transcriber.canceled.connect(_on_canceled)

        if progress_callback:
            progress_callback(0.2, "Transcribing with Azure ConversationTranscriber…")

        try:
            transcriber.start_transcribing_async().get()
            if not done.wait(timeout=7200):
                raise TimeoutError(
                    "Azure ConversationTranscriber timed out after 7200s with no "
                    "session_stopped/canceled signal"
                )
            transcriber.stop_transcribing_async().get()
        except Exception as exc:
            sanitized = self._sanitize_error(str(exc), self._api_key)
            logger.error("Azure diarization failed for file=%s: %s", filename, sanitized)
            raise RuntimeError(f"Azure diarization failed: {sanitized}") from exc

        if cancellation_reason:
            raise RuntimeError(cancellation_reason[0])

        has_speakers = any(s.speaker for s in segments)
        return segments, has_speakers
