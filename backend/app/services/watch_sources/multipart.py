"""Multi-part split-recording detection and ffmpeg stitching.

Dropped VTC/podcast connections produce recordings split as ``name_P001.ext``,
``name_P002.ext`` … This module groups such parts (by base name + extension,
within a time window), and stitches them with ffmpeg — stream-copy when the
parts share codec/params, re-encode otherwise. It never invents content: a lone
part imports as a normal standalone file.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from dataclasses import field

from app.services.watch_sources.base import RemoteFileInfo

logger = logging.getLogger(__name__)


@dataclass
class MultipartGroup:
    """A set of parts believed to belong to one recording session."""

    base_name: str
    extension: str
    parts: list[tuple[int, RemoteFileInfo]] = field(default_factory=list)
    is_complete: bool = False

    @property
    def ordered_files(self) -> list[RemoteFileInfo]:
        return [fi for _, fi in sorted(self.parts, key=lambda p: p[0])]

    @property
    def max_part(self) -> int:
        return max((n for n, _ in self.parts), default=0)


def parse_part(regex: str, filename: str) -> tuple[str, int, str] | None:
    """Return ``(base_name, part_number, extension)`` or None if no match.

    The regex must expose three groups: base name, zero-padded part number,
    and extension (including the dot) — the default
    ``^(.+?)_P(\\d{3})(\\.[^.]+)$`` does.
    """
    try:
        m = re.match(regex, filename)
    except re.error as e:
        raise ValueError(f"Invalid multipart regex: {e}") from e
    if not m or len(m.groups()) < 3:
        return None
    base, part, ext = m.group(1), m.group(2), m.group(3)
    try:
        return base, int(part), ext
    except ValueError:
        return None


def detect_groups(
    files: list[RemoteFileInfo],
    regex: str,
    time_window_hours: int = 24,
) -> tuple[list[MultipartGroup], list[RemoteFileInfo]]:
    """Split ``files`` into multi-part groups and standalone files.

    Files whose names don't match the regex are standalone. Matching files are
    grouped by ``(base_name, extension)``; a group whose parts span more than
    ``time_window_hours`` is treated as separate sessions and its parts fall
    back to standalone (different recordings reusing a base name).
    """
    grouped: dict[tuple[str, str], list[tuple[int, RemoteFileInfo]]] = {}
    standalone: list[RemoteFileInfo] = []

    for fi in files:
        parsed = parse_part(regex, fi.name)
        if not parsed:
            standalone.append(fi)
            continue
        base, part_num, ext = parsed
        grouped.setdefault((base, ext), []).append((part_num, fi))

    groups: list[MultipartGroup] = []
    window_seconds = time_window_hours * 3600
    for (base, ext), parts in grouped.items():
        if len(parts) == 1:
            # A lone part is not a group — import as standalone.
            standalone.append(parts[0][1])
            continue

        # Enforce the time window: if mtimes span more than the window, these
        # are different sessions reusing the base name — don't stitch.
        mtimes = [fi.modified_time for _, fi in parts if fi.modified_time]
        if mtimes and (max(mtimes) - min(mtimes)).total_seconds() > window_seconds:
            logger.info(
                "Multipart group %s spans > %dh — treating parts as standalone",
                base,
                time_window_hours,
            )
            standalone.extend(fi for _, fi in parts)
            continue

        part_numbers = sorted(n for n, _ in parts)
        is_complete = part_numbers == list(range(1, len(part_numbers) + 1))
        groups.append(
            MultipartGroup(base_name=base, extension=ext, parts=parts, is_complete=is_complete)
        )

    return groups, standalone


def generate_stitched_filename(base_name: str, extension: str) -> str:
    """Clean output name for a stitched recording (no ``_P###`` suffix)."""
    return f"{base_name}{extension}"


def _probe_stream_signature(path: str) -> tuple[str, str] | None:
    """Return ``(video_codec_or_'', audio_codec_or_'')`` via ffprobe, or None."""
    try:
        out = subprocess.run(  # noqa: S603 # nosec B603
            [  # noqa: S607 # nosec B607
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height,sample_rate",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        data = json.loads(out.stdout)
        vcodec = ""
        acodec = ""
        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and not vcodec:
                vcodec = f"{s.get('codec_name')}:{s.get('width')}x{s.get('height')}"
            elif s.get("codec_type") == "audio" and not acodec:
                acodec = f"{s.get('codec_name')}:{s.get('sample_rate')}"
        return vcodec, acodec
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        logger.warning("ffprobe failed for %s: %s", path, e)
        return None


def _compatible(paths: list[str]) -> bool:
    """True when all parts share codec/params (stream-copy concat is safe)."""
    sigs = [_probe_stream_signature(p) for p in paths]
    if any(s is None for s in sigs):
        return False
    return len(set(sigs)) == 1


def stitch_files(local_paths: list[str], output_path: str) -> bool:
    """Concatenate ordered ``local_paths`` into ``output_path`` with ffmpeg.

    Stream-copies when the parts are codec-compatible (fast, lossless); otherwise
    re-encodes to H.264/AAC. Returns True on a non-empty output.
    """
    if not local_paths:
        return False

    compatible = _compatible(local_paths)

    # ffmpeg concat demuxer needs a list file with absolute, escaped paths.
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as listf:
        for p in local_paths:
            escaped = os.path.abspath(p).replace("'", "'\\''")
            listf.write(f"file '{escaped}'\n")
        list_path = listf.name

    try:
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path]
        if compatible:
            cmd += ["-c", "copy"]
        else:
            logger.info("Multipart parts not codec-compatible — re-encoding")
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"]
        cmd.append(output_path)

        subprocess.run(cmd, capture_output=True, text=True, timeout=3600, check=True)  # noqa: S603 # nosec B603
        ok = os.path.exists(output_path) and os.path.getsize(output_path) > 0
        if not ok:
            logger.error("Stitched output missing or empty: %s", output_path)
        return ok
    except subprocess.CalledProcessError as e:
        logger.error("ffmpeg stitch failed: %s", e.stderr[-500:] if e.stderr else e)
        return False
    except (subprocess.SubprocessError, OSError) as e:
        logger.error("ffmpeg stitch error: %s", e)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.remove(list_path)
