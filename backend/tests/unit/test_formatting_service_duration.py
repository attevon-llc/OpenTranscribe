"""B7: ``format_duration`` must roll over to hours past 3600 seconds.

It used to format MM:SS unconditionally (never carrying into hours), so a
7500-second recording rendered "125:00" on every gallery card instead of
"2:05:00" — reaching the UI unmodified since the frontend deliberately does
not re-format formatted display fields.

B7 follow-up: ``format_duration_with_millis`` had the identical bug and was not
touched by the first fix — it duplicated the non-hour-carrying arithmetic
rather than sharing it, so a segment more than an hour into a recording
rendered "65:30.0" via ``formatted_timestamp``/``display_timestamp``
(``GET /files/{uuid}`` and ``.../segments``, feeding
``TranscriptSegmentList.svelte``) while the edit path
(``files/crud.py``/``transcript_segments.py``, both via
``format_timestamp_simple``) rendered the same instant "1:05:30" — editing a
segment past the one-hour mark visibly changed its own timestamp's format.
"""

from __future__ import annotations

from app.services.formatting_service import FormattingService
from app.utils.time_format import format_timestamp_simple


def test_format_duration_rolls_over_past_one_hour():
    assert FormattingService.format_duration(3599) == "59:59"
    assert FormattingService.format_duration(3600) == "1:00:00"
    assert FormattingService.format_duration(7500) == "2:05:00"
    assert FormattingService.format_duration(36000) == "10:00:00"


def test_format_duration_under_an_hour_and_edge_guards():
    """Control: sub-hour formatting and the None/<=0 guard are unchanged."""
    assert FormattingService.format_duration(599) == "9:59"
    assert FormattingService.format_duration(0) is None
    assert FormattingService.format_duration(-5) is None
    assert FormattingService.format_duration(None) is None


def test_format_duration_with_millis_rolls_over_past_one_hour():
    """The B7 follow-up, on the millis-carrying formatter specifically.

    3930.0s = 1h05m30s: the pre-fix arithmetic rendered "65:30.0".
    """
    assert FormattingService.format_duration_with_millis(3930.0) == "1:05:30.0"
    assert FormattingService.format_duration_with_millis(3599.95) == "59:59.9"
    assert FormattingService.format_duration_with_millis(3600.0) == "1:00:00.0"
    assert FormattingService.format_duration_with_millis(36000.25) == "10:00:00.2"


def test_format_duration_with_millis_under_an_hour_and_edge_guards():
    """Control: sub-hour formatting, sub-second precision and the guards are unchanged."""
    assert FormattingService.format_duration_with_millis(45.2) == "0:45.2"
    assert FormattingService.format_duration_with_millis(0) == "0:00.0"
    assert FormattingService.format_duration_with_millis(-5) is None
    assert FormattingService.format_duration_with_millis(None) is None


def test_the_view_path_and_the_edit_path_agree_past_one_hour():
    """Closes the divergence: editing a segment past 1h must not change its own
    timestamp's rendered FORMAT (H:MM:SS vs MM:SS), only the value.

    ``format_duration_with_millis`` (the view path, ``formatted_timestamp``/
    ``display_timestamp``) and ``format_timestamp_simple`` (the edit path,
    ``files/crud.py``/``transcript_segments.py``) must render the same
    hour-carrying shape for an identical instant, modulo the millis suffix the
    view path alone carries.
    """
    seconds = 3930.0  # 1h05m30s
    view = FormattingService.format_duration_with_millis(seconds)
    edit = format_timestamp_simple(seconds)
    assert view == f"{edit}.0"
