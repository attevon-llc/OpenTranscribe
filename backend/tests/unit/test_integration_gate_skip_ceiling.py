"""A mass-SKIPPED gate phase must not report as a passed phase (#491 follow-up).

``SKIP_S3`` / ``SKIP_OPENSEARCH`` are set by the root conftest from a TCP probe,
so with either service down the tests that need it **skip** rather than fail —
and pytest exits **0**. Measured on `scripts/run-integration-tests.sh`:

    stack up     101 passed,  3 skipped   exit 0   -> "✓ passed"
    stack down    34 passed, 71 skipped   exit 0   -> "✓ passed"

Identical verdict, 67 fewer tests executed. That is the documented silent-skip
trap, and it sat directly under the evidence for #400/#435 and #405/#432 — the
only tests that exercise real OpenSearch semantics for either.

This module is the self-test for the guard, driven the way `audit-tests.py`'s own
self-test is: a detector that silently matched nothing would report a clean gate
forever. It caught a real bug on its first run — ``|| true`` after the ``tee``
clobbered ``PIPESTATUS``, so a genuinely FAILING phase was recorded as neither
failed nor skipped.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[3] / "scripts" / "run-integration-tests.sh"

pytestmark = pytest.mark.skipif(
    not GATE.is_file() or shutil.which("bash") is None,
    reason="the gate script or bash is not present in this checkout",
)


def _extract(function_name: str) -> str:
    """Lift one shell function out of the gate script, verbatim."""
    source = GATE.read_text(encoding="utf-8")
    start = source.index(f"{function_name}() {{")
    return source[start : source.index("\n}\n", start) + 3]


def _run_case(summary_line: str, exit_code: int, ceiling: int = 5) -> tuple[int, int]:
    """Drive the REAL function with a stand-in phase. Returns (skipped, failed) counts."""
    harness = textwrap.dedent(f"""
        set -euo pipefail
        BLUE='' GREEN='' RED='' YELLOW='' NC=''
        INTEGRATION_SKIP_CEILING={ceiling}
        FAILED_PHASES=(); SKIPPED_PHASES=()
        {_extract("run_phase_watching_skips")}
        stand_in() {{ echo "{summary_line}"; return {exit_code}; }}
        run_phase_watching_skips "case" stand_in >/dev/null
        echo "${{#SKIPPED_PHASES[@]}} ${{#FAILED_PHASES[@]}}"
    """)
    result = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, f"harness failed: {result.stderr}"
    skipped, failed = result.stdout.strip().split()[-2:]
    return int(skipped), int(failed)


def test_a_mass_skipped_phase_is_not_measured():
    """The trap itself: exit 0 with 71 skips must not read as a pass."""
    skipped, failed = _run_case("34 passed, 71 skipped, 20 deselected in 99s", 0)
    assert (skipped, failed) == (1, 0), "a mass-skipped phase was counted as passed"


def test_a_healthy_phase_still_passes():
    """The control. Without it, "never pass" would satisfy the test above."""
    skipped, failed = _run_case("101 passed, 3 skipped, 20 deselected in 182s", 0)
    assert (skipped, failed) == (0, 0), "the measured healthy baseline was rejected"


def test_a_phase_with_no_skips_at_all_passes():
    """The summary line omits "skipped" entirely when nothing skipped."""
    skipped, failed = _run_case("12 passed in 2s", 0)
    assert (skipped, failed) == (0, 0)


def test_a_genuinely_failing_phase_is_still_recorded_as_failed():
    """⚠️ This is the case the guard originally got WRONG.

    ``"$@" | tee "$out" || true`` clobbers ``PIPESTATUS`` — it becomes the status
    of ``true`` — so a failing phase was recorded as neither failed nor skipped
    and the gate exited 0. A skip guard that swallows real failures is worse than
    no skip guard.
    """
    skipped, failed = _run_case("5 failed, 1 skipped in 3s", 1)
    assert (skipped, failed) == (0, 1), "a FAILING phase was not recorded as a failure"


def test_a_failing_phase_with_many_skips_is_a_failure_not_merely_unmeasured():
    """Failure outranks unmeasured: the skip branch only applies on exit 0."""
    skipped, failed = _run_case("2 failed, 80 skipped in 9s", 1)
    assert (skipped, failed) == (0, 1)


def test_the_ceiling_is_actually_consulted():
    """Guard on the guard: prove the threshold is read rather than hardcoded."""
    assert _run_case("1 passed, 7 skipped in 1s", 0, ceiling=5) == (1, 0)
    assert _run_case("1 passed, 7 skipped in 1s", 0, ceiling=50) == (0, 0)


def test_the_gate_refuses_the_phase_outright_when_a_service_is_down():
    """Belt and braces: the ports are probed before the phase runs at all.

    The ceiling alone would let the phase run for three minutes and then decline
    to trust it. Probing first says so immediately, and names what to start.
    """
    source = GATE.read_text(encoding="utf-8")
    assert "STACK_INCOMPLETE" in source, "the preflight no longer records unreachable services"

    probe_at = source.index("STACK_INCOMPLETE+=")
    dispatch_at = source.index('run_phase_watching_skips "Integration-marked tests"')
    assert probe_at < dispatch_at, (
        "the services must be probed BEFORE the integration phase is dispatched — "
        "otherwise the phase runs for minutes and only then declines to trust itself"
    )

    guard_at = source.index("if [ ${#STACK_INCOMPLETE[@]} -gt 0 ]")
    assert guard_at < dispatch_at, "the dispatch is not behind the reachability guard"
