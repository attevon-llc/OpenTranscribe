"""`65-rehearse.sh` must WAIVE its three scenarios under `--patch`, and must run
them in full otherwise (issue #784).

`release.sh`'s `patch_prepare()` resolves the waiver once and exports
`OT_PATCH_SKIP_REASON`; `65-rehearse.sh` only reads it. This drives the REAL
`65-rehearse.sh`, copied into a scratch `scripts/release/` tree alongside the
real `criteria-lib.sh` (never a re-implementation of either), with the three
scenario scripts replaced by fast stubs and `docker` faked on `PATH` so the
`live-stack-stopped` check is deterministic regardless of whether this host's
own dev stack happens to be up.

Two cases, and the second is the one that actually guards the first:

(a) must-fire — `OT_PATCH_SKIP_REASON` set: none of the three scenario scripts
    may run, all three criteria record `not-measured` with `"severity":"warn"`
    in `--json`, and the stage still exits 0.
(b) must-stay-clean control — unset: the invocation lines must be REACHED (all
    three scenario stubs actually invoked). Without this control, a waiver
    predicate that had silently become always-true would still pass (a).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
REHEARSE_SH = REPO_ROOT / "scripts" / "release" / "65-rehearse.sh"
CRITERIA_LIB_SH = REPO_ROOT / "scripts" / "release" / "criteria-lib.sh"

pytestmark = pytest.mark.skipif(
    not REHEARSE_SH.exists() or not CRITERIA_LIB_SH.exists(),
    reason="scripts/release/{65-rehearse,criteria-lib}.sh missing",
)

_CRITERIA_YAML = """
version: 2
stages:
  rehearse:
    criteria:
      - id: live-stack-stopped
        severity: blocking
      - id: fresh-install
        severity: blocking
      - id: upgrade-from-previous
        severity: blocking
      - id: lite-mode
        severity: blocking
"""

_FAKE_DOCKER = """#!/bin/bash
# Only `ps`/`ps -a --filter ...` calls happen in 65-rehearse.sh; both just need
# to report "no matching containers" so live-stack-stopped is deterministic
# regardless of whether THIS host's own dev stack happens to be up right now.
if [ "$1" = "ps" ]; then
  exit 0
fi
exit 0
"""


def _stub_scenario(path: Path, marker: Path, rc: int = 0) -> None:
    path.write_text(f'#!/bin/bash\necho "invoked $0 $*" >> {marker}\nexit {rc}\n')
    path.chmod(0o755)


@pytest.fixture
def scratch_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A scratch REPO_ROOT with the REAL 65-rehearse.sh + criteria-lib.sh, stub
    scenarios, and a fake `docker` prepended to PATH. Returns (repo, marker_dir).
    """
    repo = tmp_path / "repo"
    release_dir = repo / "scripts" / "release"
    tests_dir = repo / "scripts" / "release-tests"
    release_dir.mkdir(parents=True)
    tests_dir.mkdir(parents=True)

    shutil.copy(REHEARSE_SH, release_dir / "65-rehearse.sh")
    (release_dir / "65-rehearse.sh").chmod(0o755)
    shutil.copy(CRITERIA_LIB_SH, release_dir / "criteria-lib.sh")
    (release_dir / "release-criteria.yaml").write_text(_CRITERIA_YAML)

    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    for name in ("test-fresh-install.sh", "test-upgrade.sh", "test-lite-mode.sh"):
        _stub_scenario(tests_dir / name, marker_dir / f"{name}.log")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(_FAKE_DOCKER)
    fake_docker.chmod(0o755)

    return repo, marker_dir


def _run_rehearse(
    repo: Path, *, patch_skip_reason: str | None, json_out: bool = False
) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{repo.parent / 'bin'}:/usr/bin:/bin",
        "RELEASE_VERSION": "v0.5.1",
        "JSON_OUT": "true" if json_out else "false",
    }
    if patch_skip_reason is not None:
        env["OT_PATCH_SKIP_REASON"] = patch_skip_reason
    return subprocess.run(
        [str(repo / "scripts" / "release" / "65-rehearse.sh"), "v0.5.1"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _criteria_by_id(stdout_line: str) -> dict[str, dict]:
    payload = json.loads(stdout_line)
    return {c["id"]: c for c in payload["criteria"]}


@pytest.mark.unit
def test_patch_skip_reason_waives_all_three_scenarios(scratch_repo: tuple[Path, Path]) -> None:
    repo, marker_dir = scratch_repo
    reason = "diff touches none of 8 disqualifying triggers"

    proc = _run_rehearse(repo, patch_skip_reason=reason, json_out=True)

    assert proc.returncode == 0, f"a waived rehearsal must still exit 0:\n{proc.stderr}"
    assert reason in proc.stderr, proc.stderr

    for name in ("test-fresh-install.sh", "test-upgrade.sh", "test-lite-mode.sh"):
        marker = marker_dir / f"{name}.log"
        assert not marker.exists(), (
            f"{name} was invoked despite OT_PATCH_SKIP_REASON being set — the waiver "
            f"did not actually skip the scenario"
        )

    criteria = _criteria_by_id(proc.stdout.strip().splitlines()[-1])
    for cid in ("fresh-install", "upgrade-from-previous", "lite-mode"):
        assert criteria[cid]["outcome"] == "not-measured", criteria[cid]
        assert criteria[cid]["severity"] == "warn", (
            f"{cid} must be downgraded to warn via the waived 5th arg, else a waived "
            f"rehearsal would block the release exactly like an unrun one: {criteria[cid]}"
        )
        assert reason in criteria[cid]["detail"], criteria[cid]


@pytest.mark.unit
def test_must_stay_clean_without_the_env_var_every_scenario_actually_runs(
    scratch_repo: tuple[Path, Path],
) -> None:
    """The control: this is what catches a waiver predicate that silently
    became always-true. Without OT_PATCH_SKIP_REASON, all three scenario
    scripts (and the Scenario A/B teardown re-invocations) must actually run.
    """
    repo, marker_dir = scratch_repo

    proc = _run_rehearse(repo, patch_skip_reason=None, json_out=True)

    assert proc.returncode == 0, proc.stderr
    for banner in ("Scenario A", "Scenario B", "Scenario C"):
        assert banner in proc.stderr, (
            f"{banner} banner missing — the normal path was not taken:\n{proc.stderr}"
        )

    for name in ("test-fresh-install.sh", "test-upgrade.sh", "test-lite-mode.sh"):
        marker = marker_dir / f"{name}.log"
        assert marker.exists(), f"{name} was never invoked with no waiver in effect"

    criteria = _criteria_by_id(proc.stdout.strip().splitlines()[-1])
    for cid in ("fresh-install", "upgrade-from-previous", "lite-mode"):
        assert criteria[cid]["outcome"] == "pass", criteria[cid]
        assert criteria[cid]["severity"] == "blocking", (
            f"an un-waived pass must keep its declared severity, not read as warn: {criteria[cid]}"
        )


@pytest.mark.unit
def test_a_waived_run_records_no_detail_leak_into_an_unrelated_stage(
    scratch_repo: tuple[Path, Path],
) -> None:
    """Guard against a sloppy env read: an OT_PATCH_SKIP_REASON of the empty
    string must behave exactly like it being unset (scenarios still run).
    """
    repo, marker_dir = scratch_repo

    proc = _run_rehearse(repo, patch_skip_reason="", json_out=False)

    assert proc.returncode == 0, proc.stderr
    for name in ("test-fresh-install.sh", "test-upgrade.sh", "test-lite-mode.sh"):
        assert (marker_dir / f"{name}.log").exists(), (
            f"an empty OT_PATCH_SKIP_REASON must not waive {name}"
        )
