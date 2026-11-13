"""--compare-verdicts and the parallel judge (EFFA, design 2a items 9-11)."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_LIB = _ROOT / "scripts" / "lib"


def _log(path: Path, verdicts: dict[str, str], complete: bool = True) -> str:
    lines = ["mutmut noise", "=== OT-MUTATION-SUMMARY BEGIN key=spans path=x.py ==="]
    lines += [f"{n}: {v}" for n, v in verdicts.items()]
    if complete:
        lines.append(
            "=== OT-MUTATION-SUMMARY END key=spans mutants=2 survived=1 not_checked=0 run_mutants=2 run_survived=1 ==="
        )
    path.write_text("\n".join(lines) + "\n")
    return str(path)


@pytest.fixture
def iso(monkeypatch):
    monkeypatch.syspath_prepend(str(_LIB))
    sys.modules.pop("mutation_isolation", None)
    return importlib.import_module("mutation_isolation")


BASE = {"app.x.x_a__mutmut_1": "killed", "app.x.x_a__mutmut_2": "survived"}


def test_identical_logs_exit_0(iso, tmp_path):
    a, b = _log(tmp_path / "a", BASE), _log(tmp_path / "b", BASE)
    assert iso.compare(a, b)[0] == 0


def test_a_flipped_verdict_exits_1_and_names_the_mutant(iso, tmp_path):
    a = _log(tmp_path / "a", BASE)
    b = _log(tmp_path / "b", {**BASE, "app.x.x_a__mutmut_2": "killed"})
    rc, msg = iso.compare(a, b)
    assert rc == 1 and "app.x.x_a__mutmut_2: survived != killed" in msg


def test_a_log_without_the_end_marker_is_not_measured(iso, tmp_path):
    a, b = _log(tmp_path / "a", BASE), _log(tmp_path / "b", BASE, complete=False)
    assert iso.compare(a, b)[0] == 4


def test_the_shell_flag_reaches_the_same_comparison(tmp_path):
    a, b = (
        _log(tmp_path / "a", BASE),
        _log(tmp_path / "b", {**BASE, "app.x.x_a__mutmut_1": "survived"}),
    )
    r = subprocess.run(
        [str(_ROOT / "scripts" / "run-mutation-tests.sh"), "--compare-verdicts", a, b],
        capture_output=True, text=True, timeout=60, env={"PATH": "/usr/bin:/bin", "OT_MUTATION_OUT_DIR": str(tmp_path)},
    )  # fmt: skip
    assert r.returncode == 1, r.stderr


def test_judge_does_not_pass_survivors_that_fell_below_baseline(iso, tmp_path):
    log = _log(tmp_path / "a", {"app.x.x_a__mutmut_1": "killed", "app.x.x_a__mutmut_2": "killed"})
    rc, msg = iso.judge("spans", log, 5, accept_improved=False)
    assert rc == 4 and "IMPROVED UNDER --parallel" in msg
    assert iso.judge("spans", log, 5, accept_improved=True)[0] == 0
    assert iso.judge("spans", log, 0, accept_improved=False)[0] == 0


def _empty_log(path: Path, body: str) -> str:
    path.write_text(
        body
        + "\n=== OT-MUTATION-SUMMARY BEGIN key=lockout path=x.py ===\n"
        + "=== OT-MUTATION-SUMMARY END key=lockout mutants=0 survived=0 not_checked=0 run_mutants=? run_survived=? ===\n"
    )
    return str(path)


STATS_FAIL = (
    "____________ test_lockout_threshold_locks_on_the_configured_attempt ____________\n"
    "mutmut.runners.harness.BadTestExecutionCommandsException: Failed to run pytest"
)


def test_zero_mutants_is_a_failure_naming_module_and_test_not_an_improvement(iso, tmp_path):
    log = _empty_log(tmp_path / "a", STATS_FAIL)
    for accept in (False, True):
        rc, msg = iso.judge("lockout", log, 38, accept_improved=accept)
        assert rc == 1, msg
        assert "lockout" in msg and "test_lockout_threshold_locks_on_the_configured_attempt" in msg
        assert "IMPROVED" not in msg


def test_zero_mutants_without_stats_text_still_fails(iso, tmp_path):
    rc, msg = iso.judge(
        "lockout", _empty_log(tmp_path / "a", "nothing useful"), 38, accept_improved=True
    )
    assert rc == 1 and "lockout" in msg


def test_stats_failure_with_mutants_present_still_fails(iso, tmp_path):
    log = _log(tmp_path / "a", BASE)
    p = Path(log)
    p.write_text("failed to collect stats\n" + p.read_text())
    assert iso.judge("spans", log, 5, accept_improved=True)[0] == 1


def test_a_healthy_log_stays_clean(iso, tmp_path):
    assert iso.judge("spans", _log(tmp_path / "a", BASE), 1, accept_improved=False)[0] == 0
