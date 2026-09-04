"""`scripts/run-integration-tests.sh` must not let a NOT-MEASURED diar-native leg read as green.

Issue #669: `scripts/diar-native-smoke.sh` correctly exits 4 (NOT MEASURED) when the sidecar
isn't running, and `scripts/test-matrix.sh` was fixed to propagate that honestly via its own
EXIT_NOT_MEASURED. `scripts/run-integration-tests.sh` — THE pre-merge gate — was not: it ran the
smoke script through the generic `run_phase` helper, which maps exit 4 to a silent skip and an
overall exit 0 unconditionally. So the gate a developer runs before merging was green on a stack
whose diarizer never ran, even on a machine where native diarization was fully configured and the
sidecar simply wasn't started.

The fix is policy, not a blanket flip: fail on NOT MEASURED only when the sidecar was EXPECTED
(engine.diarizer_backend resolves to native AND weights-or-token are configured — the same
predicate opentr.sh's add_diar_native_overlay() uses to decide whether to auto-load the sidecar
overlay at all). When it wasn't expected, skip honestly but visibly. A real failure (exit 1) must
never be downgraded by any of this.

These tests extract the REAL bash source — the `diar_native_sidecar_expected()` predicate and the
decision block that consumes `diar-native-smoke.sh`'s exit code — and run it for real under bash,
per this repo's established pattern (test_test_matrix_execution.py's
test_not_measured_legs_do_not_exit_zero). A grep for a string would pass against a block that
computed the wrong outcome.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "run-integration-tests.sh"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists(), reason="scripts/run-integration-tests.sh not present in this checkout"
)


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _predicate_block() -> str:
    """The `read_env_var` + `diar_native_sidecar_expected` function definitions, verbatim."""
    source = _source()
    match = re.search(
        r"^read_env_var\(\) \{\n.*?^diar_native_sidecar_expected\(\) \{\n.*?^\}$",
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert match, (
        "diar_native_sidecar_expected()/read_env_var() were not found in "
        "run-integration-tests.sh — if they were renamed, update this test rather than "
        "deleting it: it guards the #669 weights-or-token predicate reuse"
    )
    return match.group(0)


def _decision_block() -> str:
    """The block that consumes diar-native-smoke.sh's exit code and files it into
    FAILED_PHASES / SKIPPED_PHASES."""
    source = _source()
    match = re.search(
        r"^    diar_native_rc=0\n.*?^    fi$",
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert match, (
        "the diar-native decision block was not found in run-integration-tests.sh — if it "
        "was renamed or restructured, update this test rather than deleting it: it guards "
        "the #669 NOT-MEASURED-must-not-read-as-green fix"
    )
    return match.group(0)


def test_the_predicate_and_decision_blocks_are_present():
    """Vacuity guard for the two regexes above."""
    assert "diar_native_sidecar_expected" in _predicate_block()
    assert "FAILED_PHASES+=" in _decision_block()
    assert "SKIPPED_PHASES+=" in _decision_block()


def _make_fake_smoke_script(tmp_path: Path, rc: int) -> Path:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    fake = scripts_dir / "diar-native-smoke.sh"
    fake.write_text(f"#!/bin/bash\nexit {rc}\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def _run(
    tmp_path: Path,
    smoke_rc: int,
    *,
    backend: str = "native",
    models_dir_has_export: bool = False,
    hf_token: str = "",
) -> tuple[int, list[str], list[str]]:
    """Run the real predicate + decision block under bash, with a stubbed smoke script and
    env inputs, and report (bash_exit, FAILED_PHASES, SKIPPED_PHASES)."""
    project_root = _make_fake_smoke_script(tmp_path, smoke_rc)

    models_dir = tmp_path / "diar-models"
    if models_dir_has_export:
        models_dir.mkdir(parents=True, exist_ok=True)
        (models_dir / "weights.onnx").write_text("x", encoding="utf-8")

    snippet = (
        "set -uo pipefail\n"
        f'PROJECT_ROOT="{project_root}"\n'
        f'SCRIPT_DIR="{REPO_ROOT / "scripts"}"\n'
        "BLUE=''; GREEN=''; YELLOW=''; RED=''; NC=''\n"
        f'ENGINE_DIARIZER_BACKEND="{backend}"\n'
        f'DIAR_NATIVE_MODELS_DIR="{models_dir}"\n'
        f'HUGGINGFACE_TOKEN="{hf_token}"\n'
        "FAILED_PHASES=()\n"
        "SKIPPED_PHASES=()\n"
        f"{_predicate_block()}\n"
        f"{_decision_block()}\n"
        'printf "FAILED:"; printf "%s|" "${FAILED_PHASES[@]:-}"; printf "\\n"\n'
        'printf "SKIPPED:"; printf "%s|" "${SKIPPED_PHASES[@]:-}"; printf "\\n"\n'
        "exit 0\n"
    )
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
    )
    failed: list[str] = []
    skipped: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("FAILED:"):
            failed = [x for x in line[len("FAILED:") :].split("|") if x]
        elif line.startswith("SKIPPED:"):
            skipped = [x for x in line[len("SKIPPED:") :].split("|") if x]
    assert proc.returncode == 0, (
        f"harness itself failed (not the thing under test): rc={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    return proc.returncode, failed, skipped


def test_sidecar_expected_and_not_measured_fails(tmp_path: Path):
    """native backend + a real export present + smoke exits 4 -> must be filed as a FAILURE,
    never silently skipped."""
    _, failed, skipped = _run(
        tmp_path, smoke_rc=4, backend="native", models_dir_has_export=True, hf_token=""
    )
    assert failed, "an expected-but-NOT-MEASURED diar-native leg must land in FAILED_PHASES"
    assert not skipped, "an expected-but-NOT-MEASURED leg must not also be counted as a skip"


def test_sidecar_expected_via_token_and_not_measured_fails(tmp_path: Path):
    """native backend + no export yet but a HUGGINGFACE_TOKEN configured (so the backend will
    provision on startup) + smoke exits 4 -> also expected, also a failure."""
    _, failed, skipped = _run(
        tmp_path,
        smoke_rc=4,
        backend="native",
        models_dir_has_export=False,
        hf_token="hf_dummy_token",
    )
    assert failed, "a token-configured-but-NOT-MEASURED diar-native leg must fail the gate"
    assert not skipped


def test_sidecar_not_expected_and_not_measured_passes_but_is_reported(tmp_path: Path):
    """native backend, but no export AND no token -> the sidecar could never have started, so
    this must NOT fail the gate -- but it must still show up by name, not vanish silently."""
    _, failed, skipped = _run(
        tmp_path, smoke_rc=4, backend="native", models_dir_has_export=False, hf_token=""
    )
    assert not failed, "an unexpected NOT-MEASURED leg must not fail the everyday gate"
    assert skipped, "an unexpected NOT-MEASURED leg must still be named, not silently dropped"


def test_sidecar_not_expected_non_native_backend_and_not_measured_passes(tmp_path: Path):
    """engine.diarizer_backend != native -> the sidecar is never expected regardless of
    weights/token, so this must pass (but still be reported)."""
    _, failed, skipped = _run(
        tmp_path,
        smoke_rc=4,
        backend="pyannote",
        models_dir_has_export=True,
        hf_token="hf_dummy_token",
    )
    assert not failed, "a non-native backend must never make a NOT-MEASURED leg fail"
    assert skipped, "it must still be named as not-measured"


def test_everything_measured_and_passing_reports_neither_failed_nor_skipped(tmp_path: Path):
    _, failed, skipped = _run(tmp_path, smoke_rc=0, backend="native", models_dir_has_export=True)
    assert not failed
    assert not skipped


@pytest.mark.parametrize("real_failure_rc", [1, 2, 3])
def test_a_real_failure_keeps_its_own_verdict_never_downgraded_to_skip(
    tmp_path: Path, real_failure_rc: int
):
    """A real failure (any non-0, non-4 exit) must always land in FAILED_PHASES, regardless of
    whether the sidecar was 'expected' -- the expected/not-expected policy applies ONLY to the
    NOT-MEASURED (exit 4) case."""
    _, failed, skipped = _run(
        tmp_path,
        smoke_rc=real_failure_rc,
        backend="native",
        models_dir_has_export=False,
        hf_token="",
    )
    assert failed, f"a real failure (rc={real_failure_rc}) must never be downgraded to a skip"
    assert not skipped
