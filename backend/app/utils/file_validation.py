"""
File validation utilities for secure upload handling.

This module provides magic byte (file signature) validation to ensure
uploaded files match their declared MIME types, preventing disguised
malicious file uploads.
"""

import logging
from typing import BinaryIO

logger = logging.getLogger(__name__)

# Magic byte signatures for audio/video formats
# Format: (offset, signature_bytes, mime_types)
# Some formats share signatures (e.g., MP4/MOV/M4A all use 'ftyp')
MAGIC_SIGNATURES: list[tuple[int, bytes, list[str]]] = [
    # MP4, MOV, M4A, M4V - all use ISO Base Media File Format
    # 'ftyp' appears at offset 4, preceded by box size (4 bytes)
    (4, b"ftyp", ["video/mp4", "video/quicktime", "audio/mp4", "audio/x-m4a", "video/x-m4v"]),
    # WebM/Matroska - EBML header
    (0, b"\x1a\x45\xdf\xa3", ["video/webm", "audio/webm", "video/x-matroska", "audio/x-matroska"]),
    # OGG container (Vorbis, Opus, Theora)
    (0, b"OggS", ["audio/ogg", "video/ogg", "application/ogg", "audio/opus"]),
    # RIFF container (WAV, AVI)
    (0, b"RIFF", ["audio/wav", "audio/x-wav", "audio/wave", "video/x-msvideo", "video/avi"]),
    # MP3 with ID3v2 tag
    (0, b"ID3", ["audio/mpeg", "audio/mp3"]),
    # MP3 frame sync (no ID3 tag) - 0xFF followed by 0xFB, 0xFA, or 0xF3
    (0, b"\xff\xfb", ["audio/mpeg", "audio/mp3"]),
    (0, b"\xff\xfa", ["audio/mpeg", "audio/mp3"]),
    (0, b"\xff\xf3", ["audio/mpeg", "audio/mp3"]),
    (0, b"\xff\xf2", ["audio/mpeg", "audio/mp3"]),
    # Raw AAC in an ADTS stream (*.aac): 12-bit sync word, then MPEG-4 (F1) or MPEG-2 (F9)
    (0, b"\xff\xf1", ["audio/aac", "audio/x-aac", "audio/aacp"]),
    (0, b"\xff\xf9", ["audio/aac", "audio/x-aac", "audio/aacp"]),
    # FLAC
    (0, b"fLaC", ["audio/flac", "audio/x-flac"]),
    # AIFF
    (0, b"FORM", ["audio/aiff", "audio/x-aiff"]),
    # ASF/WMA/WMV - Microsoft Advanced Systems Format
    (
        0,
        b"\x30\x26\xb2\x75\x8e\x66\xcf\x11",
        ["audio/x-ms-wma", "video/x-ms-wmv", "video/x-ms-asf"],
    ),
    # MPEG Transport Stream
    (0, b"\x47", ["video/mp2t", "video/MP2T"]),
    # MPEG Program Stream
    (0, b"\x00\x00\x01\xba", ["video/mpeg", "video/x-mpeg"]),
    # 3GP/3G2
    (4, b"ftyp3g", ["video/3gpp", "video/3gpp2", "audio/3gpp", "audio/3gpp2"]),
    # MKV specifically (after EBML header, doctype)
    # Note: Already covered by WebM/Matroska above
]

# Minimum bytes needed to read for validation
MIN_HEADER_SIZE = 32


def get_magic_bytes(file_content: bytes | BinaryIO, size: int = MIN_HEADER_SIZE) -> bytes:
    """
    Get the first N bytes from file content for magic byte checking.

    Args:
        file_content: File content as bytes or file-like object
        size: Number of bytes to read (default: 32)

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


def validate_magic_bytes(header: bytes, declared_mime_type: str) -> tuple[bool, str]:
    """
    Validate that file header magic bytes match the declared MIME type.

    Args:
        header: First bytes of the file (at least MIN_HEADER_SIZE)
        declared_mime_type: MIME type declared by the client

    Returns:
        Tuple of (is_valid, detected_type_or_error_message)
    """
    if len(header) < 4:
        return False, "File too small to validate"

    # Normalize MIME type (handle variations)
    normalized_mime = declared_mime_type.lower().strip()

    # Check each signature
    for offset, signature, valid_mimes in MAGIC_SIGNATURES:
        if len(header) > offset and header[offset : offset + len(signature)] == signature:
            # Found a matching signature
            # Check if declared MIME type is compatible
            if any(normalized_mime.startswith(m.split("/")[0]) for m in valid_mimes):
                # Type category matches (audio/* or video/*)
                return True, valid_mimes[0]
            if normalized_mime in valid_mimes:
                return True, normalized_mime

            # Signature found but MIME type doesn't match
            logger.warning(
                f"Magic byte mismatch: declared={declared_mime_type}, "
                f"detected={valid_mimes[0]}, signature={signature!r}"
            )
            # Allow if it's still audio/video (might be a browser quirk)
            if normalized_mime.startswith(("audio/", "video/")):
                return True, valid_mimes[0]
            return False, f"File signature indicates {valid_mimes[0]}, not {declared_mime_type}"

    # No known signature matched - reject for security
    # This prevents uploading disguised files (e.g., executables renamed to .mp4)
    logger.warning(
        f"Rejected file with unknown signature for declared type {declared_mime_type}: "
        f"header={header[:16].hex()}"
    )
    return False, (
        "This file doesn't appear to be a valid audio/video file. "
        "Please ensure you're uploading a supported media format (MP4, WebM, MP3, WAV, etc.)."
    )


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
        is_valid, result = validate_magic_bytes(header, declared_mime_type)

        if is_valid:
            logger.debug(f"File validation passed: {filename or 'unknown'} ({result})")
        else:
            logger.warning(f"File validation failed: {filename or 'unknown'} - {result}")

        return is_valid, result

    except Exception as e:
        logger.error(f"Error validating file {filename or 'unknown'}: {e}")
        # On error, fail closed (reject the file)
        return False, f"Validation error: {str(e)}"
