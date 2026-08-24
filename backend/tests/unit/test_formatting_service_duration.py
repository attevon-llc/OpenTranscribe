"""B7: ``format_duration`` must roll over to hours past 3600 seconds.

It used to format MM:SS unconditionally (never carrying into hours), so a
7500-second recording rendered "125:00" on every gallery card instead of
"2:05:00" — reaching the UI unmodified since the frontend deliberately does
not re-format formatted display fields.
"""

from __future__ import annotations

from app.services.formatting_service import FormattingService


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
