"""Caption presentation rules: the numbers every cue is held to, and where they come from.

Every value here is taken from a published broadcast/streaming style guide, not tuned by
eye. Where the guides disagree, the stricter (more readable) value wins unless noted.

Sources:

- **BBC Subtitle Guidelines** v1.2 (bbc.github.io/subtitle-guidelines): ~37 characters
  per line for the 4:3-era teletext safe area, two lines preferred, break at natural
  linguistic points ("line breaks should be inserted at natural linguistic breaks"),
  never split a name, a number from its unit, or an article from its noun; speaker
  identification when the speaker changes and is not obvious; minimum display ~0.3 s per
  word, presentation rate 160-180 words per minute (~13-17 characters per second).
- **Netflix Timed Text Style Guide** (English and per-language pages): 42 characters per
  line, max 2 lines, max duration 7 s, minimum duration 5/6 s (20 frames at 24 fps),
  reading speed 20 cps adult / 17 cps children, 2-frame minimum gap, "close gaps of less
  than 12 frames to 2 frames", Simplified Chinese 16 characters/line at 9 cps, Japanese
  13 characters/line at 4 cps, Korean 16 characters/line at 12 cps, Arabic 42.
- **Ofcom, "Guidance on standards for subtitling"** (2017 re-issue): no more than two
  lines, dialogue from two speakers in one subtitle each on its own line, speaker
  changes made clear, presentation rate not above ~160-180 wpm for pre-recorded work.

Choices made where the guides leave room:

- Latin line length 42 (Netflix) rather than BBC's 37: the player is a modern 16:9
  element whose caption box is ~80% of the frame, which is what Netflix's 42 assumes.
- Reading speed 17 cps — the Netflix children's/BBC upper value — not 20: an ASR
  transcript of conversation is denser than scripted dialogue and is never condensed
  the way a professional subtitle is, so the slower rate buys back some of that.
- Minimum duration 1.0 s rather than Netflix's 0.833 s: the request that motivated this
  module asked for ~1 s, and BBC's per-word floor lands near it for short cues.
- One CJK preset (16 characters/line, 10 cps) instead of per-language values: the
  transcript carries no reliable per-segment language, and 16 is the Chinese/Korean
  figure (Japanese's 13 would wrap Chinese unnecessarily).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CaptionRules:
    """Limits one cue must satisfy.

    Attributes:
        max_line_chars: Characters per rendered line, spaces and the speaker label
            included (Netflix counts every character).
        max_lines: Lines per cue.
        max_cps: Target reading speed in characters per second; cue ends are extended
            toward it where the timeline has room.
        min_duration: Shortest a cue may be shown, in seconds, room permitting.
        max_duration: Longest a cue may be shown.
        frame_gap: The minimum gap kept between consecutive cues (2 frames at 24 fps).
        close_gap_below: A gap shorter than this is closed to ``frame_gap`` so captions
            do not flash off and on between consecutive cues (Netflix: 12 frames).
        max_linger: How far past its last spoken word a cue may be held to meet the
            reading-speed or minimum-duration rule.
        pause_split: A silence at least this long between two words always ends the
            cue: a caption must not stay up across a pause in speech.
    """

    max_line_chars: int
    max_lines: int = 2
    max_cps: float = 17.0
    min_duration: float = 1.0
    max_duration: float = 7.0
    frame_gap: float = 2 / 24
    close_gap_below: float = 0.5
    max_linger: float = 2.0
    pause_split: float = 1.5

    @property
    def max_cue_chars(self) -> int:
        """Upper bound on a cue's characters (every line full)."""
        return self.max_line_chars * self.max_lines


LATIN_RULES = CaptionRules(max_line_chars=42)
CJK_RULES = CaptionRules(max_line_chars=16, max_cps=10.0)
