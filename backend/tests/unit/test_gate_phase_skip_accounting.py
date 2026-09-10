"""Every pytest phase in the gate must be dispatched through ``run_phase_watching_skips``.

``scripts/run-integration-tests.sh`` has two dispatchers. ``run_phase`` prints ``✓ passed`` for
any exit 0. ``run_phase_watching_skips`` additionally refuses to call a phase passed when it
skipped past a documented ceiling — because pytest exits **0** on a run that skipped everything,
so a mass skip and a real pass are indistinguishable in an exit code.

Only two of six pytest phases used the second one. Measured on the 2026-09-07 gate's own junit
artifacts:

===========================  =======  ============================  ==========
phase                        tests    dispatcher                    ceiling
===========================  =======  ============================  ==========
Unit/API suite                13,622  ``run_phase``                 **none**
Gated security, FIPS off         394  ``run_phase``                 **none**
Gated security, FIPS on          394  ``run_phase``                 **none**
Integration-marked               177  ``run_phase_watching_skips``  7
GPU-marked                        20  ``run_phase_watching_skips``  12
Model-vs-schema drift              3  ``run_phase``                 **none**
===========================  =======  ============================  ==========

**99% of the gate by test count could mass-skip and still print ``✓ Unit/API suite passed``.**

``test_integration_gate_skip_ceiling.py`` already covers the ceiling *mechanism* — that
``run_phase_watching_skips`` counts correctly, that ``|| true`` would clobber ``PIPESTATUS``.
What nothing covered was **which phases use it**, which is why five sixths of the gate did not.
A mechanism no caller invokes is worth exactly as much as no mechanism.

Static: the failure is invisible at runtime, since a phase with no ceiling reports a pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE = REPO_ROOT / "scripts" / "run-integration-tests.sh"

pytestmark = pytest.mark.skipif(
    not GATE.is_file(), reason="scripts/run-integration-tests.sh not in this checkout"
)

#: A phase dispatch, with any `VAR=value` command prefixes in front of it.
#:
#: The prefix group is still captured even though no dispatch may use one any more — that is
#: what `test_no_dispatch_supplies_its_ceiling_as_an_environment_prefix` checks. A prefix
#: assignment on a shell FUNCTION is exported into every process the function starts, so
#: `PHASE_SKIP_CEILING=151 run_phase_watching_skips ...` ran pytest with that variable set.
_DISPATCH = re.compile(
    r"^\s*(?P<prefix>(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*)"
    r"(?P<fn>run_phase_watching_skips|run_phase)\b"
    r"(?P<rest>.*)$"
)
#: `run_phase_watching_skips <title> <ceiling> <command...>`: the ceiling is the second
#: POSITIONAL argument. Accepted spellings are a `*_SKIP_CEILING` variable reference (every
#: real call site — the derivation is written beside the constant) or a bare integer (the
#: self-test harnesses). Anything else means the argument was dropped and the whole command
#: shifted left by one.
_CEILING_ARG = re.compile(
    r'^\s*"[^"]*"\s+(?:"?\$\{?(?P<var>[A-Za-z_][A-Za-z0-9_]*)\}?"?|(?P<literal>\d+))(?:\s|$)'
)


class Phase:
    """One dispatch and the lines belonging to it, up to the next dispatch."""

    def __init__(self, fn: str, prefix: str, rest: str, title: str, lineno: int) -> None:
        self.fn = fn
        self.prefix = prefix
        #: Everything after the function name on the dispatch's own (continuation-folded)
        #: line — i.e. its positional arguments, which is where the ceiling now lives.
        self.rest = rest
        self.title = title
        self.lineno = lineno
        self.lines: list[str] = []

    @property
    def body(self) -> str:
        return "\n".join(self.lines)

    @property
    def runs_pytest(self) -> bool:
        """Does this phase EXECUTE tests?

        ``--collect-only`` is excluded on purpose: the collection-determinism phase runs pytest
        twice and asserts on the ids, never running a test, so a skip ceiling is meaningless
        for it — and it carries its own non-vacuity check (both collections must be non-empty).
        """
        return any("-m pytest" in line and "--collect-only" not in line for line in self.lines)

    @property
    def has_ceiling(self) -> bool:
        """Is a skip ceiling supplied as the dispatch's second positional argument?

        ⚠️ It used to be enough for `PHASE_SKIP_CEILING=` to appear as a command PREFIX, or
        for the default ceiling variable to appear anywhere in the phase's body. Both were
        wrong: the prefix form leaks the value into pytest's environment, and "the variable
        is mentioned somewhere below" is satisfied by a comment.
        """
        if self.fn != "run_phase_watching_skips":
            return False
        match = _CEILING_ARG.match(self.rest)
        if not match:
            return False
        if match.group("literal") is not None:
            return True
        var = match.group("var")
        return var is not None and var.endswith("SKIP_CEILING")

    @property
    def reports_skip_reasons(self) -> bool:
        return "SKIP_REASONS" in self.body or bool(re.search(r"(?<![\w-])-rs\b", self.body))

    def __repr__(self) -> str:  # pragma: no cover - assertion messages only
        return f"{self.title!r} (line {self.lineno}, via {self.fn})"


def _logical_lines(source: str) -> list[tuple[int, str]]:
    """Fold backslash continuations, keeping the line number the statement STARTED on.

    Without this a dispatch written as ``PHASE_SKIP_CEILING=x \\`` / ``    run_phase_... "T" \\``
    parses as a prefix-less call and reads as a phase with no ceiling — a false positive that
    would push an author toward putting a 110-character command on one line to satisfy a
    linter, which is the wrong lesson.
    """
    folded: list[tuple[int, str]] = []
    buf = ""
    start = 0
    for lineno, raw in enumerate(source.splitlines(), start=1):
        if not buf:
            start = lineno
        if raw.endswith("\\"):
            buf += raw[:-1]
            continue
        folded.append((start, buf + raw))
        buf = ""
    if buf:
        folded.append((start, buf))
    return folded


def _parse_phases(source: str) -> list[Phase]:
    phases: list[Phase] = []
    current: Phase | None = None
    for lineno, raw in _logical_lines(source):
        if raw.lstrip().startswith("#"):
            continue
        match = _DISPATCH.match(raw)
        if match:
            rest = match.group("rest")
            title_match = re.search(r'"([^"]*)"', rest)
            current = Phase(
                fn=match.group("fn"),
                prefix=match.group("prefix"),
                rest=rest,
                title=title_match.group(1) if title_match else rest.strip(),
                lineno=lineno,
            )
            current.lines.append(rest)
            phases.append(current)
        elif current is not None:
            current.lines.append(raw)
    return phases


def _gate_phases() -> list[Phase]:
    return _parse_phases(GATE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------
# Non-vacuity. Everything below filters this list; an empty parse passes everything.
# --------------------------------------------------------------------------------------


def test_the_parser_finds_the_phases_that_are_there():
    phases = _gate_phases()
    assert len(phases) >= 8, f"only {len(phases)} phases parsed out of the gate script"
    pytest_phases = [p for p in phases if p.runs_pytest]
    assert len(pytest_phases) >= 5, (
        f"only {len(pytest_phases)} pytest-executing phases found: {pytest_phases}. The gate "
        "has at least five; a parser that finds none passes every check below."
    )
    # ...and the exclusion is real, not an empty branch: the collection-determinism phase runs
    # pytest and must NOT be required to carry a skip ceiling.
    collect_only = [p for p in phases if not p.runs_pytest and "--collect-only" in p.body]
    assert collect_only, (
        "no --collect-only phase found — the exclusion below is untested, so a future phase "
        "could be silently exempted by it"
    )


def test_the_ceiling_mechanism_is_actually_consulted():
    """A ceiling argument no dispatcher reads is decoration.

    The runtime behaviour is `test_integration_gate_skip_ceiling.py`'s job; this only pins
    that the dispatcher still TAKES the ceiling positionally and compares against it, so the
    static `has_ceiling` check below is checking something real.
    """
    source = GATE.read_text(encoding="utf-8")
    body = source.split("run_phase_watching_skips() {", 1)
    assert len(body) == 2, "run_phase_watching_skips is not defined in the gate script"
    fn = body[1].split("\n}", 1)[0]
    assert re.search(r"local\s+title=\$1\s+ceiling=\$2", fn), (
        "the dispatcher no longer takes <title> <ceiling> as its first two positional "
        "arguments. If the ceiling moved back to an environment prefix, note that a prefix "
        "on a shell FUNCTION is exported into every process the phase starts."
    )
    assert re.search(r"\bskipped\b.*>.*\bceiling\b|\bceiling\b.*<.*\bskipped\b", fn), (
        "the dispatcher no longer compares the skip count against the ceiling"
    )


# --------------------------------------------------------------------------------------
# The checks.
# --------------------------------------------------------------------------------------


def test_every_pytest_phase_watches_its_skips():
    offenders = [p for p in _gate_phases() if p.runs_pytest and p.fn != "run_phase_watching_skips"]
    assert not offenders, (
        f"these pytest phases go through run_phase, which has no skip ceiling: {offenders}. "
        "pytest exits 0 on a run that skipped everything, so such a phase prints "
        "'✓ <name> passed' having verified nothing — the exact state the Unit/API phase "
        "(13,622 tests, 99% of the gate) was in. Dispatch it through "
        "run_phase_watching_skips with a ceiling DERIVED from a measurement, and write the "
        "derivation beside the constant the way INTEGRATION_SKIP_CEILING does."
    )


def test_every_pytest_phase_has_a_ceiling():
    offenders = [p for p in _gate_phases() if p.runs_pytest and not p.has_ceiling]
    assert not offenders, (
        f"these pytest phases carry no skip ceiling: {offenders}. Pass it as the dispatch's "
        "SECOND POSITIONAL argument — run_phase_watching_skips <title> <ceiling> "
        "<command...> — with the measurement it came from written beside the constant. Never "
        "raise one to make a phase pass, and never supply it as a `VAR=x` command prefix "
        "(see the next test)."
    )


def test_no_dispatch_supplies_its_ceiling_as_an_environment_prefix():
    """⚠️ H1: `VAR=x <shell function>` EXPORTS `VAR` into that function's child processes.

    It is not scoped to the call, the way it is for an external command:

        $ bash -c 'f(){ env | grep -c "^LEAK="; }; LEAK=1 f; echo "after:[${LEAK:-unset}]"'
        1
        after:[unset]

    So `PHASE_SKIP_CEILING=151 run_phase_watching_skips ... pytest ...` ran **pytest** with
    `PHASE_SKIP_CEILING=151` in its environment — which reached this gate's own self-test,
    whose `bash -c` harness then used 151 in place of the 5 it declares, and turned three of
    its cases red for a reason none of them was about.

    Nothing about the prefix form is specific to the ceiling, so this bans the shape rather
    than the variable: a phase's environment must be built deliberately (`env VAR=x ...` as
    part of the COMMAND, which the FIPS and drift phases both do), not inherited by accident.
    """
    offenders = [p for p in _gate_phases() if p.prefix.strip()]
    assert not offenders, (
        "these dispatches carry a `VAR=value` command prefix, which exports the variable "
        "into every process the phase starts: "
        + ", ".join(f"{p!r} prefix={p.prefix.strip()!r}" for p in offenders)
        + ". Pass a ceiling positionally; put any real environment change in the command "
        "itself with `env VAR=value ...`."
    )


def test_every_pytest_phase_reports_why_it_skipped():
    """`-rs`, or the skips it counts against the ceiling have no recorded cause.

    The 2026-09-06 gate's 21 + 18 + 78 + 56 skips had no attribution anywhere and diagnosing
    them meant re-running a 733-second phase by hand. Two phases still passed ``-o addopts=""``
    — which drops pyproject's flags — without restoring it.
    """
    offenders = [p for p in _gate_phases() if p.runs_pytest and not p.reports_skip_reasons]
    assert not offenders, (
        f"these pytest phases do not pass -rs: {offenders}. A phase can then trip its ceiling "
        'and report NOT MEASURED with nothing in the log saying why. Add "${SKIP_REASONS[@]}".'
    )


# --------------------------------------------------------------------------------------
# Guard the guard.
# --------------------------------------------------------------------------------------


_FIXTURE_BAD = """
run_phase "Unit/API suite" "$VENV_PY" -m pytest tests/ \\
    --junitxml="$DIR/unit.xml"
"""

_FIXTURE_GOOD = """
run_phase_watching_skips "Unit/API suite" "$UNIT_SKIP_CEILING" \\
    "$VENV_PY" -m pytest tests/ "${SKIP_REASONS[@]}" \\
    --junitxml="$DIR/unit.xml"
"""

#: The ceiling argument dropped: the command shifts left by one, so `$VENV_PY` lands where the
#: ceiling belongs. Reads as a correctly-dispatched phase to everything except `has_ceiling`.
_FIXTURE_NO_CEILING = """
run_phase_watching_skips "Unit/API suite" \\
    "$VENV_PY" -m pytest tests/ "${SKIP_REASONS[@]}" \\
    --junitxml="$DIR/unit.xml"
"""

#: The pre-H1 shape. Correct dispatcher, correct-looking ceiling — and it sets
#: PHASE_SKIP_CEILING for pytest and everything else the phase starts.
_FIXTURE_ENV_PREFIX = """
PHASE_SKIP_CEILING="$UNIT_SKIP_CEILING" run_phase_watching_skips "Unit/API suite" \\
    "$VENV_PY" -m pytest tests/ "${SKIP_REASONS[@]}"
"""

_FIXTURE_NON_PYTEST = """
run_phase "Dependency parity: venv vs container" \\
    "$PROJECT_ROOT/scripts/check-dependency-parity.sh"
"""

_FIXTURE_COLLECT_ONLY = """
run_phase "Collection determinism (two processes, same test ids)" \\
    "$VENV_PY" -m pytest --collect-only -q -o addopts=
"""

#: The ceiling on its own continued line — how the longest dispatch is actually written.
#: `_logical_lines` must fold continuations before the arguments are parsed, or a phase that
#: HAS a ceiling reads as having none.
_FIXTURE_GOOD_WRAPPED = """
    run_phase_watching_skips "Search quality harness (corpus-dependent)" \\
        "$SEARCH_QUALITY_SKIP_CEILING" \\
        env RUN_SEARCH_QUALITY_TESTS=true "$VENV_PY" -m pytest tests/test_search_quality.py \\
        -o addopts="" -q --tb=short "${SKIP_REASONS[@]}"
"""


def test_a_pytest_phase_on_the_wrong_dispatcher_is_detected():
    phases = _parse_phases(_FIXTURE_BAD)
    assert len(phases) == 1
    assert phases[0].runs_pytest
    assert phases[0].fn == "run_phase"
    assert not phases[0].has_ceiling
    assert not phases[0].reports_skip_reasons


def test_a_correctly_dispatched_phase_is_clean():
    phases = _parse_phases(_FIXTURE_GOOD)
    assert len(phases) == 1
    assert phases[0].runs_pytest
    assert phases[0].fn == "run_phase_watching_skips"
    assert phases[0].has_ceiling
    assert phases[0].reports_skip_reasons


def test_a_dispatch_whose_ceiling_is_on_a_continued_line_is_still_seen():
    phases = _parse_phases(_FIXTURE_GOOD_WRAPPED)
    assert len(phases) == 1
    assert phases[0].runs_pytest
    assert phases[0].fn == "run_phase_watching_skips"
    assert phases[0].has_ceiling, "backslash continuation must not hide the ceiling argument"
    assert phases[0].reports_skip_reasons


def test_a_dropped_ceiling_argument_is_detected():
    """The failure the positional form makes possible, and the one `has_ceiling` must catch.

    Everything else about this dispatch is correct — right dispatcher, `-rs`, a junit report —
    so only the argument check can see it. Without this case, a `has_ceiling` that returned
    True for every `run_phase_watching_skips` call would pass the whole module.
    """
    phases = _parse_phases(_FIXTURE_NO_CEILING)
    assert len(phases) == 1
    assert phases[0].fn == "run_phase_watching_skips"
    assert phases[0].runs_pytest
    assert not phases[0].has_ceiling, (
        "a dispatch whose second argument is the COMMAND, not a ceiling, was accepted"
    )


def test_the_environment_prefix_form_is_detected():
    """Must-fire case for the H1 ban: the exact shape the gate shipped with."""
    phases = _parse_phases(_FIXTURE_ENV_PREFIX)
    assert len(phases) == 1
    assert phases[0].prefix.strip() == 'PHASE_SKIP_CEILING="$UNIT_SKIP_CEILING"'
    assert not phases[0].has_ceiling, (
        "a ceiling supplied as an environment prefix must not count as a ceiling — it is "
        "exported into the phase's children instead of being scoped to the call"
    )


def test_a_non_pytest_phase_is_not_required_to_have_a_ceiling():
    """`run_phase` is still correct for a phase that runs no tests — most of the gate."""
    phases = _parse_phases(_FIXTURE_NON_PYTEST)
    assert len(phases) == 1
    assert not phases[0].runs_pytest


def test_a_collect_only_phase_is_not_required_to_have_a_ceiling():
    phases = _parse_phases(_FIXTURE_COLLECT_ONLY)
    assert len(phases) == 1
    assert not phases[0].runs_pytest


def test_a_commented_out_dispatch_is_not_parsed_as_a_phase():
    """Otherwise the historical notes in the gate's header would each read as a violation."""
    assert _parse_phases('# run_phase "Old phase" "$VENV_PY" -m pytest tests/\n') == []
