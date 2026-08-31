"""`scripts/test-matrix.sh`'s stage-3 legs must ALL run within one invocation.

`scripts/release/65-rehearse.sh` (leg `3`) runs Scenario B (`test-upgrade.sh`) last, and
that scenario deliberately leaves its stack running afterward on `TEST_BACKEND_PORT=5174`
("stack left running for inspection" — the right default for a human running
`release.sh rehearse` standalone). `65-rehearse.sh` itself never tears that stack down.

`check_stage3_precondition()` probes `localhost:5174` and treats it as reachable ->
`EXIT_PRECONDITION`. Before this fix, that check ran unconditionally for every stage-3 leg,
so once leg `3` passed and left its stack up, legs `3-lite` and `3-pki` were permanently
BLOCKED on the very next check within the SAME `test-matrix.sh all`/`3` invocation — they
never got a chance to even start, let alone reach their own preflight/teardown logic
(`scripts/pki/run-pki-e2e-leg.sh` already has a teardown preamble for exactly this
collision; `test-lite-mode.sh` has none of its own).

Fixed by making `check_stage3_precondition(id)` leg-aware: for leg `3` itself, a reachable
port 5174 is still a real precondition violation (the live/dev stack was never stopped).
For any OTHER stage-3 leg, a reachable port first triggers the same release-test cleanup
`run-pki-e2e-leg.sh` already uses on itself (`test-fresh-install.sh` / `test-upgrade.sh` /
`test-lite-mode.sh --cleanup --yes`), then re-checks — only failing the precondition if a
real stack remains after that attempt.

This test extracts the real, unmodified `LEGS` table and the `check_stage3_precondition` /
`run_leg` / `run_stage` functions out of the shipped script (same technique used elsewhere
in this repo for shell logic) and runs `run_stage 3` against a throwaway `REPO_ROOT`
containing STUB leg scripts at the exact relative paths `LEGS` references — so it proves the
real control flow the review was worried about, without spending 45-120 minutes rebuilding
real images or touching Docker/the live stack. `service_reachable` is the one function
replaced outright: the thing under test is the SEQUENCING (does leg 3-lite/3-pki actually
run after leg 3 leaves a stack up), not the one-line `/dev/tcp` primitive — and the real
network probe would be unsafe here anyway, since a real rehearsal-remnant stack may already
be running on this host's port 5174.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_MATRIX = REPO_ROOT / "scripts" / "test-matrix.sh"

pytestmark = pytest.mark.skipif(
    not TEST_MATRIX.exists(), reason="scripts/test-matrix.sh not present in this checkout"
)


def _extract_function(script: Path, name: str) -> str:
    out = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip(), f"{name}() not found in {script.name}"
    return out


def _extract_legs_array(script: Path) -> str:
    out = subprocess.run(
        ["sed", "-n", r"/^LEGS=(/,/^)/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert re.search(r'"3\|3\|', out), f"LEGS array (leg 3) not found in {script.name}"
    assert '"3-lite|3|' in out, f"LEGS array (leg 3-lite) not found in {script.name}"
    assert '"3-pki|3|' in out, f"LEGS array (leg 3-pki) not found in {script.name}"
    return out


def _write_stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _stage3_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a throwaway REPO_ROOT with stub leg scripts at the LEGS-referenced paths.

    Returns (fake_repo_root, marker_dir, state_file). `state_file` existing means "a
    release-test stack (Scenario B) is still reachable on 5174" — leg `3`'s stub creates
    it, and `test-upgrade.sh --cleanup`'s stub is the only one that removes it, so the
    test can tell whether the precondition's cleanup call sequence actually ran.
    """
    fake_repo = tmp_path / "repo"
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    state_file = tmp_path / "state" / "port-5174-reachable"

    (fake_repo / "VERSION").parent.mkdir(parents=True, exist_ok=True)
    (fake_repo / "VERSION").write_text("9.9.9\n")

    # Leg "3": scripts/release/65-rehearse.sh <version> — passes, and leaves Scenario B's
    # stack "up" (state_file present), exactly as the real script does.
    _write_stub(
        fake_repo / "scripts" / "release" / "65-rehearse.sh",
        f'touch "{marker_dir}/leg3.ran"\nmkdir -p "$(dirname "{state_file}")"\n'
        f'touch "{state_file}"\nexit 0\n',
    )

    # Leg "3-lite": scripts/release-tests/test-lite-mode.sh --yes (also the target of the
    # precondition's own `--cleanup --yes` call — dispatch on $1, do nothing on cleanup so
    # the test can prove test-upgrade.sh's cleanup is what actually clears the state).
    _write_stub(
        fake_repo / "scripts" / "release-tests" / "test-lite-mode.sh",
        f'if [ "$1" = "--cleanup" ]; then exit 0; fi\ntouch "{marker_dir}/leg3lite.ran"\nexit 0\n',
    )

    # Leg "3-pki": scripts/pki/run-pki-e2e-leg.sh --yes
    _write_stub(
        fake_repo / "scripts" / "pki" / "run-pki-e2e-leg.sh",
        f'touch "{marker_dir}/leg3pki.ran"\nexit 0\n',
    )

    # Cleanup-only targets the precondition calls for any leg OTHER than "3".
    _write_stub(fake_repo / "scripts" / "release-tests" / "test-fresh-install.sh", "exit 0\n")
    _write_stub(
        fake_repo / "scripts" / "release-tests" / "test-upgrade.sh",
        f'if [ "$1" = "--cleanup" ]; then rm -f "{state_file}"; fi\nexit 0\n',
    )

    return fake_repo, marker_dir, state_file


def _run_stage3(fake_repo: Path, state_file: Path) -> str:
    legs = _extract_legs_array(TEST_MATRIX)
    check_precond = _extract_function(TEST_MATRIX, "check_stage3_precondition")
    run_leg = _extract_function(TEST_MATRIX, "run_leg")
    run_stage = _extract_function(TEST_MATRIX, "run_stage")

    snippet = f"""
set -uo pipefail
EXIT_GATE=1; EXIT_MISUSE=2; EXIT_PRECONDITION=3; EXIT_ABORT=4
RED=''; GREEN=''; YELLOW=''; NC=''
err()  {{ echo "ERR: $*" >&2; }}
info() {{ echo "INFO: $*" >&2; }}
MODE_DRY_RUN=false
ASSUME_YES=true
SKIP_COUNT=0
declare -a SKIPPED_LEGS=()
LEDGER_DIR="{fake_repo}/.test-matrix"
REPORT_FILE="$LEDGER_DIR/REPORT.md"

# The one function replaced outright: the network primitive itself is a one-liner and not
# what this test is verifying. It answers from the state file the stub leg scripts
# manage, so the real /dev/tcp probe (and any real stack on this host's port 5174) is
# never touched.
service_reachable() {{ [ -f "{state_file}" ]; }}

{legs}
{check_precond}
{run_leg}
{run_stage}

cd "{fake_repo}" || exit 2
run_stage 3
echo "RC=$?"
"""
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    return proc.stdout + proc.stderr


@pytest.mark.unit
def test_all_three_stage3_legs_run_in_one_invocation(tmp_path: Path) -> None:
    """The reviewer's exact scenario: leg 3 leaves a stack up; 3-lite and 3-pki must still run."""
    fake_repo, marker_dir, state_file = _stage3_fixture(tmp_path)

    out = _run_stage3(fake_repo, state_file)

    assert (marker_dir / "leg3.ran").exists(), f"leg 3 never ran:\n{out}"
    assert (marker_dir / "leg3lite.ran").exists(), (
        f"leg 3-lite was blocked by leg 3's leftover stack — the bug being fixed:\n{out}"
    )
    assert (marker_dir / "leg3pki.ran").exists(), (
        f"leg 3-pki was blocked by leg 3's leftover stack — the bug being fixed:\n{out}"
    )
    assert "RC=0" in out, f"all three legs stubbed a pass; the stage must report success:\n{out}"
    assert not state_file.exists(), (
        "the leftover release-test stack should have been cleared before 3-lite/3-pki ran"
    )


@pytest.mark.unit
def test_leg_3_itself_is_not_torn_down_by_its_own_precondition_check(tmp_path: Path) -> None:
    """A real precondition violation for leg 3 (dev stack already up) must still block it.

    Leg "3" must never trigger the release-test cleanup on itself — that cleanup is only
    for the siblings that run AFTER leg 3 leaves its own stack up.
    """
    fake_repo, marker_dir, state_file = _stage3_fixture(tmp_path)
    # Simulate: something is already reachable on 5174 BEFORE leg 3 even starts (e.g. the
    # live/dev stack was never stopped) — leg 3 must be blocked, not cleaned up and run.
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.touch()

    # Isolate: only run leg "3" via --only-style extraction (call check_stage3_precondition
    # directly) so this test does not depend on 3-lite/3-pki's behavior.
    check_precond = _extract_function(TEST_MATRIX, "check_stage3_precondition")
    snippet = f"""
set -uo pipefail
EXIT_PRECONDITION=3
err()  {{ echo "ERR: $*" >&2; }}
info() {{ echo "INFO: $*" >&2; }}
service_reachable() {{ [ -f "{state_file}" ]; }}

{check_precond}

check_stage3_precondition "3"
echo "RC=$?"
"""
    proc = subprocess.run(
        ["bash", "-c", snippet], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}
    )
    out = proc.stdout + proc.stderr

    assert "RC=3" in out, f"leg 3 with a real precondition violation must be BLOCKED:\n{out}"
    assert state_file.exists(), (
        "leg 3's own precondition check must never clear a stack it does not know is a "
        "release-test remnant — only 3-lite/3-pki's checks may attempt that cleanup"
    )
