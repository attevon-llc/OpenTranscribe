"""``--with-gpu-scale`` must not guess a GPU count, and must not over-deselect (M2 / M3).

Two defects in the same eight lines of ``scripts/run-dev-tests.sh``, both of which made the
multi-GPU legs quietly disappear on a host whose ``CLAUDE.md`` says they must run.

**M3 — a failed probe read as "1 GPU".** ``project_gpu_count`` shells out to
``backend/venv/bin/python`` + ``python-dotenv`` against ``.env``. In a git worktree — the case
the script's own documentation calls out, since neither ``.env`` nor ``backend/venv`` comes
along — it prints nothing, and ``[[ "" -lt 2 ]]`` is **true** in bash. So the script printed
"only 1 GPU available for this project" as a hardware claim it had never measured, and skipped
the leg. An unmeasurable count is now a precondition failure, not a verdict.

**M2 — a file-level deselect dropped a deliberately-selectable test.**
``--deselect=tests/integration/test_gpu_scale_smoke_live.py`` removed all three of that file's
tests, but only two carry ``multi_gpu``. ``test_default_worker_is_registered_in_dual_gpu_mode``
carries none on purpose (commit ``48fc6593``): it asserts the DEFAULT worker is registered,
which is a real claim on a single-GPU deployment too, and it has its own
``GPU_SCALE_DEFAULT_WORKER`` skipif. The marker-based deselection in
``run-integration-tests.sh`` — which *detects* the worker topology rather than assuming it —
already removes exactly the two that need it, so the file-level deselect was both redundant
and wider than the condition.

Static plus a direct drive of the real shell function; none of it needs a GPU or the stack.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEV_TESTS = REPO_ROOT / "scripts" / "run-dev-tests.sh"
INTEGRATION_GATE = REPO_ROOT / "scripts" / "run-integration-tests.sh"
SMOKE_TEST = REPO_ROOT / "backend" / "tests" / "integration" / "test_gpu_scale_smoke_live.py"

pytestmark = pytest.mark.skipif(
    not DEV_TESTS.is_file() or shutil.which("bash") is None,
    reason="scripts/run-dev-tests.sh or bash is not present in this checkout",
)


def _extract(script: Path, function_name: str) -> str:
    source = script.read_text(encoding="utf-8")
    start = source.index(f"{function_name}() {{")
    return source[start : source.index("\n}\n", start) + 3]


def _validate(count: str) -> tuple[int, str]:
    """Drive the REAL validate_project_gpu_count. Returns (exit status, stdout)."""
    harness = (
        textwrap.dedent(f"""
        set -uo pipefail
        {_extract(DEV_TESTS, "validate_project_gpu_count")}
    """)
        + f"validate_project_gpu_count {count!r}\n"
    )
    result = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        # Hermetic, like test_integration_gate_skip_ceiling.py's harness: nothing this
        # function reads should be able to arrive from the ambient environment.
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"},
    )
    return result.returncode, result.stdout.strip()


# ------------------------------------------------------------------ M3: the count is validated


@pytest.mark.parametrize("count", ["1", "2", "3", "8"])
def test_a_real_count_is_accepted_and_echoed(count: str):
    """The control. Without it, "reject everything" would satisfy every case below.

    ``1`` is included deliberately: a single-GPU project is a legitimate ANSWER, and the
    caller — not this function — decides what it means. Rejecting it would turn the honest
    single-GPU skip into a precondition failure.
    """
    assert _validate(count) == (0, count)


@pytest.mark.parametrize(
    ("count", "why"),
    [
        ("", "empty — what a missing python-dotenv or backend/venv actually produces"),
        ("   ", "whitespace only"),
        ("0", "zero GPUs is not a count this script can act on"),
        ("two", "non-numeric"),
        ("-1", "negative"),
        ("1.5", "not an integer"),
        ("1 2", "two values, e.g. an unexpected second line of output"),
        ("Traceback (most recent call last):", "the probe crashed and printed to stdout"),
    ],
)
def test_a_non_count_is_refused(count: str, why: str):
    status, out = _validate(count)
    assert status != 0, f"{why}: accepted as a GPU count ({out!r})"
    assert "not a positive integer" in out, f"{why}: refused without saying why ({out!r})"


def test_the_empty_case_is_the_one_bash_would_silently_call_one_gpu():
    """Pins the exact mechanism, so this cannot be "fixed" back by relaxing the pattern.

    ``[[ "" -lt 2 ]]`` is TRUE — bash treats an empty string as 0 in an arithmetic
    comparison — which is why an unvalidated empty probe result read as a single-GPU host
    rather than as an error.
    """
    proven = subprocess.run(
        ["bash", "-c", 'if [[ "" -lt 2 ]]; then echo yes; else echo no; fi'],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proven.stdout.strip() == "yes", (
        "bash no longer treats an empty string as 0 in [[ -lt ]]; this test's premise (and "
        "the guard it defends) needs revisiting"
    )
    assert _validate("")[0] != 0


def test_the_call_site_exits_precondition_rather_than_skipping_the_leg():
    """A validator nothing calls is decoration — and the wrong reaction is a silent skip."""
    source = DEV_TESTS.read_text(encoding="utf-8")
    call = re.search(
        r'if ! PROJECT_GPU_COUNT="\$\(validate_project_gpu_count "\$\(project_gpu_count\)"\)";'
        r" then(?P<body>.*?)\n    fi",
        source,
        re.S,
    )
    assert call, (
        "project_gpu_count's output is no longer validated before it is compared numerically "
        "— an unmeasurable count would read as 1 GPU again"
    )
    assert 'exit "$EXIT_PRECONDITION"' in call.group("body"), (
        "a GPU count that could not be measured must be a precondition failure, not a skip: "
        "a skip here is indistinguishable from an honest single-GPU host"
    )


# --------------------------------------------------- M2: the deselect must not be file-level


def test_the_gpu_scale_smoke_file_is_not_deselected_wholesale():
    source = DEV_TESTS.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if "--deselect" in line
        and "test_gpu_scale_smoke_live" in line
        and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "run-dev-tests.sh deselects tests/integration/test_gpu_scale_smoke_live.py by FILE: "
        f"{offenders}. That is wider than the condition — the file holds a test that carries "
        "no multi_gpu marker on purpose. run-integration-tests.sh's MULTI_GPU_FILTER already "
        "deselects by marker, and only when the worker topology is absent."
    )


def test_the_file_really_does_hold_a_deliberately_unmarked_test():
    """Non-vacuity for the check above: prove the premise instead of asserting it.

    If every test in that file ever carries ``multi_gpu``, the file-level deselect stops being
    wrong and this whole section should be deleted rather than left passing for free.
    """
    if not SMOKE_TEST.is_file():
        pytest.skip("tests/integration/test_gpu_scale_smoke_live.py is not in this checkout")

    tree = ast.parse(SMOKE_TEST.read_text(encoding="utf-8"))
    unmarked: list[str] = []
    marked: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        markers = {
            d.func.attr
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            else d.attr
            if isinstance(d, ast.Attribute)
            else ""
            for d in node.decorator_list
        }
        (marked if "multi_gpu" in markers else unmarked).append(node.name)

    assert marked, (
        "no test in test_gpu_scale_smoke_live.py carries @pytest.mark.multi_gpu — the "
        "marker-based deselection this relies on now selects nothing"
    )
    assert "test_default_worker_is_registered_in_dual_gpu_mode" in unmarked, (
        "test_default_worker_is_registered_in_dual_gpu_mode no longer sits in that file "
        f"WITHOUT a multi_gpu marker (unmarked: {unmarked}). Either it was marked — in which "
        "case say why, since it asserts the DEFAULT worker and commit 48fc6593 left it "
        "unmarked deliberately — or it moved, and this guard needs repointing."
    )


def test_the_marker_based_deselection_is_what_covers_the_multi_gpu_tests():
    """The redundancy claim, asserted rather than assumed.

    Deleting the file-level deselect is only safe because the gate deselects by MARKER when
    the worker topology is absent — and detects that topology rather than assuming it.
    """
    if not INTEGRATION_GATE.is_file():
        pytest.skip("scripts/run-integration-tests.sh not in this checkout")
    gate = INTEGRATION_GATE.read_text(encoding="utf-8")
    assert 'MULTI_GPU_FILTER=" and not multi_gpu"' in gate, (
        "the gate no longer deselects multi_gpu by marker, so removing run-dev-tests.sh's "
        "file-level deselect would leave those tests to fail on a single-GPU host"
    )
    assert "multi_gpu_topology_present" in gate, (
        "the deselection is no longer conditional on detecting the worker topology — an "
        "unconditional one hides real coverage on a host that CAN run these"
    )
