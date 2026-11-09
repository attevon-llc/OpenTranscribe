"""Log an exception's type and stack frames without its message (issue #1022).

The message of a rendering or parsing error can quote transcript text. Attach
``MessageFreeTracebackFilter`` to a logger whose records may carry such an
exception; sites then log with ``exc_info=True`` as usual and the formatted
traceback keeps every frame but drops ``str(exc)``, including any chained cause.
"""

from __future__ import annotations

import logging
import traceback


def format_frames_only(exc: BaseException) -> str:
    """Format ``exc`` as its stack frames plus type names, never its message."""
    lines: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        stack = traceback.extract_tb(current.__traceback__)
        lines.append("Traceback (most recent call last):\n")
        lines.extend(traceback.format_list(stack))
        lines.append(f"{type(current).__module__}.{type(current).__qualname__}\n")
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
        if current is not None:
            lines.append("\nCaused by:\n")
    return "".join(lines).rstrip("\n")


class MessageFreeTracebackFilter(logging.Filter):
    """Replace a record's traceback with frames and type names only."""

    def filter(self, record: logging.LogRecord) -> bool:
        info = record.exc_info
        if info and info[1] is not None:
            record.exc_text = format_frames_only(info[1])
            record.exc_info = None
        return True
