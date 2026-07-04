"""Google Cloud Speech-to-Text v2 ASR provider.

Targets google-cloud-speech >= 2.24.0 (v1 synchronous SpeechClient).
Supports Chirp 3 and legacy long/short models.

Authentication: service account via GOOGLE_APPLICATION_CREDENTIALS or
the credentials_file constructor argument.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

from .base import ASRProvider
from .types import ASRConfig
from .types import ASRResult
from .types import ASRSegment
from .types import ASRWord

logger = logging.getLogger(__name__)


class GoogleASRProvider(ASRProvider):
    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = "chirp-3",
        credentials_file: str | None = None,
    ):
        self._api_key = api_key
        self._model_name = model_name
        self._credentials_file = credentials_file

    @property
    def provider_name(self) -> str:
        return "google"

    def supports_diarization(self) -> bool:
        return True

    def supports_vocabulary(self) -> bool:
        return False

    def supports_translation(self) -> bool:
        return False

    def _set_creds(self) -> None:
        if self._credentials_file:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self._credentials_file

    def validate_connection(self) -> tuple[bool, str, float]:
        """Verify credentials by instantiating a SpeechClient (no network call)."""
        start = time.time()
        try:
            from google.cloud import speech  # noqa: F401
        except ImportError:
            return (
                False,
                "google-cloud-speech not installed. Run: pip install 'google-cloud-speech>=2.24.0'",
                0.0,
            )
        try:
            self._set_creds()
            speech.SpeechClient()
            ms = (time.time() - start) * 1000
            return True, "Google Cloud Speech validated", ms
        except Exception as e:
            ms = (time.time() - start) * 1000
            # Sanitize — Google ADC errors can expose service-account tokens in some SDK versions.
            sanitized = self._sanitize_error(str(e), self._api_key)
            return False, sanitized, ms

    def transcribe(  # noqa: C901
        self,
        audio_path: str,
        config: ASRConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        try:
            from google.cloud import speech
        except ImportError as err:
            raise RuntimeError(
                "google-cloud-speech not installed. Run: pip install 'google-cloud-speech>=2.24.0'"
            ) from err

        # Validate the file exists before attempting network I/O.
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        filename = os.path.basename(audio_path)
        t_start = time.time()
        logger.info(
            "Google transcribe start: file=%s model=%s diarize=%s lang=%s",
            filename,
            self._model_name,
            config.enable_diarization,
            config.language,
        )

        self._set_creds()
        if progress_callback:
            progress_callback(0.1, "Uploading to Google Cloud Speech…")

        client = speech.SpeechClient()
        with open(audio_path, "rb") as f:
            audio_content = f.read()

        audio = speech.RecognitionAudio(content=audio_content)
        dz_cfg = None
        if config.enable_diarization:
            dz_cfg = speech.SpeakerDiarizationConfig(
                enable_speaker_diarization=True,
                min_speaker_count=config.min_speakers,
                max_speaker_count=config.max_speakers,
            )

        lang = config.language if config.language != "auto" else "en-US"
        recognition_cfg = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            language_code=lang,
            model=self._model_name,
            enable_automatic_punctuation=True,
            enable_word_time_offsets=True,
            diarization_config=dz_cfg,
        )

        if progress_callback:
            progress_callback(0.25, "Transcribing with Google Cloud Speech…")

        try:
            op = client.long_running_recognize(config=recognition_cfg, audio=audio)
            response = op.result(timeout=3600)
        except Exception as exc:
            logger.error("Google Cloud Speech transcription failed for file=%s: %s", filename, exc)
            raise RuntimeError(f"Google Cloud Speech transcription failed: {exc}") from exc

        elapsed_ms = (time.time() - t_start) * 1000
        logger.info("Google transcribe complete: file=%s duration_ms=%.0f", filename, elapsed_ms)

        if progress_callback:
            progress_callback(0.85, "Parsing Google response…")

        segments: list[ASRSegment] = []
        for res in response.results:
            alt = res.alternatives[0]
            word_list = alt.words or []
            if config.enable_diarization and word_list:
                # Group contiguous words by speaker_tag into one segment per speaker run.
                # Preserves temporal order rather than grouping all words for the same speaker
                # globally (which would lose conversational turn structure).
                #
                # Google speaker_tag semantics:
                #   0  = unknown / no tag assigned (treat as no speaker label)
                #   1+ = 1-indexed speaker IDs  → map to 0-indexed SPEAKER_XX labels
                cur_tag: int | None = None
                cur_ws: list = []
                for w in word_list:
                    raw_tag = getattr(w, "speaker_tag", 0)
                    # Normalize tag: 0 → None (unknown), 1-indexed N → 0-indexed (N-1)
                    tag = None if raw_tag == 0 else raw_tag
                    if tag != cur_tag:
                        if cur_ws:
                            text = " ".join(x.word for x in cur_ws)
                            asr_words = [
                                ASRWord(
                                    x.word, x.start_time.total_seconds(), x.end_time.total_seconds()
                                )
                                for x in cur_ws
                            ]
                            spk_label = (
                                f"SPEAKER_{(cur_tag - 1):02d}" if cur_tag is not None else None
                            )
                            segments.append(
                                ASRSegment(
                                    text=text,
                                    start=cur_ws[0].start_time.total_seconds(),
                                    end=cur_ws[-1].end_time.total_seconds(),
                                    speaker=spk_label,
                                    confidence=alt.confidence,
                                    words=asr_words,
                                )
                            )
                        cur_tag, cur_ws = tag, []
                    cur_ws.append(w)
                if cur_ws:
                    text = " ".join(x.word for x in cur_ws)
                    asr_words = [
                        ASRWord(x.word, x.start_time.total_seconds(), x.end_time.total_seconds())
                        for x in cur_ws
                    ]
                    spk_label = f"SPEAKER_{(cur_tag - 1):02d}" if cur_tag is not None else None
                    segments.append(
                        ASRSegment(
                            text=text,
                            start=cur_ws[0].start_time.total_seconds(),
                            end=cur_ws[-1].end_time.total_seconds(),
                            speaker=spk_label,
                            confidence=alt.confidence,
                            words=asr_words,
                        )
                    )
            else:
                asr_words = [
                    ASRWord(w.word, w.start_time.total_seconds(), w.end_time.total_seconds())
                    for w in word_list
                ]
                segments.append(
                    ASRSegment(
                        text=alt.transcript,
                        start=asr_words[0].start if asr_words else 0.0,
                        end=asr_words[-1].end if asr_words else 0.0,
                        confidence=alt.confidence,
                        words=asr_words,
                    )
                )

        if progress_callback:
            progress_callback(1.0, "Google Cloud Speech transcription complete")

        return ASRResult(
            segments=segments,
            language=lang,
            # has_speakers is True only when at least one segment actually received
            # a speaker label.  speaker_tag=0 words are treated as unknown and produce
            # speaker=None, so a response with only tag-0 words correctly yields False.
            has_speakers=config.enable_diarization and any(s.speaker for s in segments),
            provider_name="google",
            model_name=self._model_name,
        )
