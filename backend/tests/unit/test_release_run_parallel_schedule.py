"""`release.sh run` schedules `test` beside `build -> scan`, and ledgers durations.

Drives the REAL scripts/release.sh copied into a scratch tree beside stub stage scripts that
sleep and log start/end times, so the schedule is observed, not read from source. Nothing here
builds an image or starts a stack: the stubs stand in for the 45-minute stages.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"

STAGE_FILES = {
    "preflight": "10-preflight.sh",
    "bump": "20-bump.sh",
    "verify": "30-verify.sh",
    "test": "60-test.sh",
    "build": "40-build.sh",
    "scan": "50-scan.sh",
    "rehearse": "65-rehearse.sh",
    "tag": "70-tag.sh",
    "publish": "80-publish.sh",
    "smoke": "85-smoke.sh",
    "promote": "90-promote.sh",
    "finish": "95-finish.sh",
}

# Each stub logs "<stage> start|end <epoch-ms>" and honours STUB_SLEEP_<STAGE> / STUB_RC_<STAGE>.
STUB = """#!/bin/bash
stage="{stage}"
up="$(echo "$stage" | tr a-z A-Z)"
ms() {{ date +%s%3N; }}
if [[ "${{1:-}}" == "--cold-cache-only" ]]; then echo "cold start $(ms)" >> "$STUB_LOG"; sleep "${{STUB_SLEEP_COLD:-0.2}}"; echo "cold end $(ms)" >> "$STUB_LOG"; exit "${{STUB_RC_COLD:-0}}"; fi
echo "$stage start $(ms)" >> "$STUB_LOG"
sleep_var="STUB_SLEEP_$up"; rc_var="STUB_RC_$up"
sleep "${{!sleep_var:-0.1}}"
echo "$stage end $(ms)" >> "$STUB_LOG"
exit "${{!rc_var:-0}}"
"""


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts" / "release").mkdir(parents=True)
    (root / "scripts" / "release-tests" / "lib").mkdir(parents=True)
    shutil.copy(SCRIPTS / "release.sh", root / "scripts" / "release.sh")
    shutil.copy(
        SCRIPTS / "release-tests" / "lib" / "versions.sh", root / "scripts/release-tests/lib"
    )
    for lib in ("patch-lib.sh", "live-stack-lib.sh"):
        shutil.copy(SCRIPTS / "release" / lib, root / "scripts" / "release" / lib)
    for stage, fname in STAGE_FILES.items():
        path = root / "scripts" / "release" / fname
        path.write_text(STUB.format(stage=stage))
        path.chmod(0o755)
    (root / "scripts" / "release.sh").chmod(0o755)
    return root


def _run(tree: Path, *args: str, env: dict[str, str] | None = None):
    log = tree.parent / "stub.log"
    log.write_text("")
    proc = subprocess.run(
        [str(tree / "scripts" / "release.sh"), "run", "1.2.3", *args],
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tree.parent),
            "USER": "tester",
            "STUB_LOG": str(log),
            **(env or {}),
        },
    )
    events: dict[str, int] = {}
    order: list[str] = []
    for line in log.read_text().splitlines():
        name, edge, ms = line.split()
        events[f"{name}.{edge}"] = int(ms)
        order.append(f"{name}.{edge}")
    return proc, events, order


def test_test_overlaps_build_and_scan_and_rehearse_waits_for_both(tree: Path):
    proc, ev, _ = _run(
        tree,
        "--only",
        "test,build,scan,rehearse",
        env={"STUB_SLEEP_TEST": "1.5", "STUB_SLEEP_BUILD": "0.3", "STUB_SLEEP_SCAN": "0.3"},
    )
    assert proc.returncode == 0, proc.stderr
    assert ev["test.start"] < ev["build.end"], "test must start before build finishes (overlap)"
    assert ev["scan.start"] >= ev["build.end"], "scan must still follow build"
    assert ev["scan.end"] < ev["test.end"], "scan finished while the slow test was still running"
    assert ev["rehearse.start"] >= ev["test.end"], "rehearse starts only after BOTH branches"
    assert ev["rehearse.start"] >= ev["scan.end"]


def test_serial_flag_keeps_the_old_strict_order(tree: Path):
    proc, ev, _ = _run(tree, "--serial", "--only", "test,build,scan")
    assert proc.returncode == 0, proc.stderr
    assert ev["test.end"] <= ev["build.start"] <= ev["build.end"] <= ev["scan.start"]


def test_a_failing_test_still_fails_the_run_after_build_and_scan_finished(tree: Path):
    proc, ev, _ = _run(
        tree,
        "--only",
        "test,build,scan,rehearse",
        env={"STUB_RC_TEST": "1", "STUB_SLEEP_TEST": "0.2", "STUB_SLEEP_BUILD": "0.6"},
    )
    assert proc.returncode == 1, proc.stderr
    assert "scan.end" in ev, "build->scan must run to completion beside a failing test"
    assert "rehearse.start" not in ev, "rehearse must not start after a failed branch"
    steps = tree / ".release" / "v1.2.3" / "steps"
    assert "status=failed" in (steps / "test").read_text()
    assert "status=done" in (steps / "scan").read_text()


def test_every_failure_is_reported_not_only_the_first(tree: Path):
    proc, _, _ = _run(
        tree, "--only", "test,build,scan", env={"STUB_RC_TEST": "1", "STUB_RC_BUILD": "1"}
    )
    assert proc.returncode == 1
    assert "test failed" in proc.stderr and "build failed" in proc.stderr
    assert "scan" not in "".join(
        p.name for p in (tree / ".release" / "v1.2.3" / "steps").iterdir() if p.name == "scan"
    ), "scan must not run after build failed"


def test_without_test_nothing_overlaps(tree: Path):
    """CONTROL: the overlap engages only when test AND build are both selected."""
    _, ev, order = _run(tree, "--only", "build,scan")
    assert order == ["build.start", "build.end", "scan.start", "scan.end"], order
    assert "test.start" not in ev


def test_scenario_d_starts_after_build_beside_scan_only_when_rehearse_runs(tree: Path):
    _, ev, _ = _run(
        tree,
        "--only",
        "test,build,scan,rehearse",
        env={"STUB_SLEEP_SCAN": "0.6", "STUB_SLEEP_COLD": "0.3"},
    )
    assert ev["cold.start"] >= ev["build.end"]
    assert ev["cold.start"] < ev["scan.end"], "D must run beside scan"
    assert ev["rehearse.start"] >= ev["cold.end"]
    # CONTROL: no rehearse selected -> D is not started early.
    _, ev2, _ = _run(tree, "--only", "test,build,scan")
    assert "cold.start" not in ev2


def test_ledger_records_start_and_duration_and_status_stays_first(tree: Path):
    proc, _, _ = _run(tree, "--serial", "--only", "build", env={"STUB_SLEEP_BUILD": "1.1"})
    assert proc.returncode == 0, proc.stderr
    body = (tree / ".release" / "v1.2.3" / "steps" / "build").read_text()
    lines = dict(line.split("=", 1) for line in body.splitlines())
    assert lines["status"] == "done"
    assert body.splitlines()[0] == "status=done", "readers use grep -m1 '^status='"
    assert lines["started"].endswith("Z")
    assert int(lines["duration_s"]) >= 1


def test_skipped_stage_has_no_duration(tree: Path):
    """CONTROL: a stage that never ran must not claim a duration."""
    _run(tree, "--skip", "scan", "--only", "build,scan")
    body = (tree / ".release" / "v1.2.3" / "steps" / "scan").read_text()
    assert "duration_s" not in body and "started=" not in body
