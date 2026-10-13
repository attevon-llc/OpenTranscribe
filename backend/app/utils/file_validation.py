"""
File validation utilities for secure upload handling.

This module provides magic byte (file signature) validation to ensure
uploaded files match their declared MIME types, preventing disguised
malicious file uploads.
"""

import logging
from typing import BinaryIO

logger = logging.getLogger(__name__)

# Bytes needed to identify every container below. MPEG transport streams are only
# recognised by a repeating 0x47 sync byte (188-byte packets, or 192 for M2TS), so the
# header must reach the second packet's sync byte.
MIN_HEADER_SIZE = 256
HEADER_READ_SIZE = MIN_HEADER_SIZE

# Container family -> (MIME types a client may declare, MIME returned on success).
_FAMILIES: dict[str, tuple[frozenset[str], str]] = {
    "isobmff": (
        frozenset(
            {
                "video/mp4",
                "video/quicktime",
                "audio/mp4",
                "audio/x-m4a",
                "audio/m4a",
                "audio/mp4a-latm",
                "video/x-m4v",
                "video/3gpp",
                "video/3gpp2",
                "audio/3gpp",
                "audio/3gpp2",
                "audio/aac",
                "audio/x-aac",
            }
        ),
        "video/mp4",
    ),
    "matroska": (
        frozenset(
            {"video/webm", "audio/webm", "video/x-matroska", "audio/x-matroska", "video/matroska"}
        ),
        "video/webm",
    ),
    "ogg": (
        frozenset({"audio/ogg", "video/ogg", "application/ogg", "audio/opus"}),
        "audio/ogg",
    ),
    "wav": (
        frozenset({"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave", "audio/x-pn-wav"}),
        "audio/wav",
    ),
    "avi": (
        frozenset({"video/x-msvideo", "video/avi", "video/vnd.avi", "video/msvideo"}),
        "video/x-msvideo",
    ),
    "mpeg_audio": (
        frozenset({"audio/mpeg", "audio/mp3", "audio/x-mp3", "audio/mpeg3", "audio/x-mpeg"}),
        "audio/mpeg",
    ),
    "aac": (frozenset({"audio/aac", "audio/x-aac", "audio/aacp"}), "audio/aac"),
    # An ID3v2 tag may front MP3, AAC or FLAC; the container behind it is not checked.
    "id3": (
        frozenset(
            {
                "audio/mpeg",
                "audio/mp3",
                "audio/x-mp3",
                "audio/mpeg3",
                "audio/x-mpeg",
                "audio/aac",
                "audio/x-aac",
                "audio/flac",
                "audio/x-flac",
            }
        ),
        "audio/mpeg",
    ),
    "flac": (frozenset({"audio/flac", "audio/x-flac"}), "audio/flac"),
    "aiff": (frozenset({"audio/aiff", "audio/x-aiff"}), "audio/aiff"),
    "asf": (
        frozenset({"audio/x-ms-wma", "video/x-ms-wmv", "video/x-ms-asf", "audio/x-ms-asf"}),
        "audio/x-ms-wma",
    ),
    "ts": (frozenset({"video/mp2t"}), "video/mp2t"),
    "ps": (frozenset({"video/mpeg", "video/x-mpeg"}), "video/mpeg"),
    "ac3": (frozenset({"audio/ac3", "audio/x-ac3", "audio/vnd.dolby.dd-raw"}), "audio/ac3"),
    "flv": (frozenset({"video/x-flv", "video/flv"}), "video/x-flv"),
    "amr": (frozenset({"audio/amr", "audio/amr-wb", "audio/3gpp"}), "audio/amr"),
}

# Filename extension -> container families it may legitimately hold. An extension that
# is absent here (or a name with none) cannot contradict the content and is not checked.
_EXTENSION_FAMILIES: dict[str, frozenset[str]] = {
    ext: frozenset(fams)
    for exts, fams in (
        ((".mp4", ".m4v", ".mov", ".f4v"), ("isobmff",)),
        ((".m4a", ".m4b", ".3gp", ".3g2", ".3ga"), ("isobmff",)),
        ((".webm", ".mkv", ".mka", ".weba"), ("matroska",)),
        ((".ogg", ".oga", ".ogv", ".opus", ".spx"), ("ogg",)),
        ((".wav", ".wave"), ("wav",)),
        ((".avi",), ("avi",)),
        ((".mp3", ".mp2", ".mpga", ".mpa"), ("mpeg_audio", "id3")),
        ((".aac",), ("aac", "id3", "isobmff")),
        ((".flac",), ("flac", "id3")),
        ((".aif", ".aiff", ".aifc"), ("aiff",)),
        ((".wma", ".wmv", ".asf"), ("asf",)),
        ((".ts", ".mts", ".m2ts"), ("ts",)),
        ((".mpg", ".mpeg", ".vob"), ("ps",)),
        ((".ac3",), ("ac3",)),
        ((".flv",), ("flv",)),
        ((".amr", ".awb"), ("amr",)),
    )
    for ext in exts
}

# ftyp major brands of still images in the same box format (HEIF/AVIF). They share the
# container with MP4 but are not audio or video.
_IMAGE_FTYP_BRANDS = frozenset(
    {b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis", b"jpeg", b"jpgs"}
)

# MPEG audio frame sync: 0xFF then 111 + version + layer bits. Layer III is MP3,
# layer II is MP2. ADTS (AAC) is the layer-00 variants.
_MPEG_AUDIO_SECOND_BYTES = frozenset({0xF2, 0xF3, 0xFA, 0xFB, 0xF4, 0xF5, 0xFC, 0xFD})
_ADTS_SECOND_BYTES = frozenset({0xF1, 0xF9})


# Fixed multi-byte prefixes at offset 0.
_PREFIX_FAMILIES: tuple[tuple[bytes, str], ...] = (
    (b"\x1a\x45\xdf\xa3", "matroska"),
    (b"OggS", "ogg"),
    (b"ID3", "id3"),
    (b"fLaC", "flac"),
    (b"\x30\x26\xb2\x75\x8e\x66\xcf\x11", "asf"),
    (b"\x00\x00\x01\xba", "ps"),
    (b"\x0b\x77", "ac3"),
    (b"FLV\x01", "flv"),
    (b"#!AMR", "amr"),
)


def _detect_riff(header: bytes) -> str | None:
    # The form type at offset 8 separates audio/video from WebP images.
    return {b"WAVE": "wav", b"AVI ": "avi"}.get(header[8:12])


def _detect_sync_byte(header: bytes) -> str | None:
    if header[0] == 0xFF and len(header) >= 2:
        if header[1] in _MPEG_AUDIO_SECOND_BYTES:
            return "mpeg_audio"
        if header[1] in _ADTS_SECOND_BYTES:
            return "aac"
    # A lone 0x47 proves nothing: transport streams repeat it every 188 bytes (192 for M2TS).
    if len(header) > 188 and header[0] == 0x47 and header[188] == 0x47:
        return "ts"
    if len(header) > 196 and header[4] == 0x47 and header[196] == 0x47:
        return "ts"
    return None


def detect_container(header: bytes) -> str | None:
    """Identify the container family from a file's leading bytes.

    Every signature is at least two bytes, and formats that only have a one-byte
    marker (transport streams) must repeat it at the packet boundary. RIFF is split by
    its form type so a WebP image cannot pass as a WAV.
    """
    if header[4:8] == b"ftyp" and len(header) >= 12:
        return None if header[8:12] in _IMAGE_FTYP_BRANDS else "isobmff"
    if header[:4] == b"RIFF":
        return _detect_riff(header)
    if header[:4] == b"FORM":
        return "aiff" if header[8:12] in (b"AIFF", b"AIFC") else None
    for prefix, family in _PREFIX_FAMILIES:
        if header.startswith(prefix):
            return family
    return _detect_sync_byte(header) if header else None


def get_magic_bytes(file_content: bytes | BinaryIO, size: int = HEADER_READ_SIZE) -> bytes:
    """
    Get the first N bytes from file content for magic byte checking.

    Args:
        file_content: File content as bytes or file-like object
        size: Number of bytes to read (default: HEADER_READ_SIZE)

    Returns:
        First N bytes of the file
    """
    if isinstance(file_content, bytes):
        return file_content[:size]

    # File-like object - read and seek back
    current_pos = file_content.tell()
    file_content.seek(0)
    header = file_content.read(size)
    file_content.seek(current_pos)
    return header


def _extension(filename: str | None) -> str:
    if not filename:
        return ""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return ("." + base.rsplit(".", 1)[-1].lower()) if "." in base else ""


def validate_magic_bytes(
    header: bytes, declared_mime_type: str, filename: str | None = None
) -> tuple[bool, str]:
    """
    Validate that the file header names a known audio/video container that agrees
    with both the declared MIME type and (when it is a known media one) the extension.

    Args:
        header: First bytes of the file (HEADER_READ_SIZE for transport streams)
        declared_mime_type: MIME type declared by the client
        filename: Optional filename whose extension must agree with the container

    Returns:
        Tuple of (is_valid, detected_type_or_error_message)
    """
    if len(header) < 4:
        return False, "File too small to validate"

    # Parameters ("audio/webm;codecs=opus") do not change the type.
    declared = declared_mime_type.split(";", 1)[0].lower().strip()

    family = detect_container(header)
    if family is None:
        logger.warning(
            f"Rejected file with unknown signature for declared type {declared_mime_type}: "
            f"header={header[:16].hex()}"
        )
        return False, (
            "This file doesn't appear to be a valid audio/video file. "
            "Please ensure you're uploading a supported media format (MP4, WebM, MP3, WAV, etc.)."
        )

    mimes, result = _FAMILIES[family]
    if declared not in mimes:
        logger.warning(
            f"Container mismatch: declared={declared_mime_type}, detected={family}, "
            f"header={header[:16].hex()}"
        )
        return (
            False,
            f"File contents ({family}) do not match the declared type {declared_mime_type}.",
        )

    ext_families = _EXTENSION_FAMILIES.get(_extension(filename))
    if ext_families is not None and family not in ext_families:
        logger.warning(f"Extension mismatch: name={filename!r}, detected={family}")
        return False, f"File contents ({family}) do not match the file extension."

    return True, result


def validate_uploaded_file(
    file_content: bytes | BinaryIO,
    declared_mime_type: str | None,
    filename: str | None = None,
) -> tuple[bool, str]:
    """
    Validate an uploaded file's magic bytes against its declared MIME type.

    This is a security measure to prevent uploading disguised files
    (e.g., an executable renamed to .mp4).

    Args:
        file_content: File content as bytes or file-like object
        declared_mime_type: MIME type from upload request
        filename: Optional filename for logging

    Returns:
        Tuple of (is_valid, message)
        - If valid: (True, detected_mime_type)
        - If invalid: (False, error_message)
    """
    if not declared_mime_type:
        return False, "No MIME type provided"

    # Only validate audio/video files
    if not declared_mime_type.lower().startswith(("audio/", "video/")):
        return False, f"Invalid file type: {declared_mime_type}. Only audio/video allowed."

    try:
        header = get_magic_bytes(file_content)
        is_valid, result = validate_magic_bytes(header, declared_mime_type, filename)

        if is_valid:
            logger.debug(f"File validation passed: {filename or 'unknown'} ({result})")
        else:
            logger.warning(f"File validation failed: {filename or 'unknown'} - {result}")

        return is_valid, result

    except Exception as e:
        logger.error(f"Error validating file {filename or 'unknown'}: {e}")
        # On error, fail closed (reject the file)
        return False, f"Validation error: {str(e)}"
