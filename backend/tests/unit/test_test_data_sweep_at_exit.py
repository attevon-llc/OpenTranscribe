"""The end-of-run test-data sweep (issue #1195): it runs after a run however the run ends,
never changes the run's exit code, and the gate scripts really install it.

``scripts/lib/test-data-sweep.sh`` is SOURCED and RUN here with a stand-in interpreter that
records each sweep invocation, so the EXIT/INT/TERM behaviour is observed rather than read.
The wiring half is static, because the three gate scripts need a live stack to run.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIB = _REPO_ROOT / "scripts" / "lib" / "test-data-sweep.sh"
_BASH = shutil.which("bash") or "/bin/bash"

_RECORDER = """#!/bin/bash
printf '%s\\n' "$*" >> "$SWEEP_LOG"
exit "${FAKE_SWEEP_RC:-0}"
"""


def _harness(tmp_path: Path, body: str, extra_env: dict[str, str] | None = None):
    """Run ``body`` in a bash that sourced the library and armed the exit sweep."""
    recorder = tmp_path / "python"
    recorder.write_text(_RECORDER, encoding="utf-8")
    recorder.chmod(recorder.stat().st_mode | stat.S_IEXEC)
    script = tmp_path / "run.sh"
    script.write_text(
        "set -uo pipefail\n"
        f'source "{_LIB}"\n'
        'extra() { echo "EXTRA-HANDLER-RAN" >> "$SWEEP_LOG"; }\n'
        f'tds_sweep_at_exit "{recorder}" "{_REPO_ROOT}" extra\n'
        f"{body}\n",
        encoding="utf-8",
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"OT_SKIP_TEST_DATA_SWEEP", "OT_TEST_DATA_SWEEP_AT_EXIT"}
    }
    env.update(
        {
            "SWEEP_LOG": str(tmp_path / "sweep.log"),
            "TESTRUN_REGISTRY_DIR": str(tmp_path / ".testruns"),
            **(extra_env or {}),
        }
    )
    return script, env


def _log(tmp_path: Path) -> list[str]:
    log = tmp_path / "sweep.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _sweeps(tmp_path: Path) -> list[str]:
    return [line for line in _log(tmp_path) if "cleanup-test-data.py" in line]


def _run(tmp_path: Path, body: str, extra_env: dict[str, str] | None = None):
    script, env = _harness(tmp_path, body, extra_env)
    return subprocess.run(  # noqa: S603 - fixed argv: bash and a generated script
        [_BASH, str(script)], env=env, capture_output=True, text=True, timeout=60
    )


@pytest.mark.unit
def test_a_passing_run_sweeps_at_exit_and_still_exits_zero(tmp_path: Path) -> None:
    result = _run(tmp_path, "true")
    assert result.returncode == 0
    assert len(_sweeps(tmp_path)) == 1
    assert "--execute-unambiguous" in _sweeps(tmp_path)[0], "Tier A only, never --execute"
    assert "EXTRA-HANDLER-RAN" in _log(tmp_path)


@pytest.mark.unit
def test_a_failing_run_still_sweeps_and_keeps_its_own_exit_code(tmp_path: Path) -> None:
    result = _run(tmp_path, "exit 7")
    assert result.returncode == 7
    assert len(_sweeps(tmp_path)) == 1


@pytest.mark.unit
def test_a_failing_sweep_never_turns_a_green_run_red(tmp_path: Path) -> None:
    result = _run(tmp_path, "true", {"FAKE_SWEEP_RC": "1"})
    assert result.returncode == 0
    assert "the run's own result stands" in result.stderr


@pytest.mark.unit
def test_a_terminated_run_sweeps_before_it_dies(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    script, env = _harness(tmp_path, f'touch "{ready}"\nsleep 30 & wait $!')
    proc = subprocess.Popen(  # noqa: S603 - fixed argv: bash and a generated script
        [_BASH, str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), "the run never reached its body"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
    assert proc.returncode != 0
    assert len(_sweeps(tmp_path)) == 1, "an interrupted run must still sweep"


@pytest.mark.unit
def test_the_escape_hatch_skips_the_sweep_but_not_the_handler(tmp_path: Path) -> None:
    result = _run(tmp_path, "true", {"OT_SKIP_TEST_DATA_SWEEP": "1"})
    assert result.returncode == 0
    assert _sweeps(tmp_path) == []
    assert "EXTRA-HANDLER-RAN" in _log(tmp_path)


@pytest.mark.unit
def test_a_child_of_a_sweeping_wrapper_defers_its_end_sweep(tmp_path: Path) -> None:
    result = _run(tmp_path, "true", {"OT_TEST_DATA_SWEEP_AT_EXIT": "parent"})
    assert result.returncode == 0
    assert _sweeps(tmp_path) == []
    assert "deferred to the wrapping script" in result.stdout


@pytest.mark.unit
def test_the_wrapper_itself_sweeps_once_after_its_children(tmp_path: Path) -> None:
    result = _run(tmp_path, "tds_own_end_sweep_for_children\nbash -c 'echo child'")
    assert result.returncode == 0
    assert len(_sweeps(tmp_path)) == 1


@pytest.mark.unit
def test_a_disarmed_trap_runs_the_handler_without_sweeping(tmp_path: Path) -> None:
    result = _run(tmp_path, "TDS_SWEEP_ENABLED=0\nexit 3")
    assert result.returncode == 3
    assert _sweeps(tmp_path) == []
    assert "EXTRA-HANDLER-RAN" in _log(tmp_path)


@pytest.mark.unit
def test_the_run_ends_its_own_liveness_before_its_end_sweep(tmp_path: Path) -> None:
    """Otherwise the sweep's cutoff is this run's own start and it protects everything the
    run leaked. The recorder checks, at sweep time, that no marker is still held."""
    checker = tmp_path / "python"
    script, env = _harness(tmp_path, "testrun_begin\ntrue")
    checker.write_text(
        "#!/bin/bash\n"
        'printf \'%s\\n\' "$*" >> "$SWEEP_LOG"\n'
        'ls "$TESTRUN_REGISTRY_DIR" | grep -c lock >> "$SWEEP_LOG" || true\n',
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 - fixed argv: bash and a generated script
        [_BASH, str(script)], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0
    lines = _log(tmp_path)
    sweep_at = next(i for i, line in enumerate(lines) if "cleanup-test-data.py" in line)
    assert lines[sweep_at + 1] == "0", f"the run's own marker was still held at sweep: {lines}"


# ---------------------------------------------------------------------------------------------
# Wiring: the three gate scripts install it, and nothing installs a later EXIT trap over it
# ---------------------------------------------------------------------------------------------

_WIRED = {
    "scripts/run-integration-tests.sh": False,
    "scripts/e2e/run-e2e.sh": False,
    "scripts/run-dev-tests.sh": True,  # the wrapper: sweeps once for its children
}


def _top_level_exit_traps_after(source: str, marker: str) -> list[str]:
    after = source.split(marker, 1)[1]
    return [
        line
        for line in after.splitlines()
        if re.match(r"^trap\s", line) and re.search(r"\bEXIT\b", line)
    ]


@pytest.mark.unit
@pytest.mark.parametrize(("path", "is_wrapper"), sorted(_WIRED.items()))
def test_each_gate_script_installs_the_exit_sweep(path: str, is_wrapper: bool) -> None:
    source = (_REPO_ROOT / path).read_text(encoding="utf-8")
    assert "scripts/lib/test-data-sweep.sh" in source
    match = re.search(r"^tds_sweep_at_exit .*$", source, re.MULTILINE)
    assert match, f"{path} never arms the end-of-run sweep"
    assert _top_level_exit_traps_after(source, match.group(0)) == [], (
        f"{path} installs a later EXIT trap, which would silently replace the sweep"
    )
    assert ("tds_own_end_sweep_for_children" in source) is is_wrapper


@pytest.mark.unit
def test_the_wiring_check_fires_on_a_clobbering_trap() -> None:
    clobbered = "tds_sweep_at_exit py root\nrun\ntrap 'rm -f x' EXIT\n"
    assert _top_level_exit_traps_after(clobbered, "tds_sweep_at_exit py root")
    nested = "tds_sweep_at_exit py root\n    bash -c 'trap \"rm -f x\" EXIT'\n"
    assert _top_level_exit_traps_after(nested, "tds_sweep_at_exit py root") == []
