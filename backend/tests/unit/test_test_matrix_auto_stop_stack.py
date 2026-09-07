"""`test-matrix.sh all` could never reach stage 3, and the escape hatch must stay explicit.

THE DEADLOCK

`check_stage2_precondition` requires the dev stack reachable on 5174, and leg `2a`
(`run-dev-tests.sh`) does not stop what it did not start. `check_stage3_precondition`
requires it STOPPED — the release-test scenarios bind the stock 5173-5180 ports under the
stock `opentranscribe-*` names deliberately, so the rehearsal exercises what a real user's
install produces.

So in `test-matrix.sh all`, leg `3` hit the "requires the dev stack STOPPED" refusal, and legs
`3-lite`/`3-pki` then found 5174 still reachable — the release-test cleanup they run first
correctly refuses to touch a stack that is not a release-test remnant — and reported BLOCKED
as well. **Three legs, every run, structurally.** The matrix's whole stage 3 was unreachable
in the mode the matrix exists for.

WHY THE FIX IS A SEPARATE FLAG AND NOT PART OF `--yes`

`--yes` says "I accept hours of runtime and image rebuilds". It does not say "you may stop
the deployment I am using". Spending one consent on the other is how a script ends up taking
down someone's running stack in the background; `--auto-stop-stack` is default OFF and these
tests pin that it is not reachable any other way.

APPROACH

The real `check_stage3_precondition` and the real argument parser are extracted from the
shipped script and run against a scratch REPO_ROOT holding a stub `./opentr.sh`, with
`service_reachable` answering from a state file the stub manages — so the real control flow
runs without touching this host's port 5174 or its actual stack.
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


def _extract_function(name: str) -> str:
    out = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(TEST_MATRIX)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip(), f"{name}() not found in test-matrix.sh"
    return out


def _run_precondition(
    tmp_path: Path, *, auto_stop: bool, assume_yes: bool = True, leg: str = "3"
) -> tuple[int, str, bool]:
    """Run the REAL check_stage3_precondition. Returns (rc, output, opentr_stop_called)."""
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    state = tmp_path / "port-5174-up"
    state.write_text("live dev stack\n", encoding="utf-8")
    stop_log = tmp_path / "opentr.log"

    opentr = fake_repo / "opentr.sh"
    opentr.write_text(
        f'#!/bin/bash\necho "$@" >> "{stop_log}"\n[ "$1" = "stop" ] && rm -f "{state}"\nexit 0\n',
        encoding="utf-8",
    )
    opentr.chmod(opentr.stat().st_mode | stat.S_IEXEC)

    # The three release-test cleanup scripts the non-"3" path invokes. They must NOT clear
    # the state file: a live dev stack is not a release-test remnant, and the real scripts'
    # guardrails refuse to touch one. That refusal is what made 3-lite/3-pki BLOCKED too.
    for rel in (
        "scripts/release-tests/test-fresh-install.sh",
        "scripts/release-tests/test-upgrade.sh",
        "scripts/release-tests/test-lite-mode.sh",
    ):
        stub = fake_repo / rel
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    snippet = f"""
set -uo pipefail
EXIT_PRECONDITION=3
RED=''; YELLOW=''; NC=''
err()  {{ echo "ERR: $*" >&2; }}
info() {{ echo "INFO: $*" >&2; }}
AUTO_STOP_STACK={str(auto_stop).lower()}
ASSUME_YES={str(assume_yes).lower()}
STACK_WAS_STOPPED_BY_US=false
service_reachable() {{ [ -f "{state}" ]; }}

{_extract_function("check_stage3_precondition")}

cd "{fake_repo}" || exit 2
check_stage3_precondition "{leg}"
echo "RC=$?"
echo "STOPPED_BY_US=$STACK_WAS_STOPPED_BY_US"
"""
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    called = stop_log.exists() and "stop" in stop_log.read_text(encoding="utf-8")
    return proc.returncode, out, called


@pytest.mark.unit
def test_default_off_leaves_the_stack_alone_and_blocks(tmp_path: Path) -> None:
    """Must-stay-clean: the refusal is correct behaviour without the flag."""
    _, out, stop_called = _run_precondition(tmp_path, auto_stop=False)

    assert not stop_called, "the dev stack must never be stopped without an explicit opt-in"
    assert "RC=3" in out, f"a live stack must still BLOCK stage 3:\n{out}"
    assert "--auto-stop-stack" in out, (
        f"the refusal must name the escape hatch, or the deadlock stays undiscoverable:\n{out}"
    )


@pytest.mark.unit
def test_the_flag_stops_the_stack_and_unblocks_stage_3(tmp_path: Path) -> None:
    _, out, stop_called = _run_precondition(tmp_path, auto_stop=True)

    assert stop_called, f"--auto-stop-stack must run ./opentr.sh stop:\n{out}"
    assert "RC=0" in out, f"stage 3 must proceed once the stack is down:\n{out}"
    assert "STOPPED_BY_US=true" in out, out
    assert "./opentr.sh start dev" in out, (
        f"the restart command must be printed at the moment of stopping — what follows "
        f"takes hours and the banner will have scrolled away:\n{out}"
    )


@pytest.mark.unit
def test_it_applies_to_the_sibling_legs_too(tmp_path: Path) -> None:
    """3-lite/3-pki were blocked by the same live stack, one leg later."""
    _, out, stop_called = _run_precondition(tmp_path, auto_stop=True, leg="3-pki")

    assert stop_called, out
    assert "RC=0" in out, out


@pytest.mark.unit
def test_yes_does_not_imply_auto_stop_stack(tmp_path: Path) -> None:
    """Two different consents. Spending one on the other is the hazard.

    Checked at the ARGUMENT PARSER, not only behaviourally: the behavioural test above runs
    with ASSUME_YES=true already, so this pins that no future edit makes `--yes` set the
    other variable.
    """
    text = TEST_MATRIX.read_text(encoding="utf-8")
    yes_arm = re.search(r"^\s*--yes\)(.*)$", text, re.M)
    assert yes_arm, "no --yes argument arm found"
    assert "AUTO_STOP_STACK" not in yes_arm.group(1), (
        f"--yes must not enable stack stopping: {yes_arm.group(0)!r}"
    )

    assert re.search(r"^\s*--auto-stop-stack\) AUTO_STOP_STACK=true", text, re.M), (
        "--auto-stop-stack must be its own argument arm"
    )
    assert re.search(r"^AUTO_STOP_STACK=false$", text, re.M), (
        "the default must be OFF, at the one place it is declared"
    )


@pytest.mark.unit
def test_the_flag_is_documented_in_usage() -> None:
    """A flag nobody can discover does not fix a deadlock nobody can escape."""
    proc = subprocess.run(
        ["bash", str(TEST_MATRIX), "--help"], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert "--auto-stop-stack" in proc.stdout, proc.stdout
    assert "DEFAULT OFF" in proc.stdout, proc.stdout
