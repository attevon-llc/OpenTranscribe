import contextlib
import datetime
import json
import logging
import os
import subprocess
import sys
from typing import Any

logger = logging.getLogger(__name__)

try:
    import exiftool
except ImportError:
    logging.warning("exiftool not found. Video metadata extraction will be limited.")


# Invalid date patterns to reject (common in downloaded/processed videos)
_INVALID_DATE_PATTERNS = frozenset(
    [
        "0000:00:00 00:00:00",
        "0000-00-00 00:00:00",
        "1970:01:01 00:00:00",
        "1970-01-01T00:00:00",
        "0000:00:00",
        "0000-00-00",
        "1970:01:01",
        "1970-01-01",
    ]
)

# Common date format patterns in media metadata
_DATE_PATTERNS = [
    "%Y:%m:%d %H:%M:%S",  # ISO format: 2023:12:25 14:30:45
    "%Y:%m:%d %H:%M:%S%z",  # ISO with timezone: 2023:12:25 14:30:45+00:00
    "%Y-%m-%dT%H:%M:%S",  # Standard ISO: 2023-12-25T14:30:45
    "%Y-%m-%dT%H:%M:%SZ",  # ISO with timezone: 2023-12-25T14:30:45Z
    "%Y-%m-%dT%H:%M:%S%z",  # ISO with timezone: 2023-12-25T14:30:45+00:00
    "%Y:%m:%d",  # Date only: 2023:12:25
    "%Y-%m-%d",  # Date only: 2023-12-25
    "%Y",  # Year only (for MP3, etc.): 2023
]


def _parse_integer_date(date_int: int) -> datetime.datetime | None:
    """Parse integer YYYYMMDD format (common in YouTube/platform videos)."""
    try:
        date_str = str(date_int)
        if len(date_str) != 8:
            return None
        year = int(date_str[0:4])
        month = int(date_str[4:6])
        day = int(date_str[6:8])
        return datetime.datetime(year, month, day, tzinfo=datetime.UTC)
    except (ValueError, TypeError):
        return None


def _parse_with_patterns(date_str: str) -> datetime.datetime | None:
    """Try parsing date string against common media metadata patterns."""
    for pattern in _DATE_PATTERNS:
        try:
            parsed_date = datetime.datetime.strptime(date_str.strip(), pattern)
            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=datetime.UTC)
            return parsed_date
        except ValueError:
            continue
    return None


def _parse_quicktime_format(date_str: str) -> datetime.datetime | None:
    """Fallback parser for QuickTime format dates."""
    try:
        if ":" not in date_str or len(date_str) < 10:
            return None
        create_date_str = date_str
        if len(create_date_str) <= 19:  # No timezone
            create_date_str = create_date_str.replace(":", "-", 2) + "+00:00"
        return datetime.datetime.fromisoformat(create_date_str)
    except (ValueError, TypeError):
        return None


def _parse_media_date(date_str: str) -> datetime.datetime | None:
    """
    Parse various date formats commonly found in media file metadata.

    Args:
        date_str: Date string from metadata (can also be an integer for YYYYMMDD format)

    Returns:
        Parsed datetime object or None if parsing fails
    """
    if not date_str:
        return None

    # Handle integer YYYYMMDD format
    if isinstance(date_str, int):
        return _parse_integer_date(date_str)

    if not isinstance(date_str, str):
        return None

    # Reject obviously invalid dates
    if date_str.strip() in _INVALID_DATE_PATTERNS:
        return None

    # Try common date patterns
    result = _parse_with_patterns(date_str)
    if result:
        return result

    # Fallback: try QuickTime format
    result = _parse_quicktime_format(date_str)
    if result:
        return result

    raise ValueError(f"Unable to parse date format: {date_str}")


def get_important_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """
    Extract important metadata fields from the full metadata.

    Args:
        metadata: Dictionary of all metadata

    Returns:
        Dictionary of important metadata fields
    """
    field_mappings = {
        # Basic file info
        "FileName": ["File:FileName", "FileName", "SourceFile"],
        "FileSize": ["File:FileSize", "FileSize"],
        "MIMEType": ["File:MIMEType", "MIMEType"],
        "FileType": ["File:FileType", "FileType"],
        "FileTypeExtension": ["File:FileTypeExtension", "FileTypeExtension"],
        # Video specs
        "VideoFormat": ["QuickTime:VideoFormat", "VideoFormat", "Format"],
        "Duration": ["QuickTime:Duration", "Duration", "MediaDuration"],
        "FrameRate": ["QuickTime:FrameRate", "FrameRate"],
        "FrameCount": ["QuickTime:FrameCount", "FrameCount"],
        "VideoFrameRate": ["QuickTime:VideoFrameRate", "VideoFrameRate", "FrameRate"],
        "VideoWidth": [
            "QuickTime:ImageWidth",
            "File:ImageWidth",
            "ImageWidth",
            "Width",
        ],
        "VideoHeight": [
            "QuickTime:ImageHeight",
            "File:ImageHeight",
            "ImageHeight",
            "Height",
        ],
        "AspectRatio": ["QuickTime:AspectRatio", "AspectRatio"],
        "VideoCodec": ["QuickTime:CompressorID", "CompressorID", "VideoCodec", "Codec"],
        # Audio specs
        "AudioFormat": ["QuickTime:AudioFormat", "AudioFormat"],
        "AudioChannels": ["QuickTime:AudioChannels", "AudioChannels"],
        "AudioSampleRate": ["QuickTime:AudioSampleRate", "AudioSampleRate"],
        "AudioBitsPerSample": ["QuickTime:AudioBitsPerSample", "AudioBitsPerSample"],
        # Creation info
        "CreateDate": [
            "QuickTime:ContentCreateDate",  # YouTube and other platform videos
            "QuickTime:CreateDate",
            "CreateDate",
            "DateTimeOriginal",
            "File:FileCreateDate",
            "File:FileModifyDate",
            "XMP:CreateDate",
            "XMP:DateCreated",
            "XMP:DateTimeOriginal",
            "MP4:CreateDate",
            "MP4:CreationDate",
            "Matroska:DateUTC",
            "WebM:DateUTC",
            "AVI:DateTimeOriginal",
            "WMV:DateEncoded",
            "WAV:DateCreated",
            "MP3:Year",
            "FLAC:Date",
            "OGG:Date",
        ],
        "ModifyDate": ["QuickTime:ModifyDate", "ModifyDate", "File:FileModifyDate"],
        "DateTimeOriginal": ["EXIF:DateTimeOriginal", "DateTimeOriginal", "XMP:DateTimeOriginal"],
        # Device info
        "DeviceManufacturer": [
            "QuickTime:Make",
            "EXIF:Make",
            "Make",
            "DeviceManufacturer",
        ],
        "DeviceModel": ["QuickTime:Model", "EXIF:Model", "Model", "DeviceModel"],
        # GPS info
        "GPSLatitude": ["EXIF:GPSLatitude", "GPSLatitude"],
        "GPSLongitude": ["EXIF:GPSLongitude", "GPSLongitude"],
        # Software used
        "Software": ["EXIF:Software", "Software"],
        # Content information
        "Title": ["QuickTime:Title", "Title"],
        "Artist": ["QuickTime:Artist", "Artist"],
        "Author": ["QuickTime:Author", "Author"],
        "Comment": ["QuickTime:Comment", "Comment"],
        "Description": ["QuickTime:Description", "Description"],
        "LongDescription": ["QuickTime:LongDescription", "LongDescription"],
        "HandlerDescription": ["QuickTime:HandlerDescription", "HandlerDescription"],
    }

    important_fields = {}

    # Extract values using the field mappings
    for field_name, possible_keys in field_mappings.items():
        for key in possible_keys:
            if key in metadata and metadata[key] is not None:
                important_fields[field_name] = metadata[key]
                break

    # Look for any additional fields that might be useful
    for key, value in metadata.items():
        if any(term in key.lower() for term in ["creator", "copyright", "language", "genre"]):
            important_fields[key] = value

    return important_fields


def _parse_frame_rate(value: Any) -> float | None:
    """Parse an ffprobe frame rate ("30000/1001", "24/1", "25", …) into a float."""
    if value is None:
        return None
    try:
        if isinstance(value, int | float):
            return float(value)
        text = str(value).strip()
        if "/" in text:
            num, _, den = text.partition("/")
            den_f = float(den)
            if den_f == 0:
                return None
            return float(num) / den_f
        return float(text)
    except (TypeError, ValueError):
        return None


def _run_ffprobe_json(url: str, timeout: int) -> dict[str, Any] | None:
    """Invoke ffprobe against a URL and return parsed JSON (or None)."""
    import shutil

    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        logger.warning("ffprobe binary not found on PATH")
        return None
    try:
        # Security: ffprobe_path resolved via shutil.which; URL is a
        # MinIO-internal presigned URL we generated, not user-supplied.
        proc = subprocess.run(  # noqa: S603 # nosec B603
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                "-timeout",
                str(timeout * 1_000_000),  # ffprobe wants microseconds
                url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.warning(f"ffprobe failed for presigned URL: {e}")
        return None

    if proc.returncode != 0 or not proc.stdout:
        logger.debug(f"ffprobe non-zero exit for URL ({proc.returncode}): {proc.stderr[:200]}")
        return None
    try:
        return json.loads(proc.stdout)  # type: ignore[no-any-return]
    except json.JSONDecodeError as e:
        logger.debug(f"ffprobe JSON decode failed: {e}")
        return None


def _map_ffprobe_format(fmt: dict[str, Any], out: dict[str, Any]) -> None:
    """Copy format-level ffprobe fields into the exiftool-keyed dict."""
    duration = fmt.get("duration")
    if duration is not None:
        with contextlib.suppress(TypeError, ValueError):
            out["Duration"] = float(duration)
    if fmt.get("format_name"):
        out["VideoFormat"] = fmt.get("format_name")
    if fmt.get("format_long_name"):
        out.setdefault("AudioFormat", fmt.get("format_long_name"))


def _map_ffprobe_video_stream(stream: dict[str, Any], out: dict[str, Any]) -> None:
    """Copy video-stream ffprobe fields into the exiftool-keyed dict."""
    if stream.get("width"):
        out["ImageWidth"] = int(stream["width"])
    if stream.get("height"):
        out["ImageHeight"] = int(stream["height"])
    fr = _parse_frame_rate(stream.get("r_frame_rate"))
    if fr is not None:
        out["VideoFrameRate"] = fr
    if stream.get("codec_name"):
        out["VideoCodec"] = stream.get("codec_name")
    if stream.get("nb_frames"):
        with contextlib.suppress(TypeError, ValueError):
            out["FrameCount"] = int(stream["nb_frames"])


def _map_ffprobe_audio_stream(stream: dict[str, Any], out: dict[str, Any]) -> None:
    """Copy audio-stream ffprobe fields into the exiftool-keyed dict."""
    if stream.get("channels") is not None:
        out["AudioChannels"] = int(stream["channels"])
    if stream.get("sample_rate"):
        with contextlib.suppress(TypeError, ValueError):
            out["AudioSampleRate"] = int(stream["sample_rate"])
    if stream.get("bits_per_sample"):
        with contextlib.suppress(TypeError, ValueError):
            bps = int(stream["bits_per_sample"])
            if bps > 0:
                out["AudioBitsPerSample"] = bps
    if not out.get("AudioFormat") and stream.get("codec_name"):
        out["AudioFormat"] = stream.get("codec_name")


def extract_media_metadata_from_url(url: str, timeout: int = 15) -> dict[str, Any] | None:
    """Extract media metadata via ffprobe against a presigned URL.

    ffprobe reads only container headers (≈ first 1 MB for MP4) so this is
    fast and avoids a full re-download of the original media when the
    preprocess task has already streamed audio out via a presigned URL.

    Returns a dict whose keys match the subset of exiftool field names that
    ``update_media_file_metadata`` looks up — so both paths share the same
    downstream code.

    Used in Phase 2 PR #3 to eliminate the duplicate video download on the
    fast preprocess path.
    """
    if not url:
        return None
    data = _run_ffprobe_json(url, timeout)
    if not data:
        return None

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []

    out: dict[str, Any] = {}

    # Stream-level mapping runs BEFORE format-level mapping. The real per-stream
    # audio codec name (e.g. "aac") is strictly more useful than the container's
    # format_long_name (e.g. "QuickTime / MOV"), so it must claim "AudioFormat"
    # first. _map_ffprobe_format only fills the key via setdefault(), so once the
    # codec name is in place the container long-name placeholder is used only as
    # a fallback for audio-less containers.
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video_stream:
        _map_ffprobe_video_stream(video_stream, out)
    if audio_stream:
        _map_ffprobe_audio_stream(audio_stream, out)

    _map_ffprobe_format(fmt, out)

    return out if out else None


def extract_media_metadata(file_path: str) -> dict[str, Any] | None:
    """
    Extract metadata from a media file using ExifTool.

    Args:
        file_path: Path to the media file

    Returns:
        Dictionary containing extracted metadata, or None if extraction failed
    """
    logger.info(f"Extracting metadata with ExifTool from {file_path}")
    extracted_metadata = {}

    # Try to use Python ExifTool library if available
    if "exiftool" in sys.modules:
        try:
            with exiftool.ExifToolHelper() as et:
                metadata_list = et.get_metadata(file_path)
                if metadata_list:
                    extracted_metadata = metadata_list[0]
                    logger.info(f"Successfully extracted {len(extracted_metadata)} metadata fields")
        except Exception as et_err:
            logger.warning(f"Error using Python ExifTool: {et_err}")

    # Fallback to command-line ExifTool if the Python library fails
    if not extracted_metadata:
        try:
            # Security: file_path is validated and originates from internal MinIO storage,
            # not direct user input. The file has been uploaded to our controlled storage
            # system and the path is generated internally by the application.
            exif_process = subprocess.run(  # noqa: S603 - validated MinIO path, not user input
                ["exiftool", "-json", "-n", file_path],  # noqa: S607 # nosec B603 B607
                capture_output=True,
                text=True,
                check=True,
            )

            if exif_process.stdout:
                try:
                    metadata_list = json.loads(exif_process.stdout)
                    if metadata_list:
                        extracted_metadata = metadata_list[0]
                        logger.info(
                            f"Successfully extracted {len(extracted_metadata)} metadata fields via subprocess"
                        )
                except json.JSONDecodeError as jde:
                    logger.warning(f"Error decoding ExifTool JSON output: {jde}")
        except (subprocess.SubprocessError, FileNotFoundError) as sp_err:
            logger.warning(f"Error running ExifTool subprocess: {sp_err}")

    return extracted_metadata if extracted_metadata else None


def _set_video_metadata(media_file, important_metadata: dict[str, Any]) -> None:
    """Set video-specific metadata fields on media_file."""
    media_file.resolution_width = important_metadata.get("VideoWidth") or important_metadata.get(
        "ImageWidth"
    )
    media_file.resolution_height = important_metadata.get("VideoHeight") or important_metadata.get(
        "ImageHeight"
    )
    media_file.frame_rate = important_metadata.get("VideoFrameRate") or important_metadata.get(
        "FrameRate"
    )
    media_file.codec = important_metadata.get("VideoCodec") or important_metadata.get(
        "CompressorID"
    )
    media_file.frame_count = important_metadata.get("FrameCount")
    if important_metadata.get("AspectRatio"):
        media_file.aspect_ratio = important_metadata.get("AspectRatio")


def _set_audio_metadata(media_file, important_metadata: dict[str, Any]) -> None:
    """Set audio-specific metadata fields on media_file."""
    media_file.audio_channels = important_metadata.get("AudioChannels")
    media_file.audio_sample_rate = important_metadata.get("AudioSampleRate")
    media_file.audio_bit_depth = important_metadata.get("AudioBitsPerSample")


def _set_duration(media_file, important_metadata: dict[str, Any]) -> None:
    """Parse and set duration from metadata."""
    duration = important_metadata.get("Duration")
    if not duration:
        return
    try:
        media_file.duration = float(duration)
    except (ValueError, TypeError):
        logger.warning(f"Could not parse duration: {duration}")


def _try_parse_creation_date_from_fields(media_file, important_metadata: dict[str, Any]) -> None:
    """Try to parse creation date from metadata fields."""
    creation_date_fields = ["CreateDate", "DateTimeOriginal", "ModifyDate"]
    for field_name in creation_date_fields:
        field_value = important_metadata.get(field_name)
        if not field_value or media_file.creation_date is not None:
            continue
        try:
            parsed_date = _parse_media_date(field_value)
            if parsed_date:
                media_file.creation_date = parsed_date
                logger.info(f"Successfully parsed creation date from {field_name}: {parsed_date}")
                return
        except (ValueError, TypeError) as e:
            logger.warning(f"Could not parse {field_name}: {field_value} - {e}")


def _set_modification_date(media_file, important_metadata: dict[str, Any]) -> None:
    """Parse and set modification date from metadata."""
    modify_date = important_metadata.get("ModifyDate")
    if not modify_date:
        return
    try:
        media_file.last_modified_date = _parse_media_date(modify_date)
    except (ValueError, TypeError) as e:
        logger.warning(f"Could not parse modification date: {modify_date} - {e}")


def _set_content_info(media_file, important_metadata: dict[str, Any]) -> None:
    """Set device and content information fields."""
    media_file.device_make = important_metadata.get("DeviceManufacturer")
    media_file.device_model = important_metadata.get("DeviceModel")
    media_file.title = important_metadata.get("Title")
    media_file.author = important_metadata.get("Author") or important_metadata.get("Artist")
    media_file.description = (
        important_metadata.get("Description")
        or important_metadata.get("Comment")
        or important_metadata.get("LongDescription")
    )


def update_media_file_metadata(
    media_file, extracted_metadata: dict[str, Any], content_type: str, file_path: str
) -> None:
    """
    Update a MediaFile object with extracted metadata.

    Args:
        media_file: SQLAlchemy MediaFile object
        extracted_metadata: Raw metadata from ExifTool
        content_type: MIME type of the file
        file_path: Path to the file for additional operations. May be empty
            when metadata came from a remote source (e.g. ffprobe against a
            presigned URL) — file-size and mtime fallbacks are skipped in
            that case.
    """
    important_metadata = get_important_metadata(extracted_metadata)

    # Store metadata
    media_file.metadata_raw = extracted_metadata
    media_file.metadata_important = important_metadata

    # Set basic file information. file_path may be empty when we extracted
    # metadata without a local copy; leave the DB value alone in that case.
    if file_path and os.path.exists(file_path):
        media_file.file_size = os.path.getsize(file_path)
    media_file.media_format = important_metadata.get("FileType")

    # Video/image specific metadata
    if content_type.startswith(("video/", "image/")):
        _set_video_metadata(media_file, important_metadata)

    # Audio specific metadata
    if content_type.startswith(("audio/", "video/")):
        _set_audio_metadata(media_file, important_metadata)

    # Duration
    _set_duration(media_file, important_metadata)

    # Creation date — the CONTAINER's claim, and nothing else.
    #
    # This used to end in a two-step fallback: filesystem mtime, then ``upload_time``.
    # Both are gone, and their removal is the point rather than a tidy-up. They recorded
    # no provenance, so a ``creation_date`` copied from ``upload_time`` was
    # indistinguishable from one read out of the file — and every date-scoped answer in
    # the product then treated the upload date as the recording date, confidently. mtime
    # is worse than it looks too: on an uploaded file it is when the *copy* was made.
    #
    # The fallbacks are not lost, they are promoted: "no source knew" is now an explicit
    # ``recorded_date_source`` (``none``), and the chat coverage block says so out loud
    # instead of presenting a substitute as a fact. See
    # ``services/ingest_artifacts/recorded_date.py``. NULL here now means exactly what it
    # says — the container did not tell us.
    _try_parse_creation_date_from_fields(media_file, important_metadata)

    # Modification date
    _set_modification_date(media_file, important_metadata)

    # Device and content information
    _set_content_info(media_file, important_metadata)

    # Store both important and full metadata
    media_file.important_metadata = important_metadata
    media_file.metadata = extracted_metadata
