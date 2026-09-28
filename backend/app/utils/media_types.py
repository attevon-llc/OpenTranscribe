"""Canonical media MIME types (issue #1044).

A declared media type comes from the uploading browser's ``File.type``, which is
whatever the client OS's MIME database says. It can also come from Python's
``mimetypes``. Both drift. shared-mime-info 2.4 (Ubuntu 24.04, so Firefox on Linux)
and Python 3.14 both name ``*.wav`` ``audio/vnd.wave``. That is the IANA name, but
Firefox and Chromium reject it as a ``<source type>``, so a stored alias leaves the
media player at 00:00 even though the bytes decode fine.

Every alias below was checked against Playwright Firefox and Chromium: the alias
is rejected by at least one of them, and the canonical form is accepted by both,
except where noted. Types that are not listed pass through unchanged.
"""

from __future__ import annotations

_CANONICAL_BY_ALIAS: dict[str, str] = {
    "audio/vnd.wave": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/x-wav": "audio/wav",
    "audio/x-pn-wav": "audio/wav",
    "audio/x-flac": "audio/flac",
    "audio/x-aac": "audio/aac",
    "audio/aacp": "audio/aac",
    "audio/m4a": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
    "audio/mp4a-latm": "audio/mp4",
    "audio/mp3": "audio/mpeg",
    "audio/x-mp3": "audio/mpeg",
    "audio/mpeg3": "audio/mpeg",
    "audio/x-mpeg": "audio/mpeg",
    # *.opus is Ogg-contained Opus; `audio/opus` names the bare codec (RFC 7587).
    "audio/opus": "audio/ogg",
    # Consistency only: MKV plays under either spelling, and AVI and AIFF play under
    # neither (they need a transcode). One spelling per format keeps the magic-byte
    # table and the download paths simple.
    "video/matroska": "video/x-matroska",  # Python 3.14
    "video/vnd.avi": "video/x-msvideo",  # Python 3.14
    "video/avi": "video/x-msvideo",
    "video/msvideo": "video/x-msvideo",
    "audio/aiff": "audio/x-aiff",
}


def normalize_media_content_type(content_type: str | None) -> str | None:
    """Return the browser-playable canonical spelling of a media MIME type.

    Matching ignores case and parameters. An alias maps to its canonical type,
    and any parameters are dropped (they described the alias). An unknown type,
    such as ``audio/webm;codecs=opus`` from MediaRecorder, is returned unchanged.
    ``None`` and empty strings pass through, so callers keep their own fallback.
    """
    if not content_type:
        return content_type
    base = content_type.split(";", 1)[0].strip().lower()
    return _CANONICAL_BY_ALIAS.get(base, content_type)
