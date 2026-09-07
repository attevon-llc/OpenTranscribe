"""Shared timestamp formatting utilities.

Provides canonical implementations for timestamp formatting used across
the application (transcript segments, file endpoints, subtitle generation).
"""


def _split_hms(seconds: float) -> tuple[int, int, float]:
    """Break ``seconds`` into ``(hours, minutes, remaining seconds)``.

    The ONE place the hour-carrying arithmetic lives. Both
    :func:`format_timestamp_simple` and :func:`format_timestamp_with_tenths` build
    their string from this rather than re-deriving hours/minutes themselves —
    that duplication is exactly how ``formatting_service.format_duration_with_millis``
    kept rendering ``65:30.0`` instead of ``1:05:30.0`` after
    ``formatting_service.format_duration`` was fixed to carry hours (B7): the two
    functions had their own copies of the same arithmetic, and only one copy got
    fixed. A caller wanting sub-second precision keeps ``remaining seconds`` as a
    float; one truncating it to ``int`` (as ``format_timestamp_simple`` does) is a
    display choice made at the call site, not here.

    Args:
        seconds: Time value in seconds.

    Returns:
        ``(hours, minutes, remaining_seconds)`` — ``remaining_seconds`` is a float
        in ``[0, 60)``, ``minutes`` an int in ``[0, 60)``, ``hours`` an int ``>= 0``.
    """
    total_minutes = int(seconds // 60)
    remaining_seconds = seconds % 60
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return hours, minutes, remaining_seconds


def format_timestamp_simple(seconds: float) -> str:
    """Format seconds as MM:SS or H:MM:SS for display.

    Args:
        seconds: Time value in seconds.

    Returns:
        Formatted timestamp string (e.g. "3:45" or "1:03:45").
    """
    hours, minutes, remaining_seconds = _split_hms(seconds)
    secs = int(remaining_seconds)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_timestamp_with_tenths(seconds: float) -> str:
    """Format seconds as MM:SS.f or H:MM:SS.f — ``format_timestamp_simple`` plus
    one decimal place of sub-second precision, sharing its hour-carrying via
    :func:`_split_hms` so the two can never drift apart the way
    ``formatting_service``'s pre-fix duration formatters did (see ``_split_hms``).

    Args:
        seconds: Time value in seconds.

    Returns:
        Formatted timestamp string (e.g. "0:45.2" or "1:05:30.0").
    """
    hours, minutes, remaining_seconds = _split_hms(seconds)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining_seconds:04.1f}"
    return f"{minutes}:{remaining_seconds:04.1f}"


def format_srt_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS,mmm for SRT subtitles.

    Args:
        seconds: Time value in seconds.

    Returns:
        Formatted SRT timestamp string (e.g. "00:03:45,123").
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    milliseconds = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"
