"""The caption data model shared by the segmenter, the timing pass and the renderers."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.captions.rules import CaptionRules


@dataclass
class CaptionSegment:
    """One transcript segment, as the segmenter needs it.

    Attributes:
        start: Segment start, seconds.
        end: Segment end, seconds.
        text: The text to caption (already masked if a redaction policy applies).
        speaker_key: Identity used to detect a speaker change (``None`` = unattributed).
        speaker_label: Name to display for that speaker, or ``None`` for no label.
        words: Word timestamps aligned to ``source_text``.
        source_text: The unmasked text ``words`` belong to; ``None`` = same as ``text``.
        overlap_group: Overlapping-speech group id; such segments get their own turn.
    """

    start: float
    end: float
    text: str
    speaker_key: object | None = None
    speaker_label: str | None = None
    words: list[dict] | None = None
    source_text: str | None = None
    overlap_group: object | None = None


@dataclass
class CuePart:
    """One speaker's text inside a cue (a dialogue cue has two parts, one line each)."""

    speaker_key: object | None
    speaker_label: str | None
    show_label: bool
    lines: list[str]
    #: The name is too long to share a line with speech (``segmenter.label_needs_own_line``):
    #: it is shown as a line of its own above the text instead of a prefix to it.
    label_own_line: bool = False

    def rendered_lines(self) -> list[str]:
        """Plain-text lines, the label prefixed to the first (or above them) when shown."""
        if not self.show_label or not self.speaker_label or not self.lines:
            return list(self.lines)
        if self.label_own_line:
            return [f"{self.speaker_label}:", *self.lines]
        return [f"{self.speaker_label}: {self.lines[0]}", *self.lines[1:]]


@dataclass
class Cue:
    """A caption cue."""

    start: float
    end: float
    parts: list[CuePart]
    rules: CaptionRules
    hold_until: float = 0.0

    @property
    def lines(self) -> list[str]:
        """Every rendered line of the cue, top to bottom."""
        return [line for part in self.parts for line in part.rendered_lines()]

    @property
    def chars(self) -> int:
        """Characters a viewer reads (labels included, line breaks excluded)."""
        return sum(len(line) for line in self.lines)


@dataclass
class CaptionTrack:
    """The result: cues in time order, plus how many segments had no word timing."""

    cues: list[Cue]
    segments: int = 0
    interpolated_segments: int = 0
