"""Multi-source (skip-version) upgrade rehearsal coverage (issue #783).

`FROM_VERSIONS` was documented in three places and read into a shell variable in a
fourth, and then never referenced again — no loop, no dispatch, nothing. That is the
exact "gate that reports success while unable to do its job" shape this repo has
already fixed twice (#413, #681): a variable that LOOKS like it enables a feature, and
does not.

This file has two jobs:

1. A GENERALISED detector for that class of bug — not a test of `FROM_VERSIONS`
   specifically, which would fix one instance and miss the next one. It parses the
   `Env:` block of a script's `--help` heredoc and asserts every documented knob is
   actually READ somewhere in the script, outside comments, outside the heredoc that
   documents it, and outside its own `VAR="${VAR:-default}"` defaulting assignment.
   Run against `test-upgrade.sh` as it stood before this issue's fix (captured via
   `git archive HEAD` into a scratch tree, never by reverting the live file), it fails
   on exactly `FROM_VERSIONS` — that failure is the red-before-green evidence.

2. Execution coverage for `ver_upgrade_sources()` (`scripts/release-tests/lib/
   versions.sh`) — the derivation this issue actually implements — and structural
   coverage for the re-exec dispatcher `test-upgrade.sh` wires it into.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_TESTS_DIR = REPO_ROOT / "scripts" / "release-tests"
TEST_UPGRADE_SH = RELEASE_TESTS_DIR / "test-upgrade.sh"
VERSIONS_SH = RELEASE_TESTS_DIR / "lib" / "versions.sh"
GUARDRAILS_SH = RELEASE_TESTS_DIR / "lib" / "guardrails.sh"
REHEARSE_SH = REPO_ROOT / "scripts" / "release" / "65-rehearse.sh"
RELEASING_MD = REPO_ROOT / "docs-site" / "docs" / "developer-guide" / "releasing.md"
RELEASE_TESTS_README = RELEASE_TESTS_DIR / "README.md"
UPGRADING_MD = REPO_ROOT / "docs-site" / "docs" / "operations" / "upgrading.md"

pytestmark = pytest.mark.skipif(
    not TEST_UPGRADE_SH.exists() or not VERSIONS_SH.exists(),
    reason="scripts/release-tests/{test-upgrade.sh,lib/versions.sh} missing",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# =================================================================================
# 1. The generalised dead-knob detector
# =================================================================================

_HEREDOC_START_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?\s*$")
_ENV_KNOB_RE = re.compile(r"^\s{2}([A-Z][A-Z0-9_]*)\s+\S")


def _strip_heredocs_and_comments(text: str) -> str:
    """Remove every heredoc BODY (documentation, not code) and every comment.

    A knob mentioned only inside the --help text it is documented by must not count
    as a "read" -- that is exactly how a dead knob hides: the help text repeats its
    own name back at the reader.
    """
    out_lines: list[str] = []
    in_heredoc = False
    terminator = ""
    for line in text.splitlines():
        if in_heredoc:
            if line.strip() == terminator:
                in_heredoc = False
            continue
        code_part = line.split("#", 1)[0]
        out_lines.append(code_part)
        m = _HEREDOC_START_RE.search(code_part.rstrip())
        if m:
            in_heredoc = True
            terminator = m.group(1)
    return "\n".join(out_lines)


def _documented_env_knobs(script_text: str) -> list[str]:
    """Knob names from the `Env:` block of the script's `--help` heredoc.

    Deliberately excludes CLI-only flags (`--no-rollback`, `--list-sources`, ...) --
    those lines start with `--`, never an uppercase identifier, so the knob regex
    never matches them.
    """
    m = re.search(r"^Env:\s*$", script_text, re.MULTILINE)
    assert m, "no 'Env:' heading found in the --help heredoc"
    body = script_text[m.end() :]
    # Stop at the heredoc terminator (a bare 'EOF' line) if present, else EOF of string.
    end = re.search(r"^EOF\s*$", body, re.MULTILINE)
    if end:
        body = body[: end.start()]
    knobs: list[str] = []
    for line in body.splitlines():
        km = _ENV_KNOB_RE.match(line)
        if km:
            knobs.append(km.group(1))
    return knobs


def _own_defaulting_assignment_re(knob: str) -> re.Pattern[str]:
    escaped = re.escape(knob)
    return re.compile(
        rf"^\s*(?:export\s+)?{escaped}\s*=\s*\"\$\{{{escaped}(?::-|:=)[^}}]*\}}\"\s*$"
        rf"|^\s*:\s*\"\$\{{{escaped}:=[^}}]*\}}\"\s*;?\s*$"
    )


def _undocumented_dead_knobs(script_text: str) -> list[str]:
    """Return every documented Env knob with NO read outside comments, its own
    heredoc, and its own defaulting assignment. Empty means the script is clean.
    """
    knobs = _documented_env_knobs(script_text)
    code = _strip_heredocs_and_comments(script_text)
    code_lines = code.splitlines()

    dead: list[str] = []
    for knob in knobs:
        own_assignment_re = _own_defaulting_assignment_re(knob)
        mention_re = re.compile(rf"\b{re.escape(knob)}\b")
        has_other_reference = False
        for line in code_lines:
            if not mention_re.search(line):
                continue
            if own_assignment_re.match(line):
                continue
            has_other_reference = True
            break
        if not has_other_reference:
            dead.append(knob)
    return dead


@pytest.mark.unit
def test_guard_the_guard_env_parser_finds_the_known_knobs() -> None:
    """A parser matching nothing would make every check below vacuous."""
    knobs = _documented_env_knobs(_read(TEST_UPGRADE_SH))
    assert len(knobs) >= 8, f"expected >= 8 documented Env knobs, parsed {knobs}"
    assert "FROM_VERSIONS" in knobs
    assert "REQUIRE_PREVIOUS" in knobs
    assert "OT_UPGRADE_SOURCE_MINORS" in knobs


@pytest.mark.unit
def test_no_dead_env_knobs_in_test_upgrade_sh() -> None:
    """The fixed script: every documented knob is read somewhere real.

    Before this issue's fix, this failed with `dead == ["FROM_VERSIONS"]` -- captured
    as the red-before-green evidence via `git archive HEAD` into a scratch tree (never
    by reverting the live file; see the repo's CLAUDE.md on why).
    """
    dead = _undocumented_dead_knobs(_read(TEST_UPGRADE_SH))
    assert dead == [], (
        f"{dead} are documented in test-upgrade.sh's --help Env: block but never read "
        f"anywhere outside a comment, the help text itself, or their own defaulting "
        f"assignment -- the exact 'FROM_VERSIONS' shape issue #783 was filed for"
    )


@pytest.mark.unit
def test_must_fire_a_phantom_knob_only_in_its_own_default_is_flagged() -> None:
    """Must-fire control: a synthetic script with the exact bug shape."""
    synthetic = """#!/bin/bash
FOO="${FOO:-1}"
PHANTOM="${PHANTOM:-2}"
while (( $# > 0 )); do
    case "$1" in
        --help)
            cat <<EOF
Usage: prog [--help]

Env:
  FOO       used below, this one is fine
  PHANTOM   only appears in its own default and right here
EOF
            exit 0 ;;
        *) shift ;;
    esac
done
echo "using $FOO"
"""
    dead = _undocumented_dead_knobs(synthetic)
    assert dead == ["PHANTOM"], dead


@pytest.mark.unit
def test_must_stay_clean_when_every_knob_is_genuinely_read() -> None:
    """Must-stay-clean control: same shape, but PHANTOM also gets a real read."""
    synthetic = """#!/bin/bash
FOO="${FOO:-1}"
PHANTOM="${PHANTOM:-2}"
while (( $# > 0 )); do
    case "$1" in
        --help)
            cat <<EOF
Env:
  FOO       used below
  PHANTOM   used below too
EOF
            exit 0 ;;
        *) shift ;;
    esac
done
echo "using $FOO and $PHANTOM"
"""
    assert _undocumented_dead_knobs(synthetic) == []


# =================================================================================
# 2. ver_upgrade_sources() -- EXECUTED against fixtures, never grepped.
#    No git, no Docker, no network: ver_release_tags and ver_hub_has_release are
#    redefined AFTER sourcing versions.sh, and bash's "later definition wins" rule
#    means the fixtures, not the real git/Hub-calling implementations, run.
# =================================================================================


def _run_ver_upgrade_sources(
    *,
    to_version: str,
    tags_newest_first: list[str],
    published: set[str],
    minors: int | None = None,
    from_versions: str | None = None,
) -> tuple[list[str], str]:
    tags_body = " ".join(f'"{t}"' for t in tags_newest_first)
    published_cases = "|".join(re.escape(t) for t in sorted(published)) or "__none__"
    script = f"""
set -euo pipefail
export REPO_ROOT="/nonexistent-repo-root-for-unit-test"
export TO_VERSION="{to_version}"
export FROM_VERSIONS="{from_versions or ""}"
{f'export OT_UPGRADE_SOURCE_MINORS="{minors}"' if minors is not None else "unset OT_UPGRADE_SOURCE_MINORS || true"}
source "{VERSIONS_SH}"

ver_release_tags() {{
    printf '%s\\n' {tags_body}
}}
ver_hub_has_release() {{
    case "$1" in
        {published_cases}) return 0 ;;
        *) return 1 ;;
    esac
}}

ver_upgrade_sources
"""
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return lines, proc.stderr


@pytest.mark.unit
def test_real_repo_shape_v050_derives_v041_and_v033() -> None:
    sources, stderr = _run_ver_upgrade_sources(
        to_version="v0.5.0",
        tags_newest_first=[
            "v0.4.1",
            "v0.4.0",
            "v0.3.3",
            "v0.3.2",
            "v0.2.1",
            "v0.2.0",
            "v0.1.0",
        ],
        published={"v0.4.1", "v0.3.3"},
    )
    assert sources == ["v0.4.1", "v0.3.3"], stderr


@pytest.mark.unit
def test_patch_to_collapses_to_a_single_hop() -> None:
    sources, stderr = _run_ver_upgrade_sources(
        to_version="v0.5.1",
        tags_newest_first=["v0.5.0", "v0.4.1", "v0.3.3"],
        published={"v0.5.0", "v0.4.1", "v0.3.3"},
    )
    assert sources == ["v0.5.0"], stderr


@pytest.mark.unit
def test_newest_in_series_unpublished_falls_back_within_the_series() -> None:
    sources, stderr = _run_ver_upgrade_sources(
        to_version="v0.5.0",
        tags_newest_first=["v0.4.1", "v0.4.0", "v0.3.3"],
        published={"v0.4.0", "v0.3.3"},
    )
    assert sources == ["v0.4.0", "v0.3.3"], stderr


@pytest.mark.unit
def test_one_series_only_gives_one_hop_not_two() -> None:
    sources, stderr = _run_ver_upgrade_sources(
        to_version="v0.5.0",
        tags_newest_first=["v0.4.1", "v0.4.0"],
        published={"v0.4.1", "v0.4.0"},
    )
    assert sources == ["v0.4.1"], stderr


@pytest.mark.unit
def test_nothing_published_gives_empty_list_and_rc_zero() -> None:
    sources, stderr = _run_ver_upgrade_sources(
        to_version="v0.5.0",
        tags_newest_first=["v0.4.1", "v0.3.3"],
        published=set(),
    )
    assert sources == [], stderr


@pytest.mark.unit
def test_v0_10_series_is_not_confused_with_v0_9() -> None:
    """v0.10.0 > v0.9.0 (versions.sh names this as a sort -V hazard); this pins
    that the two are also treated as DISTINCT series by ver_upgrade_sources, not
    collapsed by a suffix-strip that ignores the two-digit minor.
    """
    sources, stderr = _run_ver_upgrade_sources(
        to_version="v0.11.0",
        tags_newest_first=["v0.10.0", "v0.9.5", "v0.9.0"],
        published={"v0.10.0", "v0.9.0"},
    )
    assert sources == ["v0.10.0", "v0.9.0"], stderr


@pytest.mark.unit
def test_minors_1_yields_exactly_one_source() -> None:
    sources, stderr = _run_ver_upgrade_sources(
        to_version="v0.5.0",
        tags_newest_first=["v0.4.1", "v0.3.3"],
        published={"v0.4.1", "v0.3.3"},
        minors=1,
    )
    assert sources == ["v0.4.1"], stderr


@pytest.mark.unit
def test_from_versions_override_replaces_the_derivation_with_no_hub_check() -> None:
    """FROM_VERSIONS is an override of the derived set, not what enables multi-hop.
    Overridden entries are echoed as-is (validated later, per-hop, by
    ver_previous_version's existing FROM_VERSION branch) -- no Hub/git lookups here.
    """
    sources, stderr = _run_ver_upgrade_sources(
        to_version="v0.5.0",
        tags_newest_first=[],
        published=set(),
        from_versions="v0.9.9 v0.2.0",
    )
    assert sources == ["v0.9.9", "v0.2.0"], stderr


# =================================================================================
# 3. The dispatcher reaches the exit code (structural, mirrors
#    test_release_test_harness_verdict.py's shape for the same reason: driving the
#    real re-exec dispatcher needs multi-GB images and a stopped live stack).
# =================================================================================


@pytest.mark.unit
def test_dispatcher_assigns_release_test_exit_code_from_the_aggregate() -> None:
    code = "\n".join(line.split("#", 1)[0] for line in _read(TEST_UPGRADE_SH).splitlines())
    assert re.search(r'RELEASE_TEST_EXIT_CODE="\$agg"', code), (
        "the multi-hop dispatcher must assign RELEASE_TEST_EXIT_CODE from its own "
        "aggregate, not leave the dispatch to exit bare -- both exit paths in this "
        "script (single-hop's phase 18, and the dispatcher) must speak the same "
        "variable so a reader does not have to discover there are two verdict "
        "mechanisms"
    )
    assert 'exit "$RELEASE_TEST_EXIT_CODE"' in code, (
        "the dispatcher never actually exits with the aggregate it computed"
    )


@pytest.mark.unit
def test_must_fire_a_bare_exit_0_is_not_mistaken_for_the_real_assignment() -> None:
    bare = "RELEASE_TEST_EXIT_CODE=0\nexit 0\n"
    assert not re.search(r'RELEASE_TEST_EXIT_CODE="\$agg"', bare)


# =================================================================================
# 4. Ordering: --cleanup precedes the dispatcher, which precedes the FROM/TO banner.
#    Getting --cleanup on the wrong side makes teardown_scenario/gr_cleanup recurse
#    per source -- a subtle, expensive, silent bug.
# =================================================================================


@pytest.mark.unit
def test_cleanup_precedes_dispatcher_precedes_from_banner() -> None:
    text = _read(TEST_UPGRADE_SH)
    cleanup_idx = text.index("if (( DO_CLEANUP == 1 )); then")
    dispatcher_def_idx = text.index("_ot_dispatch_multi_hop_upgrade() {")
    dispatcher_call_idx = text.index('_ot_dispatch_multi_hop_upgrade "$TEST_ROOT"')
    from_banner_idx = text.index("# ─── Resolve FROM ")
    assert cleanup_idx < dispatcher_def_idx < dispatcher_call_idx < from_banner_idx, (
        "expected order: --cleanup early-exit, then the multi-hop dispatcher "
        "(definition and its call), then the FROM/TO resolution banner. Getting "
        "--cleanup on the wrong side of the dispatcher makes gr_cleanup reachable "
        "from inside a re-exec'd child, which would recurse per source."
    )


# =================================================================================
# 5. Teardown between hops: the dispatcher actually calls --cleanup --yes and waits
#    via gr_wait_for_stock_containers_gone, whose label filter must be BYTE-IDENTICAL
#    to 65-rehearse.sh's own stock_containers() -- both ask "is a stock-named
#    opentranscribe-* stack still around", and independently typing that filter twice
#    is a second chance for the two to drift apart.
# =================================================================================


def _extract_function_body(text: str, func_name: str) -> str:
    m = re.search(rf"^{re.escape(func_name)}\s*\(\)\s*\{{", text, re.MULTILINE)
    assert m, f"function {func_name} not found"
    depth = 1
    i = m.end()
    start = i
    while depth > 0 and i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start : i - 1]


_STOCK_FILTER_RE = re.compile(r"label=com\.docker\.compose\.project=opentranscribe")


@pytest.mark.unit
def test_dispatcher_tears_down_between_hops_and_waits() -> None:
    dispatcher_body = _extract_function_body(
        _read(TEST_UPGRADE_SH), "_ot_dispatch_multi_hop_upgrade"
    )
    assert "--cleanup --yes" in dispatcher_body, (
        "the dispatcher must tear down each non-final hop's stack with "
        "'--cleanup --yes' before the next hop starts"
    )
    assert "gr_wait_for_stock_containers_gone" in dispatcher_body, (
        "the dispatcher must wait for the torn-down stack's containers to actually "
        "go away before starting the next hop -- docker cleanup is asynchronous"
    )


@pytest.mark.unit
def test_stock_container_label_filter_is_byte_identical_across_the_two_pollers() -> None:
    guardrails_fn = _extract_function_body(
        _read(GUARDRAILS_SH), "gr_wait_for_stock_containers_gone"
    )
    rehearse_fn = _extract_function_body(_read(REHEARSE_SH), "stock_containers")

    g_match = _STOCK_FILTER_RE.search(guardrails_fn)
    r_match = _STOCK_FILTER_RE.search(rehearse_fn)
    assert g_match, "gr_wait_for_stock_containers_gone has no stock-project label filter"
    assert r_match, "65-rehearse.sh's stock_containers() has no stock-project label filter"
    assert g_match.group(0) == r_match.group(0)


@pytest.mark.unit
def test_must_fire_a_mismatched_filter_string_is_detected() -> None:
    """Must-fire control: prove the comparison actually discriminates."""
    a = "label=com.docker.compose.project=opentranscribe"
    b = "label=com.docker.compose.project=transcribe-app"
    assert a != b


# =================================================================================
# 6. Docs/code agreement: the count named in upgrading.md matches
#    OT_UPGRADE_SOURCE_MINORS' coded default, and no doc names FROM_VERSIONS without
#    also naming the derived default (so no doc can drift back into "you must set
#    this to get multi-hop").
# =================================================================================


@pytest.mark.unit
def test_upgrading_md_minor_count_matches_versions_sh_default() -> None:
    m = re.search(r"OT_UPGRADE_SOURCE_MINORS:-(\d+)", _read(VERSIONS_SH))
    assert m, "versions.sh no longer defaults OT_UPGRADE_SOURCE_MINORS the expected way"
    default = m.group(1)

    upgrading_text = _read(UPGRADING_MD)
    assert "OT_UPGRADE_SOURCE_MINORS" in upgrading_text, (
        "upgrading.md's skip-version policy must name the knob that governs it"
    )
    assert re.search(
        rf"\b{default}\b", upgrading_text.split("OT_UPGRADE_SOURCE_MINORS")[0][-200:]
    ) or (f"default `{default}`" in upgrading_text), (
        f"upgrading.md must state the coded default ({default}) explicitly, not a "
        f"number that could silently drift from versions.sh's"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "doc_path",
    [RELEASING_MD, RELEASE_TESTS_README, TEST_UPGRADE_SH],
    ids=lambda p: p.name,
)
def test_every_from_versions_mention_also_names_the_derived_default(doc_path: Path) -> None:
    text = _read(doc_path)
    if "FROM_VERSIONS" not in text:
        pytest.skip(f"{doc_path.name} does not mention FROM_VERSIONS")
    assert "OT_UPGRADE_SOURCE_MINORS" in text, (
        f"{doc_path.name} documents FROM_VERSIONS without also naming "
        f"OT_UPGRADE_SOURCE_MINORS -- that is how a doc drifts back into implying "
        f"FROM_VERSIONS is what you set to 'enable' multi-hop, rather than an "
        f"override of a derivation that already runs on its own"
    )


# ---------------------------------------------------------------------------
# The roll-up's per-hop assertion counts (issue #783, C13).
#
# These EXECUTE the real `_ot_hop_assertion_counts` extracted from
# test-upgrade.sh rather than asserting on its source text, because the whole
# claim is about what it does with a report file that is present, absent, or
# empty -- three behaviours a grep cannot tell apart.
# ---------------------------------------------------------------------------


def _run_hop_assertion_counts(report_arg: str) -> str:
    """Run the REAL helper, lifted verbatim out of test-upgrade.sh."""
    body = _extract_function_body(_read(TEST_UPGRADE_SH), "_ot_hop_assertion_counts")
    script = f"set -euo pipefail\n_ot_hop_assertion_counts() {{{body}}}\n_ot_hop_assertion_counts {report_arg}\n"
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"helper exited {proc.returncode}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.mark.unit
def test_hop_counts_are_parsed_from_the_hops_own_report(tmp_path: Path) -> None:
    """Must-fire for the counting itself: the numbers must come back off the
    `| PASS | ... |` rows lib/assertions.sh's as_record writes, not from the
    parent's own as_pass/as_fail/as_skip counters -- those live in the CHILD
    process and are permanently 0 in the dispatcher, which is indistinguishable
    from a hop that asserted nothing.
    """
    report = tmp_path / "REPORT.md"
    report.write_text(
        "| PASS | alembic head | |\n"
        "| PASS | frontend 200 | |\n"
        "| FAIL | transcript present | missing |\n"
        "| SKIP | openapi.json | not served |\n"
        "| PASS | rollback digest | |\n",
        encoding="utf-8",
    )
    assert _run_hop_assertion_counts(str(report)) == "3 | 1 | 1"


@pytest.mark.unit
def test_a_hop_with_no_report_is_dashes_not_zeroes(tmp_path: Path) -> None:
    """The distinction the roll-up exists to keep. A hop that never ran (rc=3,
    never attempted) has NO evidence; reporting it as `0 | 0 | 0` would make it
    read as a hop that ran and asserted nothing, which is a different -- and
    much worse -- fact. An EMPTY report is the genuine `0 | 0 | 0` case, and the
    two must not collapse onto each other.
    """
    assert _run_hop_assertion_counts(str(tmp_path / "never-created.md")) == "- | - | -"

    empty = tmp_path / "REPORT.md"
    empty.write_text("", encoding="utf-8")
    assert _run_hop_assertion_counts(str(empty)) == "0 | 0 | 0"


@pytest.mark.unit
def test_the_rollup_table_actually_calls_the_counts_helper() -> None:
    """Guard the guard: the two tests above prove the helper is correct, and
    would both still pass if the roll-up table stopped calling it. This is the
    wiring assertion -- without it, a correct-but-unused helper looks identical
    to a working roll-up.
    """
    dispatcher_body = _extract_function_body(
        _read(TEST_UPGRADE_SH), "_ot_dispatch_multi_hop_upgrade"
    )
    assert "_ot_hop_assertion_counts" in dispatcher_body, (
        "the multi-hop roll-up no longer calls _ot_hop_assertion_counts -- its "
        "REPORT.md table would carry a verdict per hop with no assertion counts "
        "behind it, so 'PASS' could not be distinguished from 'passed having "
        "checked nothing'"
    )
    assert "| PASS | FAIL | SKIP |" in dispatcher_body, (
        "the roll-up table header no longer declares the PASS/FAIL/SKIP columns "
        "the counts helper fills in"
    )
