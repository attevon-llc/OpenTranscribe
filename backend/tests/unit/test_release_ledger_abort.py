"""`scripts/release.sh`'s ledger must distinguish an operator ABORT from a real FAILURE.

`65-rehearse.sh` correctly propagates exit code 4 (the operator declined the scenario's
`I UNDERSTAND` confirmation prompt — nothing ran) up from `guardrails.sh`'s `gr_abort`,
distinct from exit 1 (a genuine regression a rehearsal actually found). But `run_stage()`'s
case block in `scripts/release.sh` had explicit arms only for `0`, `$EXIT_MISUSE`, and
`$EXIT_PRECONDITION` — exit 4 fell into the catch-all `*)`, which records
`status=failed detail=exit=4`, identical to a real failure, and returns `$EXIT_GATE` (1)
from `run_stage()` itself. An adversarial review confirmed this by running the case block
in isolation.

Fixed by adding a dedicated `"$EXIT_ABORT")` arm that records `status=aborted` (never
`failed`) and leaves `run_stage()`'s own return code at 4 (never coerced to `$EXIT_GATE`),
so a caller (`cmd_run`'s `run_stage ... || return $?`) can tell the two apart.

Decision recorded here too: `--force-<stage>` does NOT apply to an ABORT. Forcing past a
FAILURE means "a human reviewed a real regression and accepts the risk" — a decision worth
recording. Forcing past an ABORT would only mean "pretend the operator answered a prompt
they declined", which is not a decision. So exit 4 is never looked up in `FORCE_REASON`;
the correct recovery is re-running the stage and answering the prompt (or passing --yes).

These tests extract `run_stage()` (plus the ledger/lookup helpers it calls) out of
`scripts/release.sh` and execute it directly against a throwaway `SCRIPT_DIR`/`REPO_ROOT`
with a stub stage script — the same technique `test_install_upgrade_scripts.py` uses for
`setup-opentranscribe.sh` — rather than running the real 45-120 minute rehearsal scenario.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_SH = REPO_ROOT / "scripts" / "release.sh"

pytestmark = pytest.mark.skipif(
    not RELEASE_SH.exists(), reason="scripts/release.sh not present in this checkout"
)


def _extract_function(script: Path, name: str) -> str:
    """Pull one shell function out of a script so it can be run in isolation."""
    out = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip(), f"{name}() not found in {script.name}"
    return out


def _extract_line(script: Path, name: str) -> str:
    """Pull a one-line `name() { ... }` definition verbatim."""
    for line in script.read_text().splitlines():
        if line.strip().startswith(f"{name}() {{"):
            return line
    raise AssertionError(f"{name}() one-liner not found in {script.name}")


def _run_stage_harness(tmp_path: Path, stage_exit: int, force_reason: str | None = None) -> str:
    """Build a scratch SCRIPT_DIR/REPO_ROOT, a stub `rehearse` stage script that exits
    `stage_exit`, and run the real `run_stage()` against it. Returns combined
    stdout+stderr, which ends with `RC=<n>` and the raw ledger file contents.
    """
    script_dir = tmp_path / "scripts"
    release_dir = script_dir / "release"
    release_dir.mkdir(parents=True)

    stub = release_dir / "65-rehearse.sh"
    stub.write_text(f"#!/bin/bash\nexit {stage_exit}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    force_line = ""
    if force_reason is not None:
        force_line = f'FORCE_REASON["rehearse"]="{force_reason}"\n'

    snippet = f"""
set -euo pipefail
EXIT_GATE=1
EXIT_MISUSE=2
EXIT_PRECONDITION=3
EXIT_ABORT=4
EXTERNAL_STAGES="tag publish promote finish"

log()  {{ :; }}
ok()   {{ :; }}
warn() {{ echo "WARN: $*" >&2; }}
err()  {{ echo "ERR: $*" >&2; }}

{_extract_line(RELEASE_SH, "ledger_dir")}
{_extract_function(RELEASE_SH, "ledger_record")}
{_extract_function(RELEASE_SH, "stage_script")}
{_extract_function(RELEASE_SH, "run_stage")}

SCRIPT_DIR="{script_dir}"
REPO_ROOT="{tmp_path}"
ASSUME_YES=false
DRY_RUN=false
JSON_OUT=false
declare -A FORCE_REASON=()
{force_line}

set +e
run_stage "1.2.3" "rehearse"
rc=$?
set -e
echo "RC=$rc"
echo "--- ledger ---"
cat "$REPO_ROOT/.release/1.2.3/steps/rehearse"
"""
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "USER": "test-operator"},
    )
    return proc.stdout + proc.stderr


@pytest.mark.unit
def test_abort_is_recorded_as_aborted_not_failed(tmp_path: Path) -> None:
    """The reviewer's exact scenario: rehearse exits 4 (operator declined the prompt)."""
    out = _run_stage_harness(tmp_path, stage_exit=4)

    assert "RC=4" in out, f"run_stage() must return the ABORT code (4), not coerce it:\n{out}"
    assert "status=aborted" in out, f"an abort must be ledgered distinctly:\n{out}"
    assert "status=failed" not in out, (
        f"an abort must never be recorded as a failure — that is the bug being fixed:\n{out}"
    )


@pytest.mark.unit
def test_real_failure_is_still_recorded_as_failed(tmp_path: Path) -> None:
    """Control: a genuine gate failure (exit 1) must be unaffected by the new arm."""
    out = _run_stage_harness(tmp_path, stage_exit=1)

    assert "RC=1" in out, out
    assert "status=failed" in out, out
    assert "detail=exit=1" in out, out
    assert "status=aborted" not in out, out


@pytest.mark.unit
def test_force_rehearse_does_not_apply_to_an_abort(tmp_path: Path) -> None:
    """--force-rehearse with an ABORT must not fabricate an 'overridden' pass.

    Forcing past a FAILURE is a documented, recorded decision. Forcing past an ABORT
    would just claim the operator answered a prompt they declined — so exit 4 must
    stay `aborted` even when a force reason was supplied for the stage.
    """
    out = _run_stage_harness(tmp_path, stage_exit=4, force_reason="accepted for testing")

    assert "RC=4" in out, out
    assert "status=aborted" in out, out
    assert "status=overridden" not in out, (
        f"a force reason must not turn an ABORT into a fabricated override:\n{out}"
    )
    assert "does not apply to an abort" in out, "the operator should be told why it was ignored"


@pytest.mark.unit
def test_force_rehearse_still_overrides_a_real_failure(tmp_path: Path) -> None:
    """Control: forcing past a genuine failure (exit 1) is unchanged behavior."""
    out = _run_stage_harness(tmp_path, stage_exit=1, force_reason="accepted for testing")

    assert "RC=0" in out, "an overridden gate failure must not block the pipeline"
    assert "status=overridden" in out, out
    assert "reason=accepted for testing" in out, out
