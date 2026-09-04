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
PREDICATE_LIB = REPO_ROOT / "scripts" / "lib" / "diar-native-expected.sh"

pytestmark = pytest.mark.skipif(
    not (SCRIPT.exists() and PREDICATE_LIB.exists()),
    reason="run-integration-tests.sh / lib/diar-native-expected.sh not present in this checkout",
)


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _predicate_block() -> str:
    """A `source` of the REAL predicate library.

    This used to splice the `read_env_var` + `diar_native_sidecar_expected` function bodies
    out of run-integration-tests.sh by regex. Both moved to scripts/lib/diar-native-expected.sh
    when run-dev-tests.sh needed the same predicate (a second copy of "is native diarization
    configured?" is how this repo's env-var drift starts), and the regex then matched nothing —
    9 tests failed at once.

    Sourcing the library is strictly better than re-splicing text from its new home: the tests
    exercise the same file the gate itself sources, so the two cannot diverge, and a future move
    breaks one line here instead of a regex. The library takes no caller globals by design.
    """
    assert "diar_native_sidecar_expected()" in PREDICATE_LIB.read_text(encoding="utf-8"), (
        f"{PREDICATE_LIB.name} no longer defines diar_native_sidecar_expected() — if it moved "
        f"again, repoint this test rather than deleting it: it guards the #669 "
        f"weights-or-token predicate reuse"
    )
    assert "diar-native-expected.sh" in _source(), (
        "run-integration-tests.sh no longer sources the shared predicate library, so these "
        "tests would be exercising a predicate the gate does not actually use"
    )
    return f'source "{PREDICATE_LIB}"'


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
    """Vacuity guard: the predicate library and the decision regex both really resolve."""
    assert "diar-native-expected.sh" in _predicate_block()
    assert "diar_native_sidecar_expected()" in PREDICATE_LIB.read_text(encoding="utf-8")
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
    set_models_dir: bool = True,
    model_cache_dir_has_export: bool = False,
) -> tuple[int, list[str], list[str]]:
    """Run the real predicate + decision block under bash, with a stubbed smoke script and
    env inputs, and report (bash_exit, FAILED_PHASES, SKIPPED_PHASES).

    ``set_models_dir=False`` + ``model_cache_dir_has_export=True`` exercises the
    MODEL_CACHE_DIR cascade, which is the ONLY shape an air-gapped install actually takes:
    ``.env.example`` ships DIAR_NATIVE_MODELS_DIR commented out.
    """
    project_root = _make_fake_smoke_script(tmp_path, smoke_rc)

    models_dir = tmp_path / "diar-models"
    if models_dir_has_export:
        models_dir.mkdir(parents=True, exist_ok=True)
        (models_dir / "weights.onnx").write_text("x", encoding="utf-8")

    cache_dir = tmp_path / "model-cache"
    if model_cache_dir_has_export:
        (cache_dir / "diar-native").mkdir(parents=True, exist_ok=True)
        (cache_dir / "diar-native" / "weights.onnx").write_text("x", encoding="utf-8")

    snippet = (
        "set -uo pipefail\n"
        f'PROJECT_ROOT="{project_root}"\n'
        # Point the predicate library's .env lookup at this test's tmp dir, which has no
        # .env. Without it the library reads the REAL repo .env, and since the predicate
        # resolves inputs as ${VAR:-$(read .env)} — where `:-` treats an EMPTY value as
        # unset — passing hf_token="" would fall through to the developer's actual
        # HUGGINGFACE_TOKEN and turn every "sidecar not expected" case into "expected".
        # The test would then be reporting on the machine, not the scenario.
        f'export DIAR_NATIVE_EXPECTED_REPO_ROOT="{project_root}"\n'
        # Same reason: the cascade's legacy leg is a hardcoded workstation path that really
        # exists on the maintainer's machine and nowhere else, so "no export anywhere" would
        # resolve to "expected" there and "not expected" in CI — the test would report on the
        # host rather than the scenario. Point it somewhere guaranteed absent.
        f'export DIAR_NATIVE_EXPECTED_LEGACY_DIR="{tmp_path / "no-such-legacy-dir"}"\n'
        f'SCRIPT_DIR="{REPO_ROOT / "scripts"}"\n'
        "BLUE=''; GREEN=''; YELLOW=''; RED=''; NC=''\n"
        f'ENGINE_DIARIZER_BACKEND="{backend}"\n'
        + (f'DIAR_NATIVE_MODELS_DIR="{models_dir}"\n' if set_models_dir else "")
        + f'MODEL_CACHE_DIR="{cache_dir}"\n'
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


def test_an_export_reachable_only_via_the_model_cache_cascade_counts_as_expected(
    tmp_path: Path,
):
    """The air-gapped shape: no DIAR_NATIVE_MODELS_DIR, no token, models under MODEL_CACHE_DIR.

    `.env.example` ships DIAR_NATIVE_MODELS_DIR **commented out**, so on a real air-gapped
    install the export is only reachable through `opentr.sh`'s resolve_diar_native_models_dir()
    cascade (`${MODEL_CACHE_DIR}/diar-native`). Before that cascade was mirrored into the shared
    predicate, this gate answered "sidecar not expected" while opentr.sh auto-loaded the sidecar
    anyway — so a NOT-MEASURED leg on a **dead diarizer** exited 0 and read as green.

    Every other test here sets DIAR_NATIVE_MODELS_DIR explicitly, which is precisely why none of
    them could catch it.
    """
    _, failed, skipped = _run(
        tmp_path,
        smoke_rc=4,  # NOT MEASURED
        backend="native",
        set_models_dir=False,
        model_cache_dir_has_export=True,
        hf_token="",
    )
    assert failed, (
        "an export found via the MODEL_CACHE_DIR cascade means the sidecar IS expected, so a "
        "NOT-MEASURED leg must fail the gate rather than be filed as a skip"
    )
    assert not skipped


def test_the_cascade_does_not_invent_an_export_that_is_not_there(tmp_path: Path):
    """Control for the test above: same shape, cascade directory EMPTY.

    Without this, the cascade could be reporting "expected" unconditionally and the test above
    would still pass — which is the same false-green it was written to remove, one level up.
    """
    _, failed, skipped = _run(
        tmp_path,
        smoke_rc=4,
        backend="native",
        set_models_dir=False,
        model_cache_dir_has_export=False,
        hf_token="",
    )
    assert not failed, "no export and no token means the sidecar is genuinely not expected"
    assert skipped, "an unexpected NOT-MEASURED leg must still be REPORTED, not silently dropped"
