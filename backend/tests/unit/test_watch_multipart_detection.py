"""Unit tests for watch-source multi-part detection (pure, GPU-free, no deps)."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.models.watch_source import DEFAULT_MULTIPART_REGEX
from app.services.watch_sources import multipart
from app.services.watch_sources.base import RemoteFileInfo


def _fi(name: str, hours_offset: int = 0) -> RemoteFileInfo:
    return RemoteFileInfo(
        path=f"/x/{name}",
        name=name,
        size=1000,
        modified_time=datetime(2026, 6, 1, tzinfo=UTC) + timedelta(hours=hours_offset),
    )


def test_parse_part_default_regex():
    assert multipart.parse_part(DEFAULT_MULTIPART_REGEX, "meeting_P001.mp4") == (
        "meeting",
        1,
        ".mp4",
    )
    assert multipart.parse_part(DEFAULT_MULTIPART_REGEX, "plain.mp4") is None


def test_parse_part_invalid_regex_raises():
    import pytest

    with pytest.raises(ValueError):
        multipart.parse_part("([", "x_P001.mp4")


def test_detect_groups_complete():
    files = [_fi("rec_P001.mp4"), _fi("rec_P002.mp4", 1), _fi("solo.mp4")]
    groups, standalone = multipart.detect_groups(files, DEFAULT_MULTIPART_REGEX, 24)
    assert len(groups) == 1
    g = groups[0]
    assert g.base_name == "rec"
    assert g.max_part == 2
    assert g.is_complete is True
    assert [fi.name for fi in g.ordered_files] == ["rec_P001.mp4", "rec_P002.mp4"]
    assert [fi.name for fi in standalone] == ["solo.mp4"]


def test_detect_groups_gap_marks_incomplete():
    files = [_fi("rec_P001.mp4"), _fi("rec_P003.mp4", 1)]
    groups, _ = multipart.detect_groups(files, DEFAULT_MULTIPART_REGEX, 24)
    assert len(groups) == 1
    assert groups[0].is_complete is False  # missing P002


def test_detect_groups_lone_part_is_standalone():
    files = [_fi("rec_P001.mp4")]
    groups, standalone = multipart.detect_groups(files, DEFAULT_MULTIPART_REGEX, 24)
    assert groups == []
    assert [fi.name for fi in standalone] == ["rec_P001.mp4"]


def test_detect_groups_time_window_splits_sessions():
    # Two parts 48h apart with a 24h window → treated as separate sessions.
    files = [_fi("rec_P001.mp4"), _fi("rec_P002.mp4", 48)]
    groups, standalone = multipart.detect_groups(files, DEFAULT_MULTIPART_REGEX, 24)
    assert groups == []
    assert len(standalone) == 2


def test_generate_stitched_filename():
    assert multipart.generate_stitched_filename("meeting", ".mp4") == "meeting.mp4"
