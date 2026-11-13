"""Rehearsal phases print their UTC start and `duration_s` (LOOP_SPEED_PLAN M1), and the scenario
library names the right compose project.

The REAL `phase`/`phase_done`/`phase_check` definitions are extracted from each scenario script and
run with a no-op phase body, so a rewrite that drops the clock fails here.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
TESTS = REPO / "scripts" / "release-tests"
SCENARIOS = ("test-fresh-install.sh", "test-lite-mode.sh", "test-upgrade.sh")


def _definitions(script: str) -> str:
    text = (TESTS / script).read_text(encoding="utf-8")
    start = text.index("phase_done()")
    end = text.index("\n              }\n", start) + len("\n              }\n")
    return text[start:end]


@pytest.mark.parametrize("script", SCENARIOS)
def test_a_phase_prints_its_start_time_and_duration(script: str, tmp_path: Path) -> None:
    body = f"""
set -euo pipefail
PHASE_DIR="{tmp_path}/phases"; DO_FORCE=0
{_definitions(script)}
phase 07 true
"""
    proc = subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert re.search(r"═══ phase 07 ═══.*\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", proc.stdout), proc.stdout
    assert re.search(r"phase 07 done duration_s=\d+", proc.stdout), proc.stdout


@pytest.mark.parametrize(
    ("scenario", "project"),
    [
        ("lite-mode", "ot-reltest-lite"),
        ("upgrade", "opentranscribe"),
        ("fresh-install", "opentranscribe"),
    ],
)
def test_each_scenario_resolves_through_its_own_compose_project(
    scenario: str, project: str
) -> None:
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'TEST_SCENARIO={scenario}; TEST_PROJECT_NAME=ot-reltest-lite; REPO_ROOT="{REPO}"; '
            f'source "{TESTS}/lib/scenario-targeting.sh"; echo "$GR_SCENARIO_PROJECT"',
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.stdout.strip() == project, proc.stderr
