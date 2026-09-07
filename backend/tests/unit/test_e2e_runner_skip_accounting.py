"""The e2e phase must ACCOUNT for its skips, the way the backend gate already does.

``scripts/e2e/run-e2e.sh`` ran three pytest phases with no ``-rs``, no ``--junitxml`` and no
skip ceiling, so a phase that skipped most of itself reported PASS. Measured across three runs
of the identical suite, same tree, same day (phase 1 | chat | visual skips):

    2026-09-06 19:58  standalone   41 | 24 | 8
    2026-09-06 20:50  --full       24 | 24 | 8
    2026-09-07 01:31  --full       25 |  3 | 6

21 chat tests silently stopped running between the last two runs and both were green. ``grep
SKIPPED`` on any of those logs returns nothing, because no phase ran with ``-rs``.

Driven against the REAL shell functions, extracted from the real script — the same idiom
``test_integration_gate_skip_ceiling.py`` uses for the backend gate — because a test that
re-implements the accounting would pass against a script that lost it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
E2E_RUNNER = REPO_ROOT / "scripts" / "e2e" / "run-e2e.sh"
DEV_TESTS = REPO_ROOT / "scripts" / "run-dev-tests.sh"
INTEGRATION_GATE = REPO_ROOT / "scripts" / "run-integration-tests.sh"

pytestmark = pytest.mark.skipif(
    not E2E_RUNNER.is_file() or not DEV_TESTS.is_file() or shutil.which("bash") is None,
    reason="the runner scripts or bash are not present in this checkout",
)


def _logical_lines(script: Path) -> list[str]:
    """Script text with backslash continuations joined, so one command is one line."""
    return script.read_text(encoding="utf-8").replace("\\\n", " ").splitlines()


def _pytest_invocations(script: Path) -> list[str]:
    """Every line that actually runs pytest (not a comment, not a docstring mention)."""
    return [
        line
        for line in _logical_lines(script)
        if "-m pytest" in line and not line.lstrip().startswith("#")
    ]


def _extract(script: Path, function_name: str) -> str:
    """Lift one shell function out of a script, verbatim."""
    source = script.read_text(encoding="utf-8")
    start = source.index(f"{function_name}() {{")
    return source[start : source.index("\n}\n", start) + 3]


def _declared_int(script: Path, name: str) -> int:
    """Read a `NAME=<int>` shell constant out of a script, or fail saying it is gone."""
    match = re.search(rf"^{re.escape(name)}=(\d+)\s*$", script.read_text(encoding="utf-8"), re.M)
    assert match, f"{name} is no longer declared as a plain integer in {script.name}"
    return int(match.group(1))


def _junit_xml(tmp_path: Path, *, skipped: int, name: str = "case") -> Path:
    path = tmp_path / f"{name}.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest" errors="0" failures="0" '
        f'skipped="{skipped}" tests="{skipped + 5}" time="1.0"/></testsuites>',
        encoding="utf-8",
    )
    return path


def _run_ceiling_case(xml: Path, *, rc: int, ceiling: int, whole_tree: bool = True) -> list[str]:
    """Drive the REAL enforce_skip_ceiling/phase_skipped. Returns the phases it recorded."""
    harness = textwrap.dedent(f"""
        set -euo pipefail
        GREEN='' RED='' YELLOW='' NC=''
        VENV_PY={sys.executable!r}
        WHOLE_TREE={"true" if whole_tree else "false"}
        {_extract(E2E_RUNNER, "phase_skipped")}
        {_extract(E2E_RUNNER, "enforce_skip_ceiling")}
        enforce_skip_ceiling "the phase" {rc} {str(xml)!r} {ceiling} >/dev/null 2>&1
        printf '%s\\n' "${{NOT_MEASURED_PHASES[@]}}"
    """)
    result = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, f"harness itself failed: {result.stderr}"
    return [line for line in result.stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------- the flags


def test_every_e2e_pytest_phase_records_skip_reasons_and_a_junit_report():
    """``-rs`` and ``--junitxml`` on every phase, or a skip has no recorded cause anywhere."""
    invocations = _pytest_invocations(E2E_RUNNER)
    assert len(invocations) >= 4, (
        f"expected the custom-args passthrough plus three phases, found {len(invocations)}: "
        f"{invocations}"
    )
    missing_reasons = [i for i in invocations if "SKIP_REASONS" not in i and "-rs" not in i]
    assert not missing_reasons, (
        f"pytest phases that would not print skip reasons: {missing_reasons}"
    )
    missing_xml = [i for i in invocations if "--junitxml" not in i]
    assert not missing_xml, f"pytest phases that write no junit report: {missing_xml}"


def test_skip_reasons_is_actually_rs():
    """Guard the guard: ``SKIP_REASONS`` satisfying the check above must really be ``-rs``."""
    source = E2E_RUNNER.read_text(encoding="utf-8")
    assert re.search(r"^SKIP_REASONS=\(-rs\)", source, re.MULTILINE), (
        "SKIP_REASONS is referenced by every phase but no longer expands to -rs"
    )


def test_each_phase_writes_its_own_junit_report():
    """One shared path would let the last phase overwrite the evidence for the first two."""
    targets = re.findall(r"--junitxml=\"([^\"]+)\"", E2E_RUNNER.read_text(encoding="utf-8"))
    assert len(targets) >= 4, f"expected one --junitxml per pytest invocation, got {targets}"
    assert len(set(targets)) == len(targets), f"two phases write the same junit path: {targets}"


def test_the_artifact_dir_is_overridable_so_a_caller_can_collect_the_reports():
    source = E2E_RUNNER.read_text(encoding="utf-8")
    assert 'E2E_ARTIFACT_DIR="${E2E_ARTIFACT_DIR:-' in source, (
        "the junit artifact dir is no longer overridable — run-dev-tests.sh points it at its "
        "own per-run report dir"
    )
    assert "export E2E_ARTIFACT_DIR=" in DEV_TESTS.read_text(encoding="utf-8"), (
        "run-dev-tests.sh no longer collects the e2e junit reports into its report dir"
    )


# --------------------------------------------------------------------------- the ceiling


def test_the_three_ceilings_are_declared_and_env_overridable():
    source = E2E_RUNNER.read_text(encoding="utf-8")
    for name in ("E2E_SKIP_CEILING", "E2E_CHAT_SKIP_CEILING", "E2E_VISUAL_SKIP_CEILING"):
        assert re.search(rf'^{name}="\${{{name}:-\d+}}"', source, re.MULTILINE), (
            f"{name} is missing, hardcoded, or no longer overridable from the environment"
        )


def test_a_mass_skipped_phase_is_not_measured(tmp_path):
    """The trap itself: the chat phase's 24 skips with 11 passes must not read as a pass."""
    recorded = _run_ceiling_case(_junit_xml(tmp_path, skipped=24), rc=0, ceiling=3)
    assert recorded == ["the phase (24 skipped > 3)"], (
        f"a phase that skipped 24 of its tests was counted as passed: {recorded}"
    )


def test_a_healthy_phase_still_passes(tmp_path):
    """The control. Without it, "never pass" would satisfy the test above."""
    assert _run_ceiling_case(_junit_xml(tmp_path, skipped=3), rc=0, ceiling=3) == []


def test_the_ceiling_is_actually_consulted(tmp_path):
    """Both directions, so an implementation ignoring the argument cannot pass."""
    xml = _junit_xml(tmp_path, skipped=8)
    assert _run_ceiling_case(xml, rc=0, ceiling=5) == ["the phase (8 skipped > 5)"]
    assert _run_ceiling_case(xml, rc=0, ceiling=50) == []


def test_a_failing_phase_is_a_failure_not_merely_unmeasured(tmp_path):
    """Failure outranks unmeasured — the ceiling branch applies only on exit 0."""
    assert _run_ceiling_case(_junit_xml(tmp_path, skipped=80), rc=1, ceiling=3) == []


def test_a_caller_selected_subset_is_not_held_to_the_whole_suites_ceiling(tmp_path):
    """``run-e2e-smoke.sh``'s four files hold a different, unknown set of gated tests."""
    assert (
        _run_ceiling_case(_junit_xml(tmp_path, skipped=40), rc=0, ceiling=3, whole_tree=False) == []
    )


def test_an_unreadable_report_is_not_measured_rather_than_zero_skips(tmp_path):
    """ "Could not count" must never be indistinguishable from "counted, and it was fine"."""
    recorded = _run_ceiling_case(tmp_path / "never-written.xml", rc=0, ceiling=3)
    assert recorded == ["the phase (no readable junit report)"], (
        f"a missing junit report was treated as a clean phase: {recorded}"
    )


def test_the_runner_exits_4_when_a_phase_was_not_measured():
    """The verdict has to reach an EXIT CODE, or run-dev-tests.sh cannot see it.

    Driven on the REAL tail block of the script, so a rewrite that keeps the warning and drops
    the exit code fails here — the exact defect the backend gate shipped for months.
    """
    source = E2E_RUNNER.read_text(encoding="utf-8")
    tail = source[source.index("# A real failure outranks a not-measured phase") :]
    # ⚠️ Read from the script, never spelled here. This harness used to define its own
    # `EXIT_NOT_MEASURED=4`, so nothing tied the number the runner EMITS to the number
    # run-dev-tests.sh READS — see test_the_not_measured_exit_code_is_the_same_number_everywhere.
    not_measured_code = _declared_int(E2E_RUNNER, "EXIT_NOT_MEASURED")

    def _verdict(*, failures: tuple[int, int, int], not_measured: list[str]) -> int:
        harness = (
            textwrap.dedent(f"""
                set -uo pipefail
                GREEN='' RED='' YELLOW='' NC=''
                EXIT_NOT_MEASURED={not_measured_code}
                E2E_ARTIFACT_DIR=/tmp
                status={failures[0]}; chat_status={failures[1]}; visual_status={failures[2]}
                NOT_MEASURED_PHASES=({" ".join(f'"{p}"' for p in not_measured)})
            """)
            + tail
        )
        return subprocess.run(
            ["bash", "-c", harness], capture_output=True, text=True, timeout=60, check=False
        ).returncode

    assert _verdict(failures=(0, 0, 0), not_measured=[]) == 0, "a clean run must still exit 0"
    assert _verdict(failures=(0, 0, 0), not_measured=["Phase 2 (-m chat)"]) == not_measured_code, (
        "a run whose only anomaly is a NOT MEASURED phase exited 0 — the false green this "
        "guard exists for"
    )
    assert _verdict(failures=(1, 0, 0), not_measured=[]) == 1, "a real failure must still exit 1"
    assert _verdict(failures=(1, 0, 0), not_measured=["Phase 2 (-m chat)"]) == 1, (
        "a failure alongside a not-measured phase must report the FAILURE, not NOT MEASURED"
    )


# ------------------------------------------------- the caller-selected passthrough path


def _passthrough_block() -> str:
    """The `$HAS_CUSTOM || WORKERS==0` branch, verbatim, up to its closing `fi`."""
    source = E2E_RUNNER.read_text(encoding="utf-8")
    start = source.index('if $HAS_CUSTOM || [[ "$WORKERS" == "0" ]]; then')
    return source[start : source.index("\nfi\n", start) + 4]


def _run_passthrough(pytest_exit: int, tmp_path: Path) -> int:
    """Drive the REAL passthrough block with a stand-in `python` exiting ``pytest_exit``."""
    stub = tmp_path / "fake-python"
    stub.write_text(f"#!/bin/bash\nexit {pytest_exit}\n", encoding="utf-8")
    stub.chmod(0o755)
    harness = (
        textwrap.dedent(f"""
            set -uo pipefail
            GREEN='' RED='' YELLOW='' NC=''
            EXIT_NOT_MEASURED={_declared_int(E2E_RUNNER, "EXIT_NOT_MEASURED")}
            VENV_PY={str(stub)!r}
            E2E_ARTIFACT_DIR={str(tmp_path)!r}
            HAS_CUSTOM=true
            WORKERS=0
            SKIP_REASONS=(-rs)
            ARGS=(backend/tests/e2e/test_thing.py)
        """)
        + _passthrough_block()
    )
    return subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=60, check=False
    ).returncode


def test_the_passthrough_path_does_not_report_a_usage_error_as_not_measured(tmp_path):
    """pytest's exit 4 means USAGE ERROR; 4 is this script's NOT MEASURED code.

    The passthrough branch used to ``exec`` pytest, so its raw code became the script's — and
    ``run-dev-tests.sh`` renders 4 as "this phase declined to be counted". A mistyped flag
    therefore read as *verified nothing* rather than as a failure: the green-ish reading of a
    broken invocation, on the one path that has no ``resolve_phase`` to map codes.

    Every other code must still propagate unchanged, or this fix would have traded one
    misreport for another. ``exec`` would fail this test by construction: the stand-in's exit
    code would replace the shell's.
    """
    assert _run_passthrough(0, tmp_path) == 0, "a clean passthrough run must still exit 0"
    assert _run_passthrough(1, tmp_path) == 1, "a real test failure must still be 1"
    assert _run_passthrough(5, tmp_path) == 5, (
        "'no tests collected' on a caller-chosen selection must still propagate — the caller "
        "chose the selection, so only it can say whether that is wrong"
    )
    assert _run_passthrough(4, tmp_path) == 1, (
        "pytest's usage error (4) reached the caller unchanged, where it means NOT MEASURED"
    )


# ------------------------------------------------------- the gate must SEE that exit code


def _run_phase_verdict(exit_code: int) -> str:
    """Drive run-dev-tests.sh's REAL run_phase with a stand-in exiting ``exit_code``.

    ``PHASE_NOT_MEASURED_EXIT`` is read out of the script rather than declared here, for the
    same reason as above: a harness that supplies its own copy of a constant proves the
    harness consistent, not the scripts.
    """
    harness = textwrap.dedent(f"""
        set -uo pipefail
        GREEN='' RED='' YELLOW='' NC=''
        REPORT_DIR=$(mktemp -d)
        PHASE_NOT_MEASURED_EXIT={_declared_int(DEV_TESTS, "PHASE_NOT_MEASURED_EXIT")}
        PHASE_NAMES=(); PHASE_STATUS=(); PHASE_LOGS=(); PHASE_SECONDS=()
        {_extract(DEV_TESTS, "run_phase")}
        stand_in() {{ echo "stand-in phase"; return {exit_code}; }}
        run_phase "e2e (run-e2e.sh, full suite)" stand_in >/dev/null
        echo "${{PHASE_STATUS[0]}}"
    """)
    result = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, f"harness itself failed: {result.stderr}"
    return result.stdout.strip().splitlines()[-1]


def test_the_e2e_phase_reaches_a_not_measured_verdict_in_the_gate():
    """run-e2e.sh's NOT MEASURED exit must render as NOT MEASURED, not PASS and not FAIL.

    This is the other half of the accounting: a ceiling that no caller reads is a warning
    nobody acts on. All three verdicts, because any one alone is satisfiable by a constant.
    """
    not_measured_code = _declared_int(E2E_RUNNER, "EXIT_NOT_MEASURED")
    assert _run_phase_verdict(0) == "PASS"
    assert _run_phase_verdict(not_measured_code).startswith("NOT MEASURED"), (
        "an e2e phase that declined to count itself was folded into PASS or FAIL"
    )
    assert _run_phase_verdict(1).startswith("FAIL")


def test_the_not_measured_exit_code_is_the_same_number_everywhere():
    """⚠️ M5. Three scripts have to agree on one number and nothing checked that they did.

    ``run-e2e.sh`` DECLARES ``EXIT_NOT_MEASURED``; ``run-integration-tests.sh`` exits the same
    code from its summary block; ``run-dev-tests.sh`` is the caller that has to recognise it.
    Until now this module defined its own private ``EXIT_NOT_MEASURED=4`` in a harness string,
    which pinned the harness to itself and nothing else — so changing run-e2e.sh's constant
    would have made a NOT MEASURED e2e phase render in the report as **FAIL, with every test
    green**, and every test here would still have passed.

    Also asserts the number is NOT run-dev-tests.sh's own ``EXIT_NOT_MEASURED`` (5). The two
    differ on purpose: under the repo-wide standard contract 4 already means *operator abort*,
    so this script must re-emit 5 at its own boundary. Collapsing them would make an honest
    "a phase verified nothing" print as "the leg reported an operator abort".
    """
    emitted = _declared_int(E2E_RUNNER, "EXIT_NOT_MEASURED")
    received = _declared_int(DEV_TESTS, "PHASE_NOT_MEASURED_EXIT")
    assert emitted == received, (
        f"scripts/e2e/run-e2e.sh exits {emitted} for NOT MEASURED but "
        f"scripts/run-dev-tests.sh only recognises {received} — a phase that declined to be "
        "counted would be reported as a FAILURE with every test green"
    )

    if INTEGRATION_GATE.is_file():
        gate = INTEGRATION_GATE.read_text(encoding="utf-8")
        summary = gate[gate.index("# --- Summary ---") :]
        assert re.search(rf"^\s*exit {emitted}\s*$", summary, re.M), (
            f"scripts/run-integration-tests.sh's summary block no longer exits {emitted} for a "
            "NOT MEASURED phase, so the backend phase and the e2e phase would disagree"
        )

    re_emitted = _declared_int(DEV_TESTS, "EXIT_NOT_MEASURED")
    assert re_emitted != received, (
        "run-dev-tests.sh now emits the same code it receives. Those are deliberately "
        "different: 4 means operator abort in the standard contract this script is invoked "
        "under, which is why it re-emits 5."
    )


def test_the_e2e_phase_is_dispatched_through_that_verdict_path():
    """The dispatch must go through run_phase — not a bare call whose code is discarded."""
    source = DEV_TESTS.read_text(encoding="utf-8")
    dispatch = source.index('scripts/e2e/run-e2e.sh"')
    preceding = source[:dispatch]
    assert preceding.rindex("run_phase ") > preceding.rindex("\nfi\n"), (
        "the full e2e suite is no longer dispatched through run_phase, so its exit code — "
        "including the NOT MEASURED 4 — is not recorded in the report"
    )


def test_the_not_measured_message_tells_the_truth_about_rs():
    """A2: the gate's NOT MEASURED advice claimed something that was FALSE.

    It said "every pytest phase runs with -rs; grep for SKIPPED" while all three e2e phases
    ran without it — advice that sends you grepping a log which cannot contain the string.

    The message now claims the narrower thing that is actually true: every phase that CAN
    report NOT MEASURED prints its skip reasons. That is the e2e suite's three phases plus
    the backend gate's ``run_phase_watching_skips`` dispatches — and only those, because only
    those have a ceiling to trip.

    ⚠️ Found while writing this and deliberately NOT covered by the assertion: the backend
    gate's "Model-vs-schema drift" and "Search quality harness" phases pass ``-o addopts=""``
    without ``-rs``, so their skips have no recorded reason either. They go through plain
    ``run_phase``, so they cannot produce this message — but adding ``-rs`` to both would be
    a strict improvement.
    """
    message = DEV_TESTS.read_text(encoding="utf-8")
    claim_start = message.index("NO PHASE FAILED, BUT ONE OR MORE VERIFIED NOTHING")
    claim = message[claim_start : message.index("else", claim_start)]
    if "-rs" not in claim:
        pytest.skip("the message no longer claims -rs, so there is nothing to keep true")

    accountable = list(_pytest_invocations(E2E_RUNNER))
    if INTEGRATION_GATE.is_file():
        accountable += [
            line
            for line in _logical_lines(INTEGRATION_GATE)
            if "run_phase_watching_skips " in line and not line.lstrip().startswith("#")
        ]
    assert len(accountable) >= 6, (
        f"expected the e2e phases plus the gate's skip-watched phases, found {accountable}"
    )
    offenders = [
        line.strip()[:120]
        for line in accountable
        if "SKIP_REASONS" not in line and "-rs" not in line
    ]
    assert not offenders, (
        "the NOT MEASURED message promises -rs on every phase that can report it, but these "
        f"would print no skip reasons: {offenders}"
    )
