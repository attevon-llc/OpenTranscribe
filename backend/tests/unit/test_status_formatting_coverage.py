"""Every FileStatus must format to something a user can read (issue #301).

`FileStatus` is deliberately NOT a `StrEnum`, so `str(FileStatus.QUARANTINED)` is
`"FileStatus.QUARANTINED"`. `format_status` fell back on `str(status)`, and `QUARANTINED`
was missing from its map — so a quarantined file's `display_status` reached the gallery
and file-detail UI as the literal text "FileStatus.QUARANTINED", with an unstyled
`status-unknown` badge to match.
"""

from __future__ import annotations

import pytest

from app.core.enums import FileStatus
from app.services.formatting_service import FormattingService


@pytest.mark.parametrize("status", list(FileStatus))
def test_every_status_has_a_human_readable_display(status: FileStatus):
    display = FormattingService.format_status(status)

    assert display, f"{status} formatted to an empty string"
    assert "FileStatus" not in display, (
        f"{status} leaked its repr to the UI as {display!r} — add it to format_status's map"
    )
    assert "_" not in display, f"{status} formatted to the raw enum value {display!r}"
    assert display[0].isupper(), f"{status} formatted to {display!r}, expected title case"


@pytest.mark.parametrize("status", list(FileStatus))
def test_every_status_has_a_styled_badge_class(status: FileStatus):
    badge = FormattingService.get_status_badge_class(status.value)

    assert badge != "status-unknown", (
        f"{status} falls through to status-unknown — add it to get_status_badge_class"
    )
    assert badge.startswith("status-")


def test_known_status_labels():
    assert FormattingService.format_status(FileStatus.QUARANTINED) == "Quarantined"
    assert FormattingService.format_status(FileStatus.ORPHANED) == "Needs Recovery"
    assert FormattingService.format_status(FileStatus.DOWNLOADING) == "Downloading"
    assert FormattingService.get_status_badge_class("quarantined") == "status-quarantined"


def test_unmapped_status_falls_back_to_the_value_not_the_repr():
    """A status added to the enum but not the map must still render readably."""
    assert FormattingService.format_status("some_new_state") == "Some New State"  # type: ignore[arg-type]
