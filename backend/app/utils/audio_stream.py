"""The one audio-stream choice every decode path shares (issue #1263).

Rule: analyse the FIRST audio stream of the file, in container order. Three reasons:

* PyAV, which faster-whisper's ``decode_audio`` uses for transcription, decodes
  ``container.decode(audio=0)``, so the first stream is what ASR has always read.
* FFmpeg, given no ``-map``, selects the audio stream with the MOST CHANNELS (ffmpeg docs,
  "Stream selection"); in practice it also prefers a stream flagged default (measured on
  ffmpeg 6.1). On a multi-track file that can be a different track than ASR read, so speaker
  embeddings were computed on audio that was never transcribed.
* ``-map 0:a:0`` is the documented explicit selector for "first audio stream of input 0"
  (ffmpeg docs, ``-map`` option). A trailing ``?`` makes a missing stream non-fatal so a
  file with no audio still fails with the existing "does not contain any stream" message
  that callers classify as a no-audio-track error.

Every ffmpeg call that decodes audio must splice in :func:`audio_map_args` (or pass
:data:`AUDIO_MAP_SPEC` as ffmpeg-python's ``map=``); a structural test enforces it.
"""

AUDIO_STREAM_INDEX = 0
AUDIO_MAP_SPEC = f"0:a:{AUDIO_STREAM_INDEX}?"

#: ffprobe tag keys that carry a track's language, in the order a muxer is likely to use them.
_LANGUAGE_TAGS = ("language", "LANGUAGE")
#: ffprobe's marker for "language not set"; it is not a language.
_UNDETERMINED = frozenset({"und", "unk", "mis", "mul", "zxx", ""})


def effective_audio_index(chosen: int | None) -> int:
    """The audio-stream ordinal to decode: the user's choice, else the first stream."""
    return AUDIO_STREAM_INDEX if chosen is None else int(chosen)


def audio_map_spec(index: int | None = None) -> str:
    """The ``-map`` value for audio stream *index* (ordinal among AUDIO streams only).

    ``0:a:N`` is ffmpeg's "Nth audio stream of input 0" (ffmpeg docs, ``-map``); the
    trailing ``?`` keeps a missing stream non-fatal so the existing no-audio message stands.
    """
    return f"0:a:{effective_audio_index(index)}?"


def audio_map_args(index: int | None = None) -> list[str]:
    """Return the ffmpeg argv fragment selecting audio stream *index* (default: the first)."""
    return ["-map", audio_map_spec(index)]


def _stream_language(tags: dict[str, object]) -> str | None:
    for key in _LANGUAGE_TAGS:
        value = str(tags.get(key) or "").strip().lower()
        if value:
            return None if value in _UNDETERMINED else value
    return None


def audio_streams_from_ffprobe(data: dict) -> list[dict]:
    """Describe every audio stream in ``ffprobe -show_streams`` JSON. Pure.

    ``index`` is the ordinal among audio streams, i.e. the N in ``-map 0:a:N``, not
    ffprobe's container-wide stream index (which also counts video and data streams).
    ``language`` is the container's tag as written (an ISO 639-2 code such as ``por``), or
    ``None`` when the muxer left it unset or ``und``.
    """
    out: list[dict] = []
    for stream in data.get("streams") or []:
        if stream.get("codec_type") != "audio":
            continue
        tags = {str(k): v for k, v in (stream.get("tags") or {}).items()}
        title = str(tags.get("title") or tags.get("TITLE") or "").strip() or None
        try:
            channels = int(stream["channels"]) if stream.get("channels") is not None else None
        except (TypeError, ValueError):
            channels = None
        out.append(
            {
                "index": len(out),
                "codec": stream.get("codec_name"),
                "channels": channels,
                "language": _stream_language(tags),
                "title": title,
                "default": bool((stream.get("disposition") or {}).get("default")),
            }
        )
    return out


def validate_audio_stream_index(index: int, streams: list[dict] | None) -> bool:
    """True when *index* names a stream in the stored list (or no list is stored and it is 0)."""
    if index < 0:
        return False
    if not streams:
        return index == AUDIO_STREAM_INDEX
    return any(s.get("index") == index for s in streams)


def language_mismatch(detected: str | None, track_tag: str | None) -> bool:
    """True when the detected language and the track's tagged language name different languages.

    Both sides go through the repo's one parser (``utils.language.normalize_language``), so
    ``pt`` / ``por`` / ``pt-BR`` agree and an untagged track or an undetected file is never a
    mismatch: with only one side known there is nothing to compare.
    """
    from app.utils.language import normalize_language

    left = normalize_language(detected)
    right = normalize_language(track_tag)
    return left is not None and right is not None and left != right


def transcribed_track(streams: list[dict] | None, chosen: int | None) -> dict | None:
    """The stream the pipeline decodes: the chosen one, else the first. None if not probed."""
    wanted = effective_audio_index(chosen)
    return next((s for s in streams or [] if s.get("index") == wanted), None)
