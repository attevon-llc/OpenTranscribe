"""The one place a ``Content-Disposition`` header value is built (issue #1283).

Starlette encodes response headers as latin-1, so an ``attachment; filename="<name>"`` built
from an Arabic, Chinese or accented name raises ``UnicodeEncodeError`` while the response is
sent, which the client sees as an HTTP 500 on every export. A quote or CR/LF inside a name also
breaks out of the quoted string. RFC 6266 solves the first with an ASCII ``filename=`` fallback
plus a percent-encoded ``filename*=UTF-8''...``; this module also removes everything that must
never reach a header.

``tests/unit/test_content_disposition_convention.py`` fails on any route that builds the value
by hand instead of calling :func:`attachment_header`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal
from urllib.parse import quote

from app.utils.filename import strip_control_and_bidi

_FALLBACK_STEM = "download"
_UNSAFE_IN_QUOTED = re.compile(r'["\\/;]')
_ASCII_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,10}$")
_SEPARATORS = re.compile(r"[/\\]")


def _ascii_fallback(name: str) -> str:
    """Best-effort ASCII rendering of ``name`` for clients that ignore ``filename*``."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    folded = _UNSAFE_IN_QUOTED.sub("_", folded).strip()
    stem = _ASCII_EXTENSION.sub("", folded)
    if any(ch.isalnum() for ch in stem):
        return folded
    # Nothing but punctuation survived (an Arabic or CJK stem): keep a real extension.
    extension = _ASCII_EXTENSION.search(folded)
    return _FALLBACK_STEM + (extension.group(0) if extension else "")


def attachment_header(
    name: str, *, disposition: Literal["attachment", "inline"] = "attachment"
) -> str:
    """Build a latin-1-safe RFC 6266 ``Content-Disposition`` value for ``name``.

    Args:
        name: The file name the browser should save the response under. Any text,
            including non-ASCII and hostile input.
        disposition: ``attachment`` (download) or ``inline``.

    Returns:
        ``<disposition>; filename="<ascii>"; filename*=UTF-8''<percent-encoded>``.
    """
    clean = _SEPARATORS.sub("_", strip_control_and_bidi(name)).strip()
    if not clean:
        clean = _FALLBACK_STEM
    return (
        f'{disposition}; filename="{_ascii_fallback(clean)}"; '
        f"filename*=UTF-8''{quote(clean, safe='')}"
    )
