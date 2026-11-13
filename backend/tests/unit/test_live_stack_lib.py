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


def _harness(
    tmp_path: Path, *, live: bool = True, stays_up: bool = False, workdir: str | None = None
):
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
        f'if [[ "$1" == ps && ( ! -e {stopped} || -n "{int(stays_up) or ""}" ) ]]; then\n'
        f'  if [[ "$*" == *working_dir* ]]; then echo "transcribe-app|{workdir or root}"; '
        "else echo live-backend; fi\nfi\nexit 0\n"
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


def _marker(root: Path) -> Path:
    return root / ".release" / "v1" / "stack-stopped-by-release"


def test_the_marker_records_the_project_and_directory_that_were_stopped(tmp_path):
    proc, _, root = _run(
        tmp_path,
        "lsl_stop_stack && RELEASE_DEFER_STACK_RESTORE=1 lsl_restore_after_stage v1 true",
        workdir="/elsewhere/checkout",
    )
    text = _marker(root).read_text()
    assert "project=transcribe-app" in text and "working_dir=/elsewhere/checkout" in text, text


def test_restore_from_a_different_checkout_refuses_and_names_the_right_directory(tmp_path):
    body = (
        "lsl_stop_stack && RELEASE_DEFER_STACK_RESTORE=1 lsl_restore_after_stage v1 true\n"
        "lsl_restore_if_marked v1"
    )
    proc, calls, root = _run(tmp_path, body, workdir="/elsewhere/checkout")
    assert calls == ["opentr stop"], f"must not start dev from the wrong checkout: {calls}"
    assert "cd /elsewhere/checkout && ./opentr.sh start dev" in proc.stderr
    assert _marker(root).is_file(), "the marker must survive so the right checkout can restore"


def test_restore_from_the_recorded_checkout_still_starts(tmp_path):
    root = tmp_path / "repo"
    body = (
        "lsl_stop_stack && RELEASE_DEFER_STACK_RESTORE=1 lsl_restore_after_stage v1 true\n"
        "lsl_restore_if_marked v1"
    )
    proc, calls, _ = _run(tmp_path, body, workdir=str(root))
    assert calls == ["opentr stop", "opentr start dev"], calls
    assert not _marker(root).exists()


def test_live_stack_names_include_a_stack_started_from_this_checkout(tmp_path):
    root, bindir, log = _harness(tmp_path)
    _run(tmp_path, "lsl_live_stack_names >/dev/null")
    asked = log.read_text()
    assert f"label=com.docker.compose.project.working_dir={root}" in asked, asked
