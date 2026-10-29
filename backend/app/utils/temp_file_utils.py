"""Temporary file utilities for audio/media processing tasks."""

import logging
import os
import shutil
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from typing import BinaryIO

logger = logging.getLogger(__name__)


def download_to_temp_file(file_data: BinaryIO, suffix: str = "") -> str:
    """Download file data to a temporary file and return its path.

    Args:
        file_data: Binary file-like object to read from.
        suffix: File extension suffix (e.g. ".wav").

    Returns:
        Path to the temporary file.

    Raises:
        Exception: Re-raises any error after cleaning up the temp file.
    """
    temp_fd, temp_file_path = tempfile.mkstemp(suffix=suffix)
    try:
        os.close(temp_fd)
        with open(temp_file_path, "wb") as f:
            # Chunked copy: media objects can be gigabytes, so never read() them whole.
            shutil.copyfileobj(file_data, f, length=1024 * 1024)
        return temp_file_path
    except Exception:
        cleanup_temp_file(temp_file_path)
        raise


def cleanup_temp_file(temp_file_path: str | None) -> None:
    """Safely remove a temporary file if it exists.

    Args:
        temp_file_path: Path to the file to remove, or None (no-op).
    """
    if temp_file_path and os.path.exists(temp_file_path):
        try:
            os.unlink(temp_file_path)
        except OSError as e:
            logger.warning(f"Failed to clean up temp file {temp_file_path}: {e}")


@contextmanager
def temp_file_context(file_data: BinaryIO, suffix: str = "") -> Generator[str, None, None]:
    """Context manager that provides a temp file path and auto-cleans up.

    Args:
        file_data: Binary file-like object to read from.
        suffix: File extension suffix (e.g. ".wav").

    Yields:
        Path to the temporary file.
    """
    temp_path = download_to_temp_file(file_data, suffix=suffix)
    try:
        yield temp_path
    finally:
        cleanup_temp_file(temp_path)
