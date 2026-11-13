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
    "test_media_playback.py",
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
    assert re.search(r"^\s*\./scripts/run-dev-tests\.sh --e2e-release", text, re.M), (
        "the journeys must run through the overlay orchestrator: the chat journey needs mock-llm"
    )
    assert not re.search(r"^\s*\./scripts/e2e/run-e2e-release\.sh", text, re.M)
    dev = (REPO_ROOT / "scripts" / "run-dev-tests.sh").read_text(encoding="utf-8")
    assert "--e2e-release)" in dev
    assert 'scripts/e2e/run-e2e-release.sh"' in dev
    assert re.search(r"record release-e2e-journeys (pass|fail)", text)


def test_criterion_is_declared_blocking_for_the_test_stage() -> None:
    data = yaml.safe_load(CRITERIA.read_text(encoding="utf-8"))
    criteria = {c["id"]: c for c in data["stages"]["test"]["criteria"]}
    assert criteria["release-e2e-journeys"]["severity"] == "blocking"


def test_stage_runs_the_first_run_wizard_e2e_before_anything_dismisses_the_wizard() -> None:
    text = STAGE.read_text(encoding="utf-8")
    wizard = text.index("scripts/e2e/run-e2e-first-run.sh")
    assert re.search(r"record first-run-wizard-e2e (pass|fail)", text)
    assert wizard < text.index("run-dev-tests.sh"), "the wizard test must run before the gate"
    assert wizard < text.index("fresh_stack_complete_first_run"), "complete it only afterwards"
    assert "FRESH_STACK_DEFER_FIRST_RUN=1" in text


def test_the_fresh_stack_defers_wizard_completion_on_request() -> None:
    lib = (REPO_ROOT / "scripts" / "release" / "fresh-stack-lib.sh").read_text(encoding="utf-8")
    assert re.search(
        r'FRESH_STACK_DEFER_FIRST_RUN:-0\}" == "1" \]\] \|\| (fresh_stack_step "[^"]+" )?fresh_stack_complete',
        lib,
    )


def test_the_wizard_criterion_is_declared_as_blocking() -> None:
    declared = yaml.safe_load(CRITERIA.read_text(encoding="utf-8"))["stages"]["test"]["criteria"]
    entry = next(c for c in declared if c["id"] == "first-run-wizard-e2e")
    assert entry["severity"] == "blocking"
