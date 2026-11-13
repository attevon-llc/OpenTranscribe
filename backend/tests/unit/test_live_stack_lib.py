"""scripts/release/live-stack-lib.sh: the one stop/restore implementation shared by rehearse,
smoke and release.sh's exit path.

Executes the real library against a fake `docker` and a fake `opentr.sh` that log every call.
What is NOT covered: a real stack stopping and starting (needs Docker).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "scripts" / "release" / "live-stack-lib.sh"


def _harness(tmp_path: Path, *, live: bool = True, stays_up: bool = False):
    root = tmp_path / "repo"
    (root / ".release" / "v1").mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    log.touch()
    stopped = tmp_path / "stopped"
    if not live:
        stopped.touch()
    # docker ps lists a container until opentr.sh stop ran (or forever when stays_up).
    (bindir / "docker").write_text(
        '#!/bin/bash\necho "docker $*" >> "$FAKE_LOG"\n'
        f'if [[ "$1" == ps && ( ! -e {stopped} || -n "{int(stays_up) or ""}" ) ]]; then echo live-backend; fi\nexit 0\n'
    )
    (root / "opentr.sh").write_text(
        f'#!/bin/bash\necho "opentr $*" >> "$FAKE_LOG"\n[[ "$1" == stop ]] && touch {stopped}\n'
        f'[[ "$1" == start ]] && rm -f {stopped}\nexit 0\n'
    )
    for f in (bindir / "docker", root / "opentr.sh"):
        f.chmod(0o755)
    return root, bindir, log


def _run(tmp_path, body, env=None, **kw):
    root, bindir, log = _harness(tmp_path, **kw)
    script = f'set -uo pipefail\nREPO_ROOT="{root}"\nsource "{LIB}"\n{body}\n'
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_LOG": str(log), **(env or {})},
        timeout=30,
    )
    calls = [c for c in log.read_text().splitlines() if c.startswith("opentr")]
    return proc, calls, root


def test_stop_then_restore_stops_once_and_starts_once(tmp_path):
    proc, calls, root = _run(
        tmp_path,
        'lsl_stop_stack && stopped=true; lsl_restore_after_stage v1 "$stopped"',
    )
    assert proc.returncode == 0, proc.stderr
    assert calls == ["opentr stop", "opentr start dev"], calls


def test_a_stack_the_operator_stopped_by_hand_is_never_started(tmp_path):
    """THE property: no marker and not stopped by us means no start."""
    proc, calls, _ = _run(tmp_path, "lsl_restore_after_stage v1 false", live=False)
    assert proc.returncode == 0
    assert calls == [], calls


def test_deferred_restore_leaves_it_down_and_writes_the_marker(tmp_path):
    proc, calls, root = _run(
        tmp_path,
        "RELEASE_DEFER_STACK_RESTORE=1 lsl_restore_after_stage v1 true",
    )
    assert calls == [], "a deferred restore must not start anything"
    assert (root / ".release" / "v1" / "stack-stopped-by-release").is_file()


def test_the_marker_makes_a_later_stage_restore_exactly_once(tmp_path):
    body = (
        "RELEASE_DEFER_STACK_RESTORE=1 lsl_restore_after_stage v1 true\n"
        "lsl_restore_if_marked v1\nlsl_restore_if_marked v1"
    )
    proc, calls, root = _run(tmp_path, body)
    assert calls == ["opentr start dev"], calls
    assert not (root / ".release" / "v1" / "stack-stopped-by-release").exists()


def test_exit_path_without_a_marker_restores_nothing(tmp_path):
    """CONTROL for the release.sh early-exit restore."""
    proc, calls, _ = _run(tmp_path, "lsl_restore_if_marked v1", live=False)
    assert calls == []


def test_a_survivor_after_stop_is_a_precondition_failure_with_its_names(tmp_path):
    proc, calls, _ = _run(
        tmp_path,
        'lsl_stop_stack; rc=$?; echo "rc=$rc reason=$LSL_FAIL_REASON fix=$LSL_FAIL_FIX"',
        stays_up=True,
    )
    assert "rc=3" in proc.stdout
    assert "survived" in proc.stdout and "live-backend" in proc.stdout


def test_no_approval_means_no_stop_unattended(tmp_path):
    proc, calls, _ = _run(tmp_path, 'lsl_approved_to_stop "x" "the test" && echo YES || echo NO')
    assert proc.stdout.strip() == "NO"
    for i, env in enumerate(({"ASSUME_YES": "true"}, {"RELEASE_AUTO_STOP_STACK": "true"})):
        proc, _, _ = _run(
            tmp_path / f"again{i}", 'lsl_approved_to_stop "x" "t" && echo YES || echo NO', env=env
        )
        assert proc.stdout.strip() == "YES", env


def test_smoke_and_rehearse_share_the_one_implementation():
    """No second copy of the stop dance: both stages call the lib, neither calls opentr stop."""
    for name in ("65-rehearse.sh", "85-smoke.sh"):
        text = (REPO / "scripts" / "release" / name).read_text()
        assert "lsl_stop_stack" in text and "lsl_approved_to_stop" in text, name
        code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        assert not [ln for ln in code if "opentr.sh stop >" in ln], name
