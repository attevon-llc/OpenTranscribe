"""The first-run wizard e2e (#1366) stays out of the shared phases and out of the completion fixture."""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
E2E = REPO_ROOT / "backend" / "tests" / "e2e"
RUNNER = REPO_ROOT / "scripts" / "e2e" / "run-e2e.sh"
FIRST_RUN_RUNNER = REPO_ROOT / "scripts" / "e2e" / "run-e2e-first-run.sh"
TEST = E2E / "test_first_run_wizard_terms.py"


def test_the_shared_phase_excludes_the_marker() -> None:
    phase1 = re.search(r'-m "([^"]*not visual[^"]*)"', RUNNER.read_text(encoding="utf-8"))
    assert phase1, "run-e2e.sh phase 1 marker expression not found"
    assert "not first_run" in phase1.group(1)


def test_the_dedicated_runner_selects_the_marker_and_the_file() -> None:
    text = FIRST_RUN_RUNNER.read_text(encoding="utf-8")
    assert "backend/tests/e2e/test_first_run_wizard_terms.py" in text
    assert "-m first_run" in text


def test_the_test_never_requests_or_calls_the_wizard_completion() -> None:
    tree = ast.parse(TEST.read_text(encoding="utf-8"))
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.arg):
            used.add(node.arg)
        elif isinstance(node, ast.ImportFrom | ast.Import):
            used.update(alias.name for alias in node.names)
    assert not used & {"complete_first_run_wizard", "first_run_wizard_completed"}
    # ...while the guard that fails if anything else requests the fixture is present.
    assert "first_run_wizard_completed" in TEST.read_text(encoding="utf-8")
