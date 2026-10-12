"""Another provider's speech-to-text diarization, used only for its speakers (#1226).

pyannote.ai is the only hosted diarization-only service in reach (plan B). Every other
vendor detects speakers only while transcribing, so using vendor X for speakers alongside
vendor Y (or local Whisper) for the text means paying X for a transcription whose text is
thrown away. :class:`ASRDiarizationAdapter` is that: a :class:`DiarizationProvider` that runs
a saved ASR configuration with diarization on and keeps only the speaker turns.

- **Text is discarded.** :func:`speaker_turns` reads times and labels only; the vendor's
  ``ASRResult`` is dropped before ``diarize`` returns, and ``DiarizeResult`` has no text
  field. Nothing logs it (counts only).
- **Turns come from the vendor module's own parser.** Each provider module already parses
  its response into ``ASRSegment``/``ASRWord`` with labels normalised by the shared
  ``normalize_speaker_label``; the adapter does not re-read vendor JSON. Words carry their
  segment's speaker (no module keeps a per-word label), so a timed word becomes a piece of a
  turn and consecutive same-speaker pieces closer than :data:`COALESCE_GAP_S` are joined. A
  segment with no timed words (``gpt-4o-transcribe-diarize``, Azure without ``Words``) is one
  turn.
- **Errors are fixed sentences.** A vendor failure raises
  :class:`~app.services.diarization.errors.DiarizationProviderFailedError` (or its
  rate-limited subclass); the vendor's own text goes to the log, sanitised, never into the
  exception a caller might store or send.
- **No database.** The provider is built from a saved config by the factory inside a short
  read session; ``diarize`` makes the network call with no session open.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from collections.abc import Iterable

from app.services.asr.base import ASRProvider
from app.services.asr.base import normalize_speaker_label
from app.services.asr.errors import ASRRateLimitedError
from app.services.asr.types import ASRConfig
from app.services.asr.types import ASRResult

from .base import DiarizationProvider
from .errors import DiarizationProviderFailedError
from .errors import DiarizationRateLimitedError
from .errors import DiarizerConfigUnusableError
from .types import DiarizeConfig
from .types import DiarizeResult
from .types import DiarizeSegment

logger = logging.getLogger(__name__)

#: Two same-speaker pieces closer than this are one turn. Short pauses inside a turn are
#: not a speaker change; a longer silence is left as a gap, which ``assign_speakers``
#: fills from the nearest turn anyway.
COALESCE_GAP_S = 0.5


def speaker_turns(result: ASRResult, gap: float = COALESCE_GAP_S) -> list[DiarizeSegment]:
    """Speaker turns from a diarized transcription, without its text.

    Args:
        result: The vendor's parsed transcription.
        gap: Join consecutive same-speaker pieces separated by at most this many seconds.

    Returns:
        Turns sorted by start, labels in ``SPEAKER_XX`` form. Unlabelled segments (a vendor's
        "unknown speaker") and untimed pieces are skipped.
    """
    pieces: list[tuple[float, float, str]] = []
    for segment in result.segments:
        label = normalize_speaker_label(segment.speaker)
        if label is None:
            continue
        timed = [(w.start, w.end) for w in segment.words if w.end > w.start]
        if timed:
            pieces.extend((start, end, label) for start, end in timed)
        elif segment.end > segment.start:
            pieces.append((segment.start, segment.end, label))
    pieces.sort(key=lambda piece: (piece[0], piece[1]))

    turns: list[DiarizeSegment] = []
    for start, end, label in pieces:
        last = turns[-1] if turns else None
        if last is not None and last.speaker == label and start - last.end <= gap:
            last.end = max(last.end, end)
        else:
            turns.append(DiarizeSegment(start=start, end=end, speaker=label))
    return turns


class ASRDiarizationAdapter(DiarizationProvider):
    """Speaker turns from a speech-to-text vendor's own diarization; its text is discarded."""

    def __init__(
        self,
        asr_provider: ASRProvider,
        *,
        model_name: str = "",
        speaker_hints: Iterable[str] = (),
    ) -> None:
        """Wrap a cloud ASR provider that returns speakers.

        Args:
            asr_provider: A provider built from a saved configuration.
            model_name: The configured model, reported as the diarization model.
            speaker_hints: Which of min/max/num the provider module sends (catalog
                ``speaker_count_hints``).

        Raises:
            DiarizerConfigUnusableError: The provider is local or returns no speakers.
        """
        if asr_provider.provider_name == "local" or not asr_provider.supports_diarization():
            raise DiarizerConfigUnusableError()
        self._asr = asr_provider
        self._model_name = model_name
        self._speaker_hints = frozenset(speaker_hints)

    @property
    def provider_name(self) -> str:
        return self._asr.provider_name

    @property
    def model_name(self) -> str:
        return self._model_name

    def supports_speaker_count(self) -> bool:
        return bool(self._speaker_hints)

    def validate_connection(self) -> tuple[bool, str, float]:
        return self._asr.validate_connection()

    def diarize(
        self,
        audio_path: str,
        config: DiarizeConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> DiarizeResult:
        """Transcribe with the vendor's diarization on and keep only the speaker turns.

        Raises:
            DiarizationRateLimitedError: The vendor throttled the request (retryable).
            DiarizationProviderFailedError: Any other vendor failure.
        """
        request = ASRConfig(
            language="auto",
            min_speakers=config.min_speakers,
            max_speakers=config.max_speakers,
            num_speakers=config.num_speakers,
            enable_diarization=True,
            translate_to_english=False,
            vocabulary=None,
        )
        try:
            result = self._asr.transcribe(audio_path, request, progress_callback)
        except ASRRateLimitedError as exc:
            logger.warning(
                "Speaker detection via %s was rate limited: %s",
                self.provider_name,
                self._sanitize_error(str(exc)),
            )
            raise DiarizationRateLimitedError(
                provider=self.provider_name, retry_after=exc.retry_after
            ) from exc
        except Exception as exc:
            logger.error(
                "Speaker detection via %s failed (%s): %s",
                self.provider_name,
                type(exc).__name__,
                self._sanitize_error(str(exc)),
            )
            raise DiarizationProviderFailedError() from exc

        turns = speaker_turns(result)
        segment_count = len(result.segments)
        del result
        speakers = {turn.speaker for turn in turns}
        logger.info(
            "Speaker detection via %s: %d segments -> %d turns, %d speakers (text discarded)",
            self.provider_name,
            segment_count,
            len(turns),
            len(speakers),
        )
        return DiarizeResult(
            segments=turns,
            num_speakers=len(speakers),
            provider_name=self.provider_name,
            model_name=self._model_name,
            metadata={"via": "asr_adapter"},
        )
