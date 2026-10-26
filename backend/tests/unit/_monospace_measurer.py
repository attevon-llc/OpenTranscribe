"""A font-free stand-in for the PDF measurer: one point per character.

With ``PageSpec.text_width == 10`` a line holds ten characters, which keeps golden layouts
small enough to read and independent of any host font. Words wrap at spaces; a word longer
than the line is cut at the width, the contract the production measurer also honours.
"""

from __future__ import annotations

from app.services.transcript_document.model import Direction
from app.services.transcript_document.model import LineKind
from app.services.transcript_document.model import PageSpec


def tiny_spec(lines_per_page: int = 5, text_width: int = 10) -> PageSpec:
    """A page whose text width is ``text_width`` points and which holds ``lines_per_page`` lines."""
    return PageSpec(
        width=float(text_width + 50 + 40),
        height=float(lines_per_page * 10),
        margin_top=0.0,
        margin_bottom=0.0,
        margin_right=40.0,
        gutter_left=50.0,
        lines_per_page=lines_per_page,
    )


class MonospaceMeasurer:
    """Wrap by character count: ``int(width)`` characters per line."""

    def wrap(self, text: str, *, kind: LineKind, width: float, direction: Direction) -> list[str]:
        limit = int(width)
        lines: list[str] = []
        current = ""
        for word in text.split(" "):
            while len(word) > limit:
                if current:
                    lines.append(current)
                    current = ""
                lines.append(word[:limit])
                word = word[limit:]
            candidate = word if not current else f"{current} {word}"
            if len(candidate) <= limit:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines
