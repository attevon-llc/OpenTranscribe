"""Serialize a caption track as WebVTT or SRT.

**WebVTT** marks each speaker's text with a voice span (``<v Name>``) for assistive
technology and players that understand it, and puts the visible name in a
``<c.speaker>`` class span so a player can style it apart from the speech (the
file-detail player does, in CSS). There is deliberately **no ``STYLE`` block**: ffmpeg
(6.1, and so Plex, Jellyfin and every ffmpeg-based converter) reads a file that has one as
having no cues at all, which made the download unusable there, and bold names are not worth
that. Every piece of transcript text and every name is escaped: speaker names are
user-typed, and a cue is parsed into DOM by the browser.

**SRT** has no voice or class markup that every consumer understands (ffmpeg's burn-in
renders this file), so the name is plain ``Name:`` text.
"""

from __future__ import annotations

from app.services.captions.model import CaptionTrack
from app.services.captions.model import Cue
from app.utils.time_format import format_srt_timestamp


def _vtt_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _vtt_timestamp(seconds: float) -> str:
    return format_srt_timestamp(seconds).replace(",", ".")


def _vtt_cue_text(cue: Cue) -> str:
    rendered: list[str] = []
    for part in cue.parts:
        lines = [_vtt_escape(line) for line in part.lines]
        if part.show_label and part.speaker_label and lines:
            label = _vtt_escape(part.speaker_label)
            if part.label_own_line:
                lines.insert(0, f"<c.speaker>{label}</c>:")
            else:
                lines[0] = f"<c.speaker>{label}</c>: {lines[0]}"
        body = "\n".join(lines)
        if part.speaker_label:
            voice = _vtt_escape(part.speaker_label).replace("\n", " ")
            body = f"<v {voice}>{body}</v>"
        rendered.append(body)
    return "\n".join(rendered)


def _ordered(track: CaptionTrack, extra: list[Cue] | None) -> list[Cue]:
    cues = list(track.cues)
    if extra:
        cues.extend(extra)
        cues.sort(key=lambda c: (c.start, c.end))
    return cues


def to_webvtt(track: CaptionTrack, extra: list[Cue] | None = None) -> str:
    """Render ``track`` (plus optional ``extra`` cues, e.g. comments) as WebVTT."""
    out = ["WEBVTT", ""]
    if track.interpolated_segments:
        out += [
            "NOTE",
            f"Cue timing for {track.interpolated_segments} of {track.segments} segments is "
            "interpolated from segment boundaries (they have no word timestamps).",
            "",
        ]
    for index, cue in enumerate(_ordered(track, extra), start=1):
        out += [
            str(index),
            f"{_vtt_timestamp(cue.start)} --> {_vtt_timestamp(cue.end)}",
            _vtt_cue_text(cue),
            "",
        ]
    return "\n".join(out)


def to_srt(track: CaptionTrack, extra: list[Cue] | None = None) -> str:
    """Render ``track`` (plus optional ``extra`` cues) as SubRip."""
    blocks = []
    for index, cue in enumerate(_ordered(track, extra), start=1):
        start, end = format_srt_timestamp(cue.start), format_srt_timestamp(cue.end)
        blocks.append(f"{index}\n{start} --> {end}\n" + "\n".join(cue.lines) + "\n")
    return "\n".join(blocks)
