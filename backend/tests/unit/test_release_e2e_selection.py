"""The release `test` stage runs the v0.6.0 browser journeys (#1282 item A).

The stage used to run only the 5-file smoke set, so the journeys v0.6.0 shipped were never part
of any release verdict. These tests pin the wiring statically: the selection script names real
files, covers the named journeys, and the stage records its own criterion for it.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SELECTION = REPO_ROOT / "scripts" / "e2e" / "run-e2e-release.sh"
STAGE = REPO_ROOT / "scripts" / "release" / "60-test.sh"
CRITERIA = REPO_ROOT / "scripts" / "release" / "release-criteria.yaml"

REQUIRED_JOURNEYS = {
    "test_about_dialog.py",
    "test_export_downloads.py",
    "test_plyr_playback.py",
    "test_rtl_privacy.py",
    "test_upload_tray_and_notifications.py",
    "test_notification_missed_frames.py",
    "test_privacy_settings.py",
    "test_privacy_switch_keyboard.py",
    "test_privacy_voiceprint_hiding.py",
}


def _selected() -> list[str]:
    return re.findall(r"backend/tests/e2e/(test_\w+\.py)", SELECTION.read_text(encoding="utf-8"))


def test_selection_covers_every_named_journey() -> None:
    assert set(_selected()) >= REQUIRED_JOURNEYS


def test_every_selected_file_exists() -> None:
    missing = [f for f in _selected() if not (REPO_ROOT / "backend/tests/e2e" / f).is_file()]
    assert not missing, f"release e2e selection names files that do not exist: {missing}"


def test_stage_runs_the_selection_and_records_its_own_criterion() -> None:
    text = STAGE.read_text(encoding="utf-8")
    assert "scripts/e2e/run-e2e-release.sh" in text
    assert re.search(r"record release-e2e-journeys (pass|fail)", text)


def test_criterion_is_declared_blocking_for_the_test_stage() -> None:
    data = yaml.safe_load(CRITERIA.read_text(encoding="utf-8"))
    criteria = {c["id"]: c for c in data["stages"]["test"]["criteria"]}
    assert criteria["release-e2e-journeys"]["severity"] == "blocking"
