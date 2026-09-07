"""`scripts/test-matrix.sh` must RUN its legs, not print a placeholder that reads like coverage.

Stages 2, 3 and 4 used to check a precondition (is the stack up / down / are the scanners
installed) and then write ``NOT-MEASURED … execution is a separate, future effort`` to the report
and return 0. So ``test-matrix.sh all`` exited 0 having proven the eight fast static checks and
the leg table's own doc-sync — and nothing at all about GPU scaling, diarization providers, lite
mode, auth, PKI, fresh install or upgrade, despite listing every one of them.

That is worse than having no leg: a green checklist is read as evidence. These tests are the
guard against it coming back, plus the invariants that keep the leg table honest — every command
must name a file that exists, and every leg must declare which of this repo's TWO exit-code
conventions its command follows.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MATRIX = REPO_ROOT / "scripts" / "test-matrix.sh"
DOC = REPO_ROOT / "docs-site" / "docs" / "developer-guide" / "full-test-matrix.md"

# The three scripts that use the smoke convention (`0 pass · 1 fail · 4 NOT MEASURED`) rather
# than the standard one this repo shares between release.sh and test-matrix.sh
# (`0 pass · 1 gate · 2 misuse · 3 precondition · 4 operator abort`). Exit 4 means different
# things in the two, which is exactly why each leg has to declare which one it follows.
SMOKE_CONTRACT_SCRIPTS = {
    "scripts/gpu-scale-smoke.sh",
    "scripts/diar-native-smoke.sh",
    "scripts/lite-smoke.sh",
}

KNOWN_CONTRACTS = {"standard", "smoke"}

pytestmark = pytest.mark.skipif(
    not MATRIX.exists(), reason="scripts/test-matrix.sh not present in this checkout"
)


def _matrix_source() -> str:
    return MATRIX.read_text(encoding="utf-8")


def _legs() -> list[tuple[str, str, str, str, str]]:
    """(id, stage, description, command, exit-contract) for every LEGS entry.

    Deliberately TOLERANT of a short entry (missing contract -> ""), and asserts nothing: it is
    called at module scope by @parametrize, so a hard assert here turns every test in the file
    into one collection error and hides which invariant actually broke. The shape is asserted by
    test_every_leg_entry_has_all_five_fields below, where a failure names the offending row.
    """
    source = _matrix_source()
    block = re.search(r"^LEGS=\(\n(.*?)^\)", source, re.DOTALL | re.MULTILINE)
    if not block:
        return []
    legs = []
    for raw in block.group(1).splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = raw.strip('"').split("|")
        parts += [""] * (5 - len(parts))
        legs.append(tuple(parts[:5]))  # type: ignore[arg-type]
    return legs


def test_the_leg_table_parses_and_is_not_empty():
    legs = _legs()
    assert len(legs) >= 16, f"expected the documented 16 legs, parsed {len(legs)}"


def test_every_leg_entry_has_all_five_fields():
    """id|stage|description|command|exit-contract. A short row silently loses its contract."""
    source = _matrix_source()
    block = re.search(r"^LEGS=\(\n(.*?)^\)", source, re.DOTALL | re.MULTILINE)
    assert block, "LEGS array not found in test-matrix.sh"
    short = [
        raw.strip()
        for raw in block.group(1).splitlines()
        if raw.strip()
        and not raw.strip().startswith("#")
        and len(raw.strip().strip('"').split("|")) != 5
    ]
    assert not short, f"LEGS entries are not id|stage|description|command|exit-contract: {short}"


def test_no_stage_writes_a_not_measured_placeholder():
    """THE regression guard. This exact string is what made three stages look like coverage."""
    source = _matrix_source()
    offenders = [
        line.strip()
        for line in source.splitlines()
        if "NOT-MEASURED" in line and "echo" in line and "REPORT_FILE" in line
    ]
    assert not offenders, (
        f"test-matrix.sh writes a NOT-MEASURED placeholder to the report: {offenders}. A leg "
        "must either run and report PASS/FAIL, or report SKIP with the WRAPPED SCRIPT'S OWN "
        "stated reason — never a generic 'future effort' line that reads the same whether the "
        "leg was genuinely unmeasurable or simply never wired up."
    )
    assert "execution is a separate, future effort" not in source, (
        "the deferred-execution placeholder text is back in test-matrix.sh"
    )


def test_the_placeholder_detector_actually_fires():
    """Must-fire control: the detector above must match the shape it is meant to catch."""
    synthetic = [
        '            echo "NOT-MEASURED  $id  $desc  (future effort)" >> "$REPORT_FILE"',
        '            echo "PASS  $id  $desc" >> "$REPORT_FILE"',
        "            # NOT-MEASURED is discussed in this comment but is not a write",
    ]
    fired = [
        line.strip()
        for line in synthetic
        if "NOT-MEASURED" in line and "echo" in line and "REPORT_FILE" in line
    ]
    assert len(fired) == 1, f"expected exactly the placeholder line to fire, got: {fired}"


def test_every_stage_runs_through_the_same_single_execution_path():
    """Stage-specific work is the precondition; the RUN is one shared code path.

    Two separate execution branches is how stage 1 ended up real and stages 2/3/4 did not.
    """
    source = _matrix_source()
    runs = re.findall(r'bash -c "\$cmd"', source)
    assert len(runs) == 1, (
        f'expected exactly ONE `bash -c "$cmd"` execution site in test-matrix.sh, found '
        f"{len(runs)}. A per-stage execution branch is what let three stages quietly not run."
    )
    # And it must sit AFTER the per-stage case block, i.e. every stage reaches it.
    case_end = source.find("esac", source.find('case "$stage" in'))
    run_at = source.find('bash -c "$cmd"')
    assert case_end != -1 and run_at > case_end, (
        "the execution site is inside the per-stage `case` block — it must follow it, so that "
        "every stage runs after its own precondition rather than some stages returning early"
    )


@pytest.mark.parametrize("leg", _legs(), ids=lambda leg: leg[0])
def test_every_leg_declares_a_known_exit_contract(leg: tuple[str, str, str, str, str]):
    leg_id, _stage, _desc, _cmd, contract = leg
    assert contract in KNOWN_CONTRACTS, (
        f"leg {leg_id} declares exit-contract '{contract}'; known contracts are "
        f"{sorted(KNOWN_CONTRACTS)}. Add the new one to run_leg()'s exit-code reading in the "
        "same change, or the leg's verdict will be misread."
    )


def _repo_path_tokens(cmd: str) -> list[str]:
    """Tokens in a leg command that name a path inside the repo.

    Path-like = contains a `/` or ends in .sh/.py. Deliberately not "every token": leg 1.5 is
    `cd docs-site && npm run build`, whose only non-path tokens are real shell/npm words, and
    resolving an arbitrary shell pipeline is not this test's job.
    """
    tokens = shlex.split(cmd, comments=False, posix=True)
    return [
        t
        for t in tokens
        if not t.startswith("-")
        and "://" not in t
        # backend/venv/ is gitignored and environment-provided — absent in every fresh
        # checkout and in every git worktree (scripts/CLAUDE.md). Its absence is a local
        # setup condition, not a broken leg table.
        and not t.startswith("backend/venv/")
        and ("/" in t or t.endswith((".sh", ".py")))
    ]


@pytest.mark.parametrize("leg", _legs(), ids=lambda leg: leg[0])
def test_every_leg_command_references_only_existing_paths(leg: tuple[str, str, str, str, str]):
    """A leg naming a script that does not exist fails at run time with a shell error, not a
    verdict — and under the old deferred stages it would never even have been noticed."""
    leg_id, _stage, _desc, cmd, _contract = leg
    missing = [t for t in _repo_path_tokens(cmd) if not (REPO_ROOT / t).exists()]
    assert not missing, (
        f"leg {leg_id} references paths that do not exist: {missing} (command: {cmd})"
    )


def test_almost_every_leg_actually_invokes_a_repo_script():
    """Vacuity guard for the test above: if the path extraction stopped matching, every leg
    would trivially pass with an empty token list."""
    with_scripts = [
        leg_id
        for (leg_id, _s, _d, cmd, _c) in _legs()
        if any(t.endswith((".sh", ".py")) for t in _repo_path_tokens(cmd))
    ]
    legs = _legs()
    assert len(with_scripts) >= len(legs) - 1, (
        f"only {len(with_scripts)} of {len(legs)} legs name a repo script "
        f"({with_scripts}) — the path extraction has probably stopped matching, which would "
        "make the per-leg existence check pass on anything"
    )


def test_smoke_contract_is_declared_for_exactly_the_scripts_that_use_it():
    """The mapping must follow the scripts, not a guess.

    If a smoke script changes its exit convention (or a new leg wraps one), this fails rather
    than silently reporting that script's honest 'I could not measure this' as an operator abort.
    """
    declared_smoke = {
        cmd.strip() for (_id, _stage, _desc, cmd, contract) in _legs() if contract == "smoke"
    }
    assert declared_smoke == SMOKE_CONTRACT_SCRIPTS, (
        f"legs declaring the smoke contract are {sorted(declared_smoke)}, but the scripts that "
        f"actually use it are {sorted(SMOKE_CONTRACT_SCRIPTS)}"
    )

    for rel in sorted(SMOKE_CONTRACT_SCRIPTS):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert re.search(r"4\s+NOT MEASURED", text), (
            f"{rel} no longer documents `4 NOT MEASURED` in its exit-code header — either it "
            "changed convention (update the leg's exit-contract) or the header rotted"
        )


# --------------------------------------------------------------------------- #
# The shared exit-code lexicon
#
# 0 pass · 1 gate failed · 2 misuse · 3 precondition unmet · 4 operator abort.
# Every collapse of that into "non-zero means failed" has the same cost: an operator who
# declined a prompt, or a precondition nobody could have met, is recorded as a test that ran
# and found a regression. These pin the two places that used to do it.
# --------------------------------------------------------------------------- #

GUARDRAILS = REPO_ROOT / "scripts" / "release-tests" / "lib" / "guardrails.sh"
REHEARSE = REPO_ROOT / "scripts" / "release" / "65-rehearse.sh"


def test_a_declined_confirmation_is_an_abort_not_a_failure():
    """gr_confirmation_gate used to gr_die (exit 1) when the operator declined.

    So "I typed something other than I UNDERSTAND" propagated to release.sh's ledger and to
    test-matrix.sh's leg 3 as a FAILED rehearsal — a red result for a rehearsal that never ran.
    """
    source = GUARDRAILS.read_text(encoding="utf-8")
    gate = re.search(r"gr_confirmation_gate\(\)\s*\{.*?\n\}", source, re.DOTALL)
    assert gate, "gr_confirmation_gate not found in guardrails.sh"
    body = gate.group(0)
    assert "gr_abort" in body, (
        "the declined-confirmation branch no longer calls gr_abort — an operator abort would "
        "again be reported as a gate failure"
    )
    assert "gr_die" not in body, (
        f"gr_confirmation_gate still exits via gr_die (exit 1) somewhere: {body}"
    )
    # gr_abort is a one-liner whose body interpolates ${GR_YELLOW}, so a `[^}]*` body match
    # stops at that brace — check the definition line itself.
    abort_def = [ln for ln in source.splitlines() if ln.lstrip().startswith("gr_abort()")]
    assert len(abort_def) == 1, f"expected exactly one gr_abort definition, got {abort_def}"
    assert "exit 4" in abort_def[0], (
        f"gr_abort does not exit 4 — the shared contract's operator-abort code: {abort_def[0]}"
    )


def _run_shell(snippet: str) -> tuple[int, str]:
    import subprocess

    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


@pytest.mark.parametrize(
    ("fresh_rc", "upgrade_rc", "expected"),
    [
        (0, 0, 0),
        (1, 0, 1),
        (0, 1, 1),
        (3, 0, 3),
        (0, 3, 3),
        (4, 0, 4),
        (0, 4, 4),
        # An abort outranks a gate failure: if either half was aborted, the run did not
        # complete, so reporting "failed" would overstate what was measured.
        (1, 4, 4),
        (4, 3, 4),
    ],
)
def test_rehearse_preserves_the_exit_contract(fresh_rc: int, upgrade_rc: int, expected: int):
    """65-rehearse.sh's rc mapping, run for real with the REAL block extracted from the script.

    It used to be `[[ $fresh_rc -eq 0 && $upgrade_rc -eq 0 ]] || rc=1`, flattening abort and
    precondition into a gate failure for every caller above it.
    """
    source = REHEARSE.read_text(encoding="utf-8")
    block = re.search(r"^rc=0\n(?:if|\[\[).*?^fi$", source, re.DOTALL | re.MULTILINE)
    assert block, "the rc-mapping block was not found in 65-rehearse.sh"
    snippet = f"fresh_rc={fresh_rc}\nupgrade_rc={upgrade_rc}\n{block.group(0)}\nexit $rc\n"
    rc, out = _run_shell(snippet)
    assert rc == expected, (
        f"fresh_rc={fresh_rc} upgrade_rc={upgrade_rc} mapped to {rc}, expected {expected}: {out}"
    )


def test_the_matrix_reports_abort_and_blocked_separately_from_fail():
    """run_leg must not collapse standard-contract 3/4 into FAIL."""
    source = _matrix_source()
    assert 'contract" == "standard" && $leg_rc -eq 4' in source, (
        "test-matrix.sh no longer distinguishes a standard-contract operator abort from a FAIL"
    )
    assert 'contract" == "standard" && $leg_rc -eq 3' in source, (
        "test-matrix.sh no longer distinguishes a standard-contract unmet precondition from a FAIL"
    )
    for token in ("ABORT  $id", "BLOCKED  $id", "SKIP  $id", "FAIL  $id", "PASS  $id"):
        assert token in source, f"the report no longer emits a {token.split()[0]} line"


@pytest.mark.parametrize(
    ("rc_in", "skip_count", "expected"),
    [
        # The regression this exists for: every leg that ran passed, but a smoke leg reported
        # NOT MEASURED (exit 4). Before EXIT_NOT_MEASURED that exited 0, so a caller reading
        # only $? could not tell a fully measured matrix from one whose diar-native/gpu-scale/
        # lite legs never executed at all.
        (0, 1, 5),
        (0, 3, 5),
        # A real verdict is the more important one and must survive: never downgrade a failure
        # into "not measured".
        (1, 2, 1),
        (3, 1, 3),
        (4, 1, 4),
        # Nothing skipped -> an honest, fully measured pass stays 0.
        (0, 0, 0),
    ],
)
def test_not_measured_legs_do_not_exit_zero(rc_in: int, skip_count: int, expected: int):
    """A green matrix with skips must not produce a green EXIT CODE.

    Runs test-matrix.sh's REAL summary block rather than asserting a string is present in the
    file: a grep would pass against a block that computed the wrong code. `info` and the colour
    variables are stubbed because they are the only things the block needs from the rest of the
    script; SKIPPED_LEGS is populated so the reporting loop executes for real too.
    """
    source = _matrix_source()
    block = re.search(
        r"^    if \(\( SKIP_COUNT > 0 \)\); then\n.*?^    fi$",
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert block, (
        "the SKIP_COUNT summary block was not found in test-matrix.sh — if it was renamed or "
        "restructured, update this test rather than deleting it: it guards the NOT-MEASURED "
        "exit code from silently collapsing back to 0"
    )
    legs = " ".join(f'"leg{i}: reason"' for i in range(skip_count))
    snippet = (
        "info() { :; }\n"
        "YELLOW=''; NC=''\n"
        "EXIT_NOT_MEASURED=5\n"
        f"RC={rc_in}\n"
        f"SKIP_COUNT={skip_count}\n"
        f"declare -a SKIPPED_LEGS=({legs})\n"
        f"{block.group(0)}\n"
        "exit $RC\n"
    )
    rc, out = _run_shell(snippet)
    assert rc == expected, (
        f"RC={rc_in} with {skip_count} NOT-MEASURED leg(s) exited {rc}, expected {expected}. "
        f"A NOT-MEASURED run must never be indistinguishable from a measured pass: {out}"
    )


def test_the_doc_and_the_script_still_agree():
    """check_doc_sync's anchors are load-bearing; a doc edit must not silently break them."""
    assert DOC.is_file(), f"{DOC} is missing — the anti-staleness check cannot run"
    doc = DOC.read_text(encoding="utf-8")
    for anchor in ("Cycle 2A", "Cycle 2B", "Cycle 2C", "Cycle 2D", "## Stage 3", "## Stage 4"):
        assert anchor in doc, (
            f"full-test-matrix.md lost the '{anchor}' anchor check_doc_sync greps for"
        )
    assert "### Stage 3 — lite-mode full rehearsal" in doc
    assert re.search(r"PKI/mTLS is prod", doc, re.IGNORECASE)
