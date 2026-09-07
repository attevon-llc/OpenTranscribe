"""Every ``RUN_*`` gate a gate script hands to pytest must be read by an actual test.

``scripts/run-integration-tests.sh`` exported seven of them —
``RUN_PKI_TESTS RUN_MFA_TESTS RUN_LLM_TESTS RUN_FEDRAMP_TESTS RUN_FIPS_TESTS
RUN_AUTH_CONFIG_TESTS RUN_ADVANCED_ADMIN_TESTS`` — and **no test read any of them**. The
module-level ``skipif`` gates had been removed from all eight security suites (each now opens
``# Runs by DEFAULT. This module was gated behind RUN_<X>_TESTS with the reason ...``) and the
``GATES=(...)`` array was left behind. So a phase called "Gated security suites" set seven
variables that changed nothing, and its FIPS-off half re-ran 394 tests the Unit/API phase had
already run.

**Its predecessor, ``test_gated_files_all_have_gates.py``, could not catch that.** It asked
``if not any(gate in text for gate in gates)`` — a plain substring match over the whole file,
comments and docstrings included — and every one of those eight files still *mentions* its dead
variable in the comment quoted above. It passed, describing a mechanism that no longer existed:
a test that cannot fail, inside the file written to prevent tests that cannot fail.

So this module inverts the question and makes the match structural:

* the **claim** side is parsed out of the shell scripts (every ``RUN_x=<bool>`` that reaches a
  subprocess), and
* the **reality** side is resolved by **AST**, not by substring — a name only counts when it is
  the value of a call argument, a subscript index, or an assignment. Prose cannot satisfy it.

Static, and about shell scripts, so it belongs in the fast unit suite: at runtime the failure is
invisible, because a variable nothing reads produces three green runs that look like three green
runs.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
BACKEND_TESTS = REPO_ROOT / "backend" / "tests"

pytestmark = pytest.mark.skipif(
    not SCRIPTS.is_dir(), reason="scripts/ not present in this checkout"
)

#: A `RUN_*` variable set to a boolean-ish literal. `RUN_LOG="$OUT_DIR/..."` is a shell local
#: with an unrelated name and must not be mistaken for a test gate, which is why the VALUE is
#: part of the pattern.
_GATE_ASSIGNMENT = re.compile(r"\b(RUN_[A-Z0-9_]+)=(?:true|1|yes|on)\b", re.IGNORECASE)
_VAR_NAME = re.compile(r"RUN_[A-Z0-9_]+\Z")
#: `--full)` / `start)` — a `case` label, not a command.
_CASE_LABEL = re.compile(r"^\s*[^\s()]*\)\s*")

#: Files whose `RUN_*` string literals are DATA, not gates. Each needs a written reason, and a
#: stale entry fails (`test_the_non_consumer_exemptions_are_all_real`) so an exemption cannot
#: outlive its subject.
_NOT_A_GATE_CONSUMER: dict[str, str] = {
    "unit/test_env_gate.py": (
        "It unit-tests the gate PARSER, so its arguments are sample variable names "
        "(`gate_enabled('RUN_FIPS_TESTS')` with a monkeypatched env), not a decision about "
        "whether a suite runs. Counting them made RUN_FIPS_TESTS look live while the suite it "
        "used to gate had run unconditionally for months — the exact defect this module exists "
        "to catch, hidden by the helper's own test."
    ),
    "unit/test_gate_run_env_vars_are_live.py": (
        "This file. Its fixtures name dead variables on purpose; a detector that reads its own "
        "must-fire case as evidence is calibrated to itself."
    ),
}

#: Known-dead gates in scripts this change does not own. Keyed `<script>::<VAR>` with a
#: mandatory reason; a stale entry FAILS, so the exemption dies with the offender rather than
#: outliving it (same contract as `test_shell_expansion_guards.py`'s `_ALLOWLIST`).
_PENDING: dict[str, str] = {
    **{
        f"run-mutation-tests.sh::{var}": (
            "BACKLOG — the same dead `GATES=(...)` array, copied. Its header still says 'three "
            "of the selected test files are behind a module-level skipif and a skipped test "
            "kills no mutant', which stopped being true when the gates were removed from those "
            "files. Harmless (it sets a variable nobody reads) but it is the stale justification "
            "that would make someone re-add the array to run-integration-tests.sh. Owned by the "
            "mutation harness, not by the change that added this guard."
        )
        for var in (
            "RUN_PKI_TESTS",
            "RUN_MFA_TESTS",
            "RUN_LLM_TESTS",
            "RUN_FEDRAMP_TESTS",
            "RUN_FIPS_TESTS",
            "RUN_AUTH_CONFIG_TESTS",
            "RUN_ADVANCED_ADMIN_TESTS",
        )
    },
}


#: `GATES=(` / `SUITES+=(` — an array assignment whose `)` is on a LATER line.
_ARRAY_OPEN = re.compile(r"(?:^|[\s;&|])([A-Za-z_][A-Za-z0-9_]*)\+?=\(")


def _segments(line: str) -> list[list[str]]:
    """Split a shell line into command segments, each tokenised on whitespace."""
    if line.lstrip().startswith("#"):
        return []
    parts = re.split(r"[;&|]+", line)
    return [_CASE_LABEL.sub("", part, count=1).split() for part in parts]


def _logical_lines(text: str) -> list[str]:
    """Join a multi-line ``NAME=( ... )`` array assignment into ONE line.

    ⚠️ Without this the tokeniser is blind to an array written **one variable per line**:

        GATES=(
            RUN_PKI_TESTS=true
            RUN_MFA_TESTS=true
        )

    Each of those middle lines tokenises to a SINGLE token, and `_claimed_gates_from_text`
    skips single-token lines on purpose — a bare `RUN_GPU=true` is a shell-local phase flag
    that reaches no test process. So every gate in the array read as a local, and the whole
    detector reported nothing.

    That was not hypothetical safety margin: the array this module exists to catch survives
    today in `run-mutation-tests.sh`, and it is only *visible* because that file happens to
    write 2-3 variables per line. Reformatting it — the sort of thing a formatter or a tidy-up
    does without comment — would have silently blinded all seven dead-gate detections, and
    `test_the_pending_exemptions_are_all_real` would then have declared the exemptions stale
    and invited someone to delete them. A parser that matches nothing reports a clean tree.
    """
    out: list[str] = []
    pending: list[str] | None = None
    for raw in text.splitlines():
        if pending is not None:
            pending.append(raw.strip())
            if ")" in raw:
                out.append(" ".join(pending))
                pending = None
            continue
        match = _ARRAY_OPEN.search(raw)
        # Only an array whose closing paren is on a LATER line needs joining; anything
        # balanced on one line is already correct, and `case` arms / `$( )` must not be
        # mistaken for an unterminated array.
        if match and ")" not in raw[match.end() :]:
            pending = [raw.strip()]
            continue
        out.append(raw)
    if pending is not None:  # unterminated array: scan what we have rather than drop it
        out.append(" ".join(pending))
    return out


def _claimed_gates_from_text(text: str) -> set[str]:
    """`RUN_*` gates this shell source hands to a subprocess.

    A bare ``RUN_GPU=true`` alone on its line is a shell-local flag — it never reaches a pytest
    process, so it is not a claim about any test. Anything else (an ``env`` prefix, an ``export``,
    a command prefix, or an element of an array destined to be splatted into ``env``) is.
    """
    claimed: set[str] = set()
    for line in _logical_lines(text):
        for tokens in _segments(line):
            if len(tokens) <= 1:
                continue  # nothing to run: a local assignment
            for token in tokens:
                match = _GATE_ASSIGNMENT.search(token)
                if match:
                    claimed.add(match.group(1))
    return claimed


def _claimed_gates(script: Path) -> set[str]:
    return _claimed_gates_from_text(script.read_text(encoding="utf-8"))


#: A `RUN_*: 'true'` key inside a GitHub Actions `env:` mapping. Workflow `env` is exported
#: into the step's process exactly as `export` is, so it is the same claim in a different
#: syntax — and the seven dead gates lived in BOTH halves. Deliberately regex, not YAML: this
#: module already parses shell by hand, the shape is unambiguous, and a `pyyaml` import would
#: put a third-party dependency in the fast unit suite for four lines of matching.
_YAML_GATE = re.compile(r"^\s*(RUN_[A-Z0-9_]+)\s*:\s*['\"]?(?:true|1|yes|on)['\"]?\s*$", re.I)


def _claimed_gates_from_yaml(text: str) -> set[str]:
    return {
        match.group(1)
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
        for match in [_YAML_GATE.match(line)]
        if match
    }


def _claimed_gates_in(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yml", ".yaml"):
        return _claimed_gates_from_yaml(text)
    return _claimed_gates_from_text(text)


def _live_reads(source: str) -> set[str]:
    """`RUN_*` names this Python source READS — resolved by AST, never by substring.

    A name counts when it is a call argument (``gate_enabled("RUN_X")``,
    ``os.environ.get("RUN_X", "")``), a subscript index (``os.environ["RUN_X"]``), or the value
    of an assignment (``_GATE = "RUN_X"``, used indirectly). A mention in a comment or a
    docstring is prose and counts for nothing — which is the whole point, since every dead gate
    in this repo is still named in the comment that recorded its removal.
    """
    found: set[str] = set()

    def maybe(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _VAR_NAME.match(node.value):
                found.add(node.value)

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            for arg in node.args:
                maybe(arg)
            for kw in node.keywords:
                maybe(kw.value)
        elif isinstance(node, ast.Subscript):
            maybe(node.slice)
        elif isinstance(node, ast.Assign):
            maybe(node.value)
    return found


def _all_live_reads() -> set[str]:
    live: set[str] = set()
    for path in sorted(BACKEND_TESTS.rglob("*.py")):
        rel = path.relative_to(BACKEND_TESTS).as_posix()
        if rel in _NOT_A_GATE_CONSUMER:
            continue
        try:
            live |= _live_reads(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken test file fails elsewhere, loudly
            continue
    return live


def _gate_scripts() -> list[Path]:
    """Every source that can hand a `RUN_*` to a test process.

    ⚠️ The workflows belong here, and their absence was not an oversight anyone had reasoned
    about: `.github/workflows/pre-commit.yml` exported the SAME seven dead variables as
    `run-integration-tests.sh`, in a step named "Run security-gated suites". The local half
    was removed and the CI twin was not, so the mechanism this module exists to describe went
    on being claimed by the one gate contributors actually see on a PR.
    """
    return sorted(SCRIPTS.rglob("*.sh")) + sorted(WORKFLOWS.glob("*.y*ml"))


# --------------------------------------------------------------------------------------
# Non-vacuity. Everything below iterates these; an empty parse would pass everything.
# --------------------------------------------------------------------------------------


def test_the_scan_finds_scripts_and_gates_at_all():
    scripts = _gate_scripts()
    assert len(scripts) >= 50, (
        f"only {len(scripts)} sources found (there are ~100 shell scripts under scripts/ "
        "plus the workflows) — the recursive glob has stopped reaching most of the tree, "
        "and the check below would pass by examining almost nothing"
    )
    assert any(p.parent == WORKFLOWS for p in scripts), (
        "no GitHub Actions workflow reached the scan. CI exports these variables too, and "
        "its copy of the dead seven outlived the local one precisely because nothing "
        "looked there."
    )
    claimed = {var for s in scripts for var in _claimed_gates_in(s)}
    #: The two the gate script sets ITSELF, so this fails if the parser stops matching the
    #: exact shape it exists to read. A parser that matches nothing reports zero dead gates,
    #: which is indistinguishable from a clean tree.
    assert {"RUN_SCHEMA_DRIFT_TESTS", "RUN_SEARCH_QUALITY_TESTS"} <= claimed, (
        f"the shell parser no longer sees run-integration-tests.sh's own gates. Found: "
        f"{sorted(claimed)}"
    )


def test_the_live_read_scan_finds_something():
    live = _all_live_reads()
    assert live, (
        "no live RUN_* read found anywhere under backend/tests — the AST resolver has stopped "
        "matching, and a resolver that matches nothing marks every gate dead"
    )
    # The gates that are genuinely live today, so a regression in the resolver is loud.
    assert "RUN_SCHEMA_DRIFT_TESTS" in live
    assert "RUN_SEARCH_QUALITY_TESTS" in live


# --------------------------------------------------------------------------------------
# The check.
# --------------------------------------------------------------------------------------


def test_every_run_gate_a_script_sets_is_read_by_some_test():
    live = _all_live_reads()
    dead: list[str] = []
    for script in _gate_scripts():
        for var in sorted(_claimed_gates_in(script)):
            if var in live:
                continue
            key = f"{script.name}::{var}"
            if key in _PENDING:
                continue
            dead.append(f"{script.relative_to(REPO_ROOT)} sets {var}")

    assert not dead, (
        "these scripts hand a RUN_* variable to a test process that NO test reads: "
        f"{dead}. Setting a variable nothing reads is not coverage — it is a phase name "
        "describing a mechanism that no longer exists. Either restore the gate in the suite "
        "it is supposed to unlock, or delete the assignment. Do NOT satisfy this by mentioning "
        "the name in a comment: the reality side is resolved by AST, so prose cannot pass it."
    )


def test_the_pending_exemptions_are_all_real():
    """A stale exemption must fail, or the file can only grow."""
    live = _all_live_reads()
    by_key = {
        f"{script.name}::{var}"
        for script in _gate_scripts()
        for var in _claimed_gates_in(script)
        if var not in live
    }
    stale = sorted(set(_PENDING) - by_key)
    assert not stale, (
        f"these _PENDING entries no longer describe a real finding: {stale}. The dead gate was "
        "removed (or became live) — delete its line rather than leaving an exemption behind."
    )


def test_the_non_consumer_exemptions_are_all_real():
    missing = sorted(k for k in _NOT_A_GATE_CONSUMER if not (BACKEND_TESTS / k).is_file())
    assert not missing, (
        f"_NOT_A_GATE_CONSUMER names files that do not exist: {missing}. An exemption for a "
        "deleted file silently widens the scan's blind spot."
    )


# --------------------------------------------------------------------------------------
# Guard the guard. Both halves failed silently in the version this replaces.
# --------------------------------------------------------------------------------------


def test_a_comment_mention_is_not_a_live_read():
    """The exact shape that made the old guard vacuous.

    ``tests/test_mfa_security.py`` reads, verbatim, ``# Runs by DEFAULT. This module was gated
    behind RUN_MFA_TESTS with the reason ...`` — and nothing else. A substring detector calls
    that gated; this one must not.
    """
    var = "RUN_" + "MFA_TESTS"  # assembled, so the AST resolver cannot see it as a constant
    only_prose = (
        f'"""Set {var}=true to run these tests."""\n'
        f"# Runs by DEFAULT. This module was gated behind {var} with the reason below.\n"
        "def test_something():\n"
        "    assert True\n"
    )
    # The OLD detector — kept here as the negative control, so the improvement is measurable
    # rather than asserted.
    assert var in only_prose, "substring match (the old detector) accepts prose"
    assert _live_reads(only_prose) == set(), "AST resolver must not accept a comment or docstring"


def test_a_real_gate_is_recognised_in_each_spelling_the_tree_uses():
    var = "RUN_" + "SOMETHING_TESTS"
    for source in (
        f'pytestmark = pytest.mark.skipif(not gate_enabled("{var}"), reason="x")\n',
        f'if os.environ.get("{var}") != "1":\n    pass\n',
        f'x = os.environ["{var}"]\n',
        f'_GATE = "{var}"\n',
    ):
        assert _live_reads(source) == {var}, f"live read not recognised in: {source!r}"


def test_a_shell_local_flag_is_not_read_as_a_gate():
    """`RUN_GPU=true` in run-integration-tests.sh selects a phase; it reaches no test process."""
    assert _claimed_gates_from_text("RUN_GPU=true\n") == set()
    assert _claimed_gates_from_text("        RUN_BACKEND=true; RUN_E2E=true\n") == set()
    assert _claimed_gates_from_text("        --full) RUN_BACKEND=true ;;\n") == set()
    # ...but the same name handed to a subprocess IS a claim.
    assert _claimed_gates_from_text('env RUN_GPU=true "$PY" -m pytest x\n') == {"RUN_GPU"}
    assert _claimed_gates_from_text("export RUN_GPU=true\n") == {"RUN_GPU"}


def test_an_array_of_gates_is_read_as_a_claim():
    """The literal shape that was left behind, across a line continuation."""
    text = "GATES=(RUN_A_TESTS=true RUN_B_TESTS=true\n       RUN_C_TESTS=true RUN_D_TESTS=true)\n"
    assert _claimed_gates_from_text(text) == {
        "RUN_A_TESTS",
        "RUN_B_TESTS",
        "RUN_C_TESTS",
        "RUN_D_TESTS",
    }


def test_an_array_written_one_variable_per_line_is_still_a_claim():
    """The formatting the parser used to be blind to.

    Every middle line here is a single token, and a single token is how a shell-LOCAL flag
    looks — so the parser skipped all four and reported a clean tree. The surviving array in
    `run-mutation-tests.sh` is only visible today because it happens to be written 2-3 per
    line; reformatting it would have silently deleted all seven dead-gate findings and made
    `test_the_pending_exemptions_are_all_real` demand their exemptions be removed.
    """
    text = "GATES=(\n    RUN_A_TESTS=true\n    RUN_B_TESTS=true\n    RUN_C_TESTS=true\n)\n"
    assert _claimed_gates_from_text(text) == {"RUN_A_TESTS", "RUN_B_TESTS", "RUN_C_TESTS"}

    # ...and the closing paren sharing the last element's line is the same shape.
    trailing = "SUITES+=(\n    RUN_D_TESTS=true\n    RUN_E_TESTS=true)\n"
    assert _claimed_gates_from_text(trailing) == {"RUN_D_TESTS", "RUN_E_TESTS"}


def test_joining_arrays_does_not_swallow_the_rest_of_the_file():
    """The must-stay-clean half: a `case` arm and a `$( )` are not unterminated arrays.

    If either were mistaken for one, the joiner would absorb every following line until the
    next `)` — silently merging unrelated commands, and (worse) hiding real assignments from
    the tokeniser in a detector whose failure mode is reporting nothing.
    """
    text = (
        'case "$1" in\n'
        "    --full) RUN_BACKEND=true ;;\n"
        "esac\n"
        "x=\"$(docker ps --format '{{.Names}}')\"\n"
        "export RUN_REAL_TESTS=true\n"
    )
    assert _claimed_gates_from_text(text) == {"RUN_REAL_TESTS"}


def test_a_workflow_env_block_is_read_as_a_claim():
    """CI exports these too, and its copy of the dead seven outlived the local one.

    `.github/workflows/pre-commit.yml` had a step named "Run security-gated suites" whose
    `env:` block set all seven variables no test reads. Because the scan looked only at
    `scripts/**/*.sh`, removing the shell half left the claim standing in the one gate every
    contributor sees on a PR.
    """
    workflow = (
        "      - name: Run security-gated suites\n"
        "        env:\n"
        "          RUN_PKI_TESTS: 'true'\n"
        '          RUN_MFA_TESTS: "1"\n'
        "          FIPS_MODE: 'true'\n"
        "          # RUN_SEARCH_QUALITY_TESTS deliberately omitted\n"
        "        run: pytest tests/\n"
    )
    assert _claimed_gates_from_yaml(workflow) == {"RUN_PKI_TESTS", "RUN_MFA_TESTS"}

    # A commented-out gate is not a claim, and neither is a false one.
    assert _claimed_gates_from_yaml("          # RUN_PKI_TESTS: 'true'\n") == set()
    assert _claimed_gates_from_yaml("          RUN_PKI_TESTS: 'false'\n") == set()


def test_a_shell_variable_that_merely_starts_with_run_is_not_a_gate():
    """`RUN_LOG="$OUT_DIR/run-$$.log"` lives in run-backend-tests.sh and is not a gate."""
    assert _claimed_gates_from_text('RUN_LOG="$OUT_DIR/run-$$.log"\n') == set()
    assert _claimed_gates_from_text('mv -f "$RUN_LOG" "$LOG"\n') == set()
