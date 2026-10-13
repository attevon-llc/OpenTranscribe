import re
import unicodedata
from pathlib import Path

# C0/C1 controls (CR, LF, NUL, ESC, DEL, NEL ...) and bidirectional formatting
# characters. They survive the ASCII filter below when they are C0/DEL, and they let a
# name smuggle header lines or visually spoof its extension, so they are removed from
# the whole name before it is split into stem and extension.
_CONTROL_AND_BIDI_RE = re.compile(
    "[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)


def sanitize_filename(filename: str, max_length: int = 255) -> str:
    """Sanitize a filename to be safe for storage and processing.

    This function normalizes Unicode characters, replaces problematic characters
    with safe alternatives, ensures proper length limits, preserves file extensions,
    and handles edge cases like empty names.

    Args:
        filename: Original filename to sanitize.
        max_length: Maximum length for the sanitized filename. Defaults to 255.

    Returns:
        str: Sanitized filename that is safe for storage and processing.

    Examples:
        >>> sanitize_filename("My File—With Issues.txt")
        "My File-With Issues.txt"
        >>> sanitize_filename("")
        "unnamed_file"
    """
    if not filename:
        return "unnamed_file"

    filename = _CONTROL_AND_BIDI_RE.sub("", filename)
    if not filename:
        return "unnamed_file"

    # Split filename into name and extension
    path = Path(filename)
    name = path.stem
    extension = path.suffix

    # Normalize Unicode characters (NFD form to separate accents from base characters)
    name = unicodedata.normalize("NFD", name)
    extension = unicodedata.normalize("NFD", extension)

    # Replace problematic Unicode characters with ASCII equivalents
    # Convert em-dash (—) to regular dash (-)
    name = name.replace("\u2014", "-")  # Em dash
    name = name.replace("\u2013", "-")  # En dash
    name = name.replace("\u2019", "'")  # Right single quotation mark
    name = name.replace("\u2018", "'")  # Left single quotation mark
    name = name.replace("\u201d", '"')  # Right double quotation mark
    name = name.replace("\u201c", '"')  # Left double quotation mark
    name = name.replace("\u2026", "...")  # Horizontal ellipsis

    # Remove non-ASCII characters (keep only ASCII)
    name = "".join(char for char in name if ord(char) < 128)
    extension = "".join(char for char in extension if ord(char) < 128)

    # Replace problematic characters with underscores
    # Keep alphanumeric, hyphens, underscores, spaces, dots, and apostrophes
    name = re.sub(r"[^\w\s\-.']", "_", name)

    # Replace multiple consecutive spaces/underscores with single ones
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"_+", "_", name)

    # Remove leading/trailing spaces and underscores
    name = name.strip(" _.")

    # Handle edge cases
    if not name:
        name = "unnamed_file"

    # Ensure the full filename (name + extension) doesn't exceed max_length
    full_name = name + extension
    if len(full_name) > max_length:
        # Trim the name part to fit within the limit, preserving the extension
        available_length = max_length - len(extension)
        if available_length > 0:
            name = name[:available_length].rstrip(" _.")
        else:
            # Extension is too long, just use a generic name
            name = "file"
            extension = extension[: max_length - len(name)]

    return name + extension


def get_safe_storage_filename(original_filename: str, user_id: int, file_id: int) -> str:
    """Generate a safe filename for storage that includes the original sanitized filename.

    Creates a hierarchical storage path with user and file identifiers while
    preserving the original filename in a sanitized form.

    Args:
        original_filename: The original filename from the upload.
        user_id: The user ID for path organization.
        file_id: The unique file ID for path organization.

    Returns:
        str: Safe storage filename in the format: user_{user_id}/file_{file_id}/{sanitized_filename}

    Example:
        >>> get_safe_storage_filename("My Video.mp4", 123, 456)
        "user_123/file_456/My Video.mp4"
    """
    sanitized_filename = sanitize_filename(original_filename)
    return f"user_{user_id}/file_{file_id}/{sanitized_filename}"
