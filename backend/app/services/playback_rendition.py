"""Browser playback renditions for media the browser cannot decode.

Every upload format is accepted and transcribed, but some originals do not play in
Firefox or Chromium (issue #1044's matrix, extended here). Instead of refusing them,
preprocessing probes the original and, when it will not play, queues
``media.create_playback_rendition``. That task encodes an AAC/M4A copy that every
mainstream browser plays and stores it next to the original. ``media_file.playback_path``
names the copy, ``/stream-url`` serves it, and the original is kept for download.

Two cases get a rendition:

* **convert_audio**: an audio file whose container or codec no browser decodes (AIFF,
  WMA, ALAC-in-M4A, AC-3, MP2, ADPCM WAV). The rendition is a full replacement for
  playback.
* **audio_only_preview**: a video whose picture no browser decodes (AVI, WMV, MPEG-PS/TS,
  FLV, 3GP, MPEG-4 Part 2, Theora, ProRes). Transcoding video is costly, and is tracked
  separately. Only the audio track is rendered, and the player says the video preview is
  unavailable.

Playable originals get nothing: no encode, no stored copy, no extra object. The playability
tables below were measured with Playwright Firefox and Chromium against real
ffmpeg-generated files. An entry is "playable" only when both browsers played it with
sound (and, for video, with a picture). Anything not listed gets a rendition. That is the
safe direction: an unneeded rendition still plays, while a missing one leaves the player
stuck at 00:00.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import subprocess
from dataclasses import dataclass

from app.utils.media_types import normalize_media_content_type

logger = logging.getLogger(__name__)

#: Every rendition is AAC in an MP4 (M4A) container. Firefox, Chromium, Safari and Edge
#: all play it, which Opus-in-Ogg cannot claim for older Safari.
RENDITION_CONTENT_TYPE = "audio/mp4"
RENDITION_SUFFIX = ".playback.m4a"
RENDITION_AUDIO_BITRATE = "128k"

PROBE_TIMEOUT_SECONDS = 120
#: Encode timeout floor. The measured cost is ~1 minute of CPU per hour of audio
#: (see ``encode_audio_rendition``), so the budget is scaled by duration on top of this.
ENCODE_TIMEOUT_FLOOR_SECONDS = 600


class PlaybackNeed(enum.StrEnum):
    """What an original needs before a browser can play it."""

    NONE = "none"
    CONVERT_AUDIO = "convert_audio"
    AUDIO_ONLY_PREVIEW = "audio_only_preview"


class PlaybackMode(enum.StrEnum):
    """What ``/stream-url`` is serving. Returned to the player as ``playback``."""

    ORIGINAL = "original"
    CONVERTED = "converted"
    AUDIO_ONLY = "audio_only"


# ffprobe reports ISO-BMFF (MP4/MOV/M4A/3GP) as "mov,mp4,m4a,3gp,3g2,mj2", Matroska and
# WebM as "matroska,webm". The first matching family below wins.
_ISO_BMFF = "mp4"
_MATROSKA = "matroska"

#: Audio codecs both browsers play, per container family (measured).
_PLAYABLE_AUDIO: dict[str, frozenset[str]] = {
    "mp3": frozenset({"mp3"}),
    "aac": frozenset({"aac"}),
    "flac": frozenset({"flac"}),
    "ogg": frozenset({"vorbis", "opus", "flac"}),
    # Every PCM layout tested plays, and so do mu-law, A-law and MP3-in-WAV.
    # IMA/MS ADPCM does not play.
    "wav": frozenset(
        {
            "pcm_u8",
            "pcm_s16le",
            "pcm_s24le",
            "pcm_s32le",
            "pcm_f32le",
            "pcm_mulaw",
            "pcm_alaw",
            "mp3",
        }
    ),
    # ALAC in M4A fails in both browsers.
    _ISO_BMFF: frozenset({"aac", "mp3", "flac", "opus"}),
    # FLAC and MP3 in Matroska play in Chromium but not in Firefox.
    _MATROSKA: frozenset({"aac", "opus", "vorbis"}),
}

#: Containers whose video both browsers decode. AVI, ASF/WMV, FLV, MPEG-PS/TS and Ogg
#: (Theora) are absent: neither browser opens them, or neither shows a picture.
_PLAYABLE_VIDEO_CONTAINERS = frozenset({_ISO_BMFF, _MATROSKA})

#: Video codecs both browsers decode. HEVC is kept as playable on purpose: Safari, Edge
#: and hardware-backed Chrome play it, and an audio-only rendition would take the picture
#: away from them. Playwright's Chromium build has no HEVC, so Linux Chromium users
#: without hardware decode don't get a picture. That is tracked with the video-transcode
#: follow-up.
_PLAYABLE_VIDEO_CODECS = frozenset({"h264", "vp8", "vp9", "av1", "hevc"})


@dataclass(frozen=True)
class MediaProbe:
    """The facts about an original that decide whether a browser plays it."""

    formats: frozenset[str]
    audio_codec: str | None
    video_codec: str | None
    major_brand: str | None = None
    channels: int | None = None
    sample_rate: int | None = None
    duration: float | None = None

    @property
    def family(self) -> str | None:
        """The container family used as the key into the playability tables."""
        if "mp4" in self.formats or "mov" in self.formats:
            return _ISO_BMFF
        if "matroska" in self.formats or "webm" in self.formats:
            return _MATROSKA
        for name in ("mp3", "aac", "flac", "ogg", "wav"):
            if name in self.formats:
                return name
        return None


@dataclass(frozen=True)
class PlaybackSource:
    """What the player should load for a media file."""

    object_name: str
    content_type: str
    mode: PlaybackMode


def _audio_plays(probe: MediaProbe) -> bool:
    family = probe.family
    if family is None or probe.audio_codec is None:
        return False
    return probe.audio_codec in _PLAYABLE_AUDIO.get(family, frozenset())


def _video_plays(probe: MediaProbe) -> bool:
    family = probe.family
    if family not in _PLAYABLE_VIDEO_CONTAINERS or probe.video_codec is None:
        return False
    # 3GP shares ffprobe's ISO-BMFF format name. Firefox refuses a 3GP video even when
    # it holds H.264, so the major brand is what tells it apart.
    if (probe.major_brand or "").lower().startswith("3g"):
        return False
    return probe.video_codec in _PLAYABLE_VIDEO_CODECS


def classify_playback(probe: MediaProbe) -> PlaybackNeed:
    """Decide what a probed original needs before both browsers can play it. Pure.

    A file with a real video stream is judged on its picture: if the picture cannot be
    shown, and there is audio, it gets an audio-only preview. A video whose picture
    plays but whose audio codec does not (AC-3 in MKV, PCM or ALAC in MOV) is left
    alone. Swapping its picture for sound would be a product decision, and remuxing the
    audio is part of the video-transcode follow-up.

    A file with no video stream (cover art does not count) is judged as audio, whatever
    its declared type says.
    """
    if probe.video_codec is not None:
        if _video_plays(probe) or probe.audio_codec is None:
            return PlaybackNeed.NONE
        return PlaybackNeed.AUDIO_ONLY_PREVIEW
    if probe.audio_codec is None or _audio_plays(probe):
        return PlaybackNeed.NONE
    return PlaybackNeed.CONVERT_AUDIO


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[call-overload,no-any-return]
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_ffprobe(data: dict) -> MediaProbe:
    """Build a :class:`MediaProbe` from ``ffprobe -show_format -show_streams`` JSON. Pure.

    An attached picture (the cover art in an MP3, M4A or FLAC) is a video stream as far as
    ffprobe is concerned. It is skipped, so the file is judged as the audio it is.
    """
    fmt = data.get("format") or {}
    formats = frozenset(
        part.strip().lower() for part in str(fmt.get("format_name") or "").split(",") if part
    )
    audio = None
    video = None
    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "audio" and audio is None:
            audio = stream
        elif kind == "video" and video is None:
            if (stream.get("disposition") or {}).get("attached_pic"):
                continue
            video = stream
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    return MediaProbe(
        formats=formats,
        audio_codec=(
            str(audio["codec_name"]).lower() if audio and audio.get("codec_name") else None
        ),
        video_codec=(
            str(video["codec_name"]).lower() if video and video.get("codec_name") else None
        ),
        major_brand=str(tags["major_brand"]) if tags.get("major_brand") else None,
        channels=_int_or_none(audio.get("channels")) if audio else None,
        sample_rate=_int_or_none(audio.get("sample_rate")) if audio else None,
        duration=_float_or_none(fmt.get("duration")),
    )


def probe_media(source: str) -> MediaProbe:
    """Run ffprobe on a local path or URL.

    Raises:
        RuntimeError: ffprobe failed or timed out.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        source,
    ]
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv, no shell
            cmd, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("ffprobe timed out") from e
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.decode(errors='replace')[:500]}")
    return parse_ffprobe(json.loads(result.stdout or b"{}"))


def rendition_object_name(storage_path: str) -> str:
    """The object key for a file's rendition, next to its original. Pure.

    Derived from the original's full key, not from its directory. URL imports store
    originals as ``media/{user}/{uuid}.ext``, so many files share a directory, while every
    original key is unique.
    """
    return f"{storage_path}{RENDITION_SUFFIX}"


def build_encode_command(source: str, output_path: str, probe: MediaProbe) -> list[str]:
    """The ffmpeg argv for an AAC/M4A rendition of the first audio track. Pure.

    ``-aac_coder fast``: measured on a 60-minute 44.1 kHz stereo AIFF in the backend
    image, the default two-loop coder took 157 s and the fast coder 56 s, both at the
    same bitrate. At 128 kb/s the audible difference does not matter for reviewing a
    transcript.
    """
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source,
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-c:a",
        "aac",
        "-aac_coder",
        "fast",
        "-b:a",
        RENDITION_AUDIO_BITRATE,
    ]
    # A review copy needs neither surround nor hi-res: 128 kb/s spread over six channels
    # sounds worse than stereo, and rates above 48 kHz only cost bytes.
    if probe.channels is not None and probe.channels > 2:
        cmd += ["-ac", "2"]
    if probe.sample_rate is not None and probe.sample_rate > 48000:
        cmd += ["-ar", "48000"]
    threads = os.environ.get("FFMPEG_THREADS", "").strip()
    if threads.isdigit() and int(threads) > 0:
        cmd += ["-threads", threads]
    cmd += ["-movflags", "+faststart", output_path]
    return cmd


def encode_audio_rendition(source: str, output_path: str, probe: MediaProbe) -> None:
    """Encode the rendition to ``output_path``.

    Raises:
        RuntimeError: ffmpeg failed, timed out, or wrote nothing.
    """
    timeout = ENCODE_TIMEOUT_FLOOR_SECONDS + int((probe.duration or 0) * 0.25)
    cmd = build_encode_command(source, output_path, probe)
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv, no shell
            cmd, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"rendition encode timed out after {timeout}s") from e
    if result.returncode != 0:
        raise RuntimeError(
            f"rendition encode failed: {result.stderr.decode(errors='replace')[:500]}"
        )
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("rendition encode produced no output")


def resolve_playback(
    content_type: str | None, storage_path: str | None, playback_path: str | None
) -> PlaybackSource | None:
    """What the player should load for a file: its rendition if it has one. Pure.

    Returns None when the file has no stored media at all.
    """
    if playback_path:
        is_video = bool(content_type and content_type.lower().startswith("video/"))
        return PlaybackSource(
            object_name=playback_path,
            content_type=RENDITION_CONTENT_TYPE,
            mode=PlaybackMode.AUDIO_ONLY if is_video else PlaybackMode.CONVERTED,
        )
    if not storage_path:
        return None
    return PlaybackSource(
        object_name=storage_path,
        content_type=normalize_media_content_type(content_type) or "application/octet-stream",
        mode=PlaybackMode.ORIGINAL,
    )
