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

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[3] / "scripts" / "run-integration-tests.sh"

pytestmark = pytest.mark.skipif(
    not GATE.is_file() or shutil.which("bash") is None,
    reason="the gate script or bash is not present in this checkout",
)

#: ⚠️ This harness is HERMETIC — it constructs the child's whole environment rather than
#: inheriting pytest's.
#:
#: It did not used to be, and that cost three red tests in the gate for a reason none of them
#: was about. `run_phase_watching_skips` took its ceiling from `PHASE_SKIP_CEILING`, which the
#: gate set as a command prefix on the function call — and **a prefix assignment on a shell
#: function is exported into that function's children**, so every `pytest` the gate ran carried
#: `PHASE_SKIP_CEILING=<that phase's ceiling>`. This module's `bash -c` then inherited it and
#: silently used 151 in place of the 5 each case declares.
#:
#: The gate no longer uses the prefix form (the ceiling is a positional parameter now), but a
#: harness that reads the ambient environment is one variable away from the same class of
#: false verdict. Whatever the script does, these cases now control every input they depend on.
_HERMETIC_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", tempfile.gettempdir()),
    "LC_ALL": "C",
}


def _extract(function_name: str) -> str:
    """Lift one shell function out of the gate script, verbatim."""
    source = GATE.read_text(encoding="utf-8")
    start = source.index(f"{function_name}() {{")
    return source[start : source.index("\n}\n", start) + 3]


def _bash(harness: str) -> subprocess.CompletedProcess[str]:
    """Run a harness under bash with a fully-declared environment (see _HERMETIC_ENV)."""
    return subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=_HERMETIC_ENV,
    )


def _harness(body: str) -> str:
    """The shared preamble: colours blanked, accumulators empty, the REAL function."""
    return textwrap.dedent(f"""
        set -euo pipefail
        BLUE='' GREEN='' RED='' YELLOW='' NC=''
        FAILED_PHASES=(); SKIPPED_PHASES=()
        {_extract("run_phase_watching_skips")}
    """) + textwrap.dedent(body)


def _run_case(summary_line: str, exit_code: int, ceiling: int = 5) -> tuple[int, int]:
    """Drive the REAL function with a stand-in phase. Returns (skipped, failed) counts.

    ``ceiling`` is passed the way the gate passes it: as the dispatch's SECOND POSITIONAL
    ARGUMENT. Each phase needs its own number — the GPU phase's legitimate residue is the
    container-only diarization suites, a different set from the integration phase's — and a
    positional parameter is the only way to say so that cannot also reach pytest's environment.
    """
    result = _bash(
        _harness(f"""
        stand_in() {{ echo "{summary_line}"; return {exit_code}; }}
        run_phase_watching_skips "case" {ceiling} stand_in >/dev/null
        echo "${{#SKIPPED_PHASES[@]}} ${{#FAILED_PHASES[@]}}"
    """)
    )
    assert result.returncode == 0, f"harness failed: {result.stderr}"
    skipped, failed = result.stdout.strip().split()[-2:]
    return int(skipped), int(failed)


def test_a_mass_skipped_phase_is_not_measured():
    """The trap itself: exit 0 with 71 skips must not read as a pass."""
    skipped, failed = _run_case("34 passed, 71 skipped, 20 deselected in 99s", 0)
    assert (skipped, failed) == (1, 0), "a mass-skipped phase was counted as passed"


def test_a_healthy_phase_still_passes():
    """The control, pinned to the ceiling BOUNDARY so it cannot pass vacuously.

    ⚠️ It used to assert only that 3 skips under the default ceiling passed — which **any**
    ceiling of 3 or more satisfies, including the ambient 151 the old harness was leaking.
    A control that a broken implementation also satisfies is not a control.

    So both sides of the boundary, with the ceiling stated: exactly-at-ceiling is a pass,
    one more is not. That pins the comparison as ``>`` rather than ``>=`` as well.
    """
    assert _run_case("101 passed, 3 skipped, 20 deselected in 182s", 0, ceiling=3) == (0, 0), (
        "a phase that skipped exactly its ceiling was refused"
    )
    assert _run_case("101 passed, 4 skipped, 20 deselected in 182s", 0, ceiling=3) == (1, 0), (
        "one skip PAST the ceiling still read as a pass — the ceiling is off by one, or the "
        "control above is passing for a reason other than the ceiling"
    )


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


def test_each_phase_carries_its_own_ceiling():
    """The GPU phase needs its own number, and must actually get it.

    Both directions, because either alone is satisfiable by an implementation that ignores the
    argument: the GPU phase's measured 18 skips are NOT MEASURED under 13 and a pass under 20.
    """
    assert _run_case("8 passed, 18 skipped in 165s", 0, ceiling=13) == (1, 0)
    assert _run_case("8 passed, 18 skipped in 165s", 0, ceiling=20) == (0, 0)


def test_a_missing_ceiling_argument_is_a_failure_not_a_silent_default():
    """⚠️ Replaces "no prefix falls back to the integration ceiling".

    That fallback is gone deliberately. It made the ceiling invisible at five of six call
    sites, and it is what let the ceiling be supplied as an environment prefix in the first
    place — the mechanism that exported it into pytest.

    Omitting it now shifts the command left by one argument, so the dispatcher would run the
    caller's ``$VENV_PY`` as if it were the title. It must refuse, loudly, and be recorded as
    a FAILURE: a misconfigured dispatcher that can still print a pass is the whole family of
    bug this file exists for.
    """
    result = _bash(
        _harness("""
        stand_in() { echo "1 passed in 1s"; return 0; }
        run_phase_watching_skips "case" stand_in >/dev/null
        echo "${#SKIPPED_PHASES[@]} ${#FAILED_PHASES[@]} ${FAILED_PHASES[0]:-none}"
    """)
    )
    assert result.returncode == 0, f"harness failed: {result.stderr}"
    skipped, failed, detail = result.stdout.strip().split(maxsplit=2)
    assert (skipped, failed) == ("0", "1"), (
        f"a dispatch with no ceiling argument was not recorded as a failure ({result.stdout!r})"
    )
    assert "ceiling" in detail, f"the failure does not name the cause: {detail!r}"


def test_the_ceiling_never_reaches_the_phases_own_environment():
    """⚠️ H1. The defect this whole rework is for, asserted directly.

    ``VAR=x some_function`` does not scope ``VAR`` to the call — bash exports it to every
    process the function starts. So ``PHASE_SKIP_CEILING=151 run_phase_watching_skips ...``
    ran **pytest** with ``PHASE_SKIP_CEILING=151``, and any child that reads that name (this
    module's own harness did) silently used the gate's number instead of its own.

    The stand-in reports what its environment actually contained, which is the only way to
    see it: nothing in the phase's OUTPUT differs between the two implementations.
    """
    result = _bash(
        _harness("""
        stand_in() {
            echo "leaked=$(env | grep -c '^PHASE_SKIP_CEILING=' || true)"
            echo "1 passed in 1s"
        }
        run_phase_watching_skips "case" 5 stand_in
    """)
    )
    assert result.returncode == 0, f"harness failed: {result.stderr}"
    assert "leaked=0" in result.stdout, (
        "the skip ceiling was exported into the phase's environment — it is being passed as a "
        "command prefix on a shell function again. Pass it positionally: "
        f"run_phase_watching_skips <title> <ceiling> <command...>. Output: {result.stdout!r}"
    )


def test_no_gate_phase_is_dispatched_with_an_environment_prefix():
    """The static half of the above, over the REAL script.

    The runtime test proves the current dispatcher does not leak; this proves no CALL SITE
    reintroduces the prefix form for some other variable. Any ``VAR=value run_phase...`` line
    sets that variable for the phase's whole process tree, which is almost never what the
    author of such a line means.
    """
    offenders = [
        line.strip()
        for line in GATE.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*=\S+\s+run_phase\w*\b", line)
    ]
    assert not offenders, (
        "these dispatches assign a variable as a command prefix on a shell function, which "
        f"EXPORTS it into every process the phase starts: {offenders}"
    )


def test_the_gate_exits_4_when_a_phase_was_not_measured():
    """⚠️ The defect P0-2 fixes: the summary said NOT MEASURED and then exited 0.

    ``run-dev-tests.sh`` recorded the backend phase PASS and ``scripts/release/60-test.sh``
    recorded ``integration-gate pass`` for a release, on a run whose 733-second integration
    phase had printed ``⊘ NOT MEASURED`` for itself. Printing a warning that no exit code
    carries is the same as not printing it.

    Driven on the REAL summary block, extracted from the script, so a rewrite that keeps the
    message and drops the exit code fails here.
    """
    source = GATE.read_text(encoding="utf-8")
    start = source.index("# --- Summary ---")
    summary_block = source[start:]

    def _verdict(failed: list[str], skipped: list[str]) -> int:
        harness = (
            textwrap.dedent(f"""
            set -uo pipefail
            BLUE='' GREEN='' RED='' YELLOW='' NC=''
            FAILED_PHASES=({" ".join(f'"{p}"' for p in failed)})
            SKIPPED_PHASES=({" ".join(f'"{p}"' for p in skipped)})
        """)
            + summary_block
        )
        return _bash(harness).returncode

    assert _verdict([], []) == 0, "a clean run must still exit 0"
    assert _verdict([], ["Integration-marked tests"]) == 4, (
        "a run whose only anomaly is a NOT MEASURED phase exited 0 — that is the false green "
        "this guard exists for"
    )
    assert _verdict(["Unit/API suite"], []) == 1, "a real failure must still exit 1"
    assert _verdict(["Unit/API suite"], ["GPU-marked tests"]) == 1, (
        "a failure alongside a not-measured phase must report the FAILURE (1), not 4"
    )


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
