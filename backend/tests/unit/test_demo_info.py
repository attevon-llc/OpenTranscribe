"""The reset and upgrade times a public demo shows its visitors (#628, follow-up P-B)."""

from __future__ import annotations

import json
import re
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from app.core.demo_info import DEMO_RESET_HOUR_UTC
from app.core.demo_info import last_upgrade_at
from app.core.demo_info import next_reset_at

TIMER = Path(__file__).resolve().parents[3] / "scripts/demo/systemd/opentranscribe-demo-reset.timer"


def test_the_constant_is_the_hour_the_systemd_timer_fires():
    match = re.search(r"^OnCalendar=\S+ (\d\d):00:00 UTC$", TIMER.read_text(), re.MULTILINE)

    assert match is not None
    assert int(match.group(1)) == DEMO_RESET_HOUR_UTC


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 8, 3, 59, 59, tzinfo=UTC), datetime(2026, 11, 8, 4, 0, tzinfo=UTC)),
        (datetime(2026, 11, 8, 4, 0, 0, tzinfo=UTC), datetime(2026, 11, 9, 4, 0, tzinfo=UTC)),
        (datetime(2026, 11, 8, 23, 0, tzinfo=UTC), datetime(2026, 11, 9, 4, 0, tzinfo=UTC)),
        (datetime(2026, 12, 31, 5, 0, tzinfo=UTC), datetime(2027, 1, 1, 4, 0, tzinfo=UTC)),
    ],
)
def test_next_reset_is_the_next_four_oclock_utc(now, expected):
    assert next_reset_at(now) == expected


def _status(tmp_path, body: str) -> str:
    path = tmp_path / "demo-status.json"
    path.write_text(body)
    return str(path)


def test_an_upgrade_time_is_reported_from_a_successful_status(tmp_path):
    path = _status(
        tmp_path,
        json.dumps({"tag": "v0.7.0", "result": "upgraded", "at": "2026-11-08T10:15:00+00:00"}),
    )

    assert last_upgrade_at(path) == datetime(2026, 11, 8, 10, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"result": "rolled_back", "at": "2026-11-08T10:15:00+00:00"}),
        json.dumps({"result": "upgraded"}),
        json.dumps({"result": "upgraded", "at": "not a date"}),
        json.dumps(["not", "an", "object"]),
        "{}",
        "",
        "{broken",
    ],
)
def test_nothing_is_claimed_unless_the_host_recorded_an_upgrade(tmp_path, body):
    assert last_upgrade_at(_status(tmp_path, body)) is None


def test_an_absent_or_unset_status_file_reports_nothing(tmp_path):
    assert last_upgrade_at("") is None
    assert last_upgrade_at(str(tmp_path / "missing.json")) is None
