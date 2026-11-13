"""`test-matrix.sh pre-release` runs exactly the legs `release.sh` does not repeat (R8).

Dry-run only: no leg executes. The union check keeps the selector honest in both directions,
so a new matrix leg is either part of the pre-release run or consciously covered by a release stage.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
MATRIX = REPO / "scripts" / "test-matrix.sh"

# Legs whose command `release.sh run` already executes: preflight/verify (stage 1), `test` (the
# backend gate, whose superset 2a is), `rehearse` (3) and `scan` (4).
RELEASE_COVERED = {"1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "2a", "3", "4"}


def _dry(*args: str) -> str:
    proc = subprocess.run(
        [str(MATRIX), "--dry-run", *args], cwd=REPO, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stderr


def _leg_ids() -> list[str]:
    block = re.search(r"^LEGS=\(\n(.*?)^\)", MATRIX.read_text(), re.DOTALL | re.MULTILINE)
    assert block
    return [m.group(1) for m in re.finditer(r'^\s*"([^|"]+)\|', block.group(1), re.MULTILINE)]


def _pre_release_ids() -> list[str]:
    return re.findall(r"^\[dry-run\] (\S+) ", _dry("pre-release"), re.MULTILINE)


def test_pre_release_plus_release_stages_cover_every_leg():
    pre, covered = set(_pre_release_ids()), RELEASE_COVERED
    uncovered = set(_leg_ids()) - pre - covered
    assert not uncovered, f"legs that neither pre-release nor a release stage runs: {uncovered}"


def test_pre_release_repeats_nothing_release_sh_runs():
    assert not set(_pre_release_ids()) & RELEASE_COVERED


def test_pre_release_runs_the_overlay_auth_and_hardware_legs():
    assert _pre_release_ids() == ["2a-overlays", "2a-auth", "2b", "2c", "2d", "3-pki"]


def test_pre_legs_are_not_run_again_by_stage_2_or_all():
    """CONTROL: stage `pre` keeps the extra legs out of the stage loops (2a already ran them)."""
    for arg in ("2", "all"):
        out = _dry(arg)
        assert "2a-overlays" not in out and "2a-auth" not in out, arg
        assert re.search(r"^\[dry-run\] 2a ", out, re.MULTILINE), arg
