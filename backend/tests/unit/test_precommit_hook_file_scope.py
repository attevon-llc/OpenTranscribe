"""A pre-commit hook must SELECT every file it makes an assertion about.

A `files:` pattern that does not match the file a hook guards is the same failure mode as a
detector that matches nothing: the gate is green because it never ran, which is
indistinguishable from a gate that ran and passed.

This is not hypothetical. `publish-platforms-not-a-pass` scoped `test-publish-platforms.sh` to
six paths. The suite then grew sections asserting on `scripts/release/90-promote.sh`,
`95-finish.sh`, `published-repos.sh`, `backend/Dockerfile.blackwell`, `setup-opentranscribe.sh`
and `backend/app/services/asr/factory.py` — none of which the pattern selected. A commit
touching only `95-finish.sh` (the file whose hardcoded repo list would let a release publish
`:latest` with no lite image on Docker Hub) ran nothing.

Two directions are checked, because either alone is escapable:

1. every path the suite DECLARES as a subject is selected by its hook's `files:` regex; and
2. every repo path the suite actually READS is declared as a subject.

(2) is what stops the declaration itself going stale — without it, someone adds an assertion
about a new file, forgets to declare it, and (1) passes over a list that no longer describes
the suite.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"

# hook id -> the shell suite it runs. Both suites are offline, fixture-driven guards over the
# build/publish plane; both are wired as local pre-commit hooks with a `files:` pattern.
GUARD_SUITES = {
    "publish-platforms-not-a-pass": "scripts/tests/test-publish-platforms.sh",
    "scan-not-a-pass": "scripts/tests/test-scan-not-a-pass.sh",
}

# Paths a suite references that are NOT single files it asserts about, and so are not expected
# in SUBJECT_FILES. Keyed by suite, with a written reason — an unexplained exemption here would
# reopen exactly the hole this module closes.
_READ_EXEMPTIONS = {
    # Directory prefixes consumed with a loop variable appended (`$REPO_ROOT/backend/$df`).
    # The concrete files they expand to are declared individually.
    "scripts/tests/test-publish-platforms.sh": {
        "backend/",
        "scripts/lib",
        "scripts/release/",
    },
    "scripts/tests/test-scan-not-a-pass.sh": set(),
}


def _hooks_by_id() -> dict[str, dict]:
    config = yaml.safe_load(PRECOMMIT_CONFIG.read_text())
    found = {}
    for repo in config.get("repos", []):
        for hook in repo.get("hooks", []):
            found[hook.get("id")] = hook
    return found


def _declared_subjects(suite: str) -> list[str]:
    """Ask the suite itself for its SUBJECT_FILES, rather than re-parsing the array here.

    Running the real script is the point: a second parser could drift from the array the
    script actually declares, and then this test would be checking its own copy.
    """
    result = subprocess.run(  # noqa: S603 - fixed in-repo script, no untrusted input
        ["/bin/bash", str(REPO_ROOT / suite)],
        env={"OT_PRINT_SUBJECT_FILES": "1", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
        check=False,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]

    # A suite that does not honour the protocol runs its ENTIRE test body instead, and every
    # line of that output would then be treated as a "declared subject" — turning a missing
    # declaration into a flood of nonsense assertions rather than one clear failure. Observed
    # while writing this. Validate the shape before believing any of it.
    bad = [ln for ln in lines if not re.fullmatch(r"[A-Za-z0-9_./-]+", ln)]
    assert not bad, (
        f"{suite} does not honour OT_PRINT_SUBJECT_FILES=1: it printed non-path output "
        f"({bad[:3]}...). The early-exit must sit ABOVE every echo in the script and print "
        f"only SUBJECT_FILES."
    )
    return lines


def _paths_read(suite: str) -> set[str]:
    """Repo-relative paths the suite reads via "$REPO_ROOT/...".

    Deliberately textual: the point is to notice a NEW `$REPO_ROOT/<path>` reference appearing
    in the script, which is precisely how the pattern went stale in the first place.
    """
    text = (REPO_ROOT / suite).read_text()
    return set(re.findall(r"\$REPO_ROOT/([A-Za-z0-9_./-]+)", text))


@pytest.mark.parametrize(("hook_id", "suite"), sorted(GUARD_SUITES.items()))
def test_hook_files_pattern_selects_every_subject_the_suite_asserts_about(hook_id, suite):
    hook = _hooks_by_id().get(hook_id)
    assert hook is not None, f"{hook_id} is no longer a hook in .pre-commit-config.yaml"

    pattern = hook.get("files")
    assert pattern, f"{hook_id} has no files: pattern — it would run on every commit"
    compiled = re.compile(pattern)

    subjects = _declared_subjects(suite)
    assert subjects, (
        f"{suite} declared no SUBJECT_FILES — this test cannot check anything, which is "
        f"NOT the same as there being nothing to check"
    )

    unselected = [s for s in subjects if not compiled.search(s)]
    assert not unselected, (
        f"{hook_id}'s files: pattern does not select {unselected}. A commit touching only one "
        f"of those would not run {suite}, so the guard would be silent on the very file it "
        f"guards. Add it to the files: pattern in .pre-commit-config.yaml."
    )


@pytest.mark.parametrize(("hook_id", "suite"), sorted(GUARD_SUITES.items()))
def test_every_repo_path_the_suite_reads_is_declared_as_a_subject(hook_id, suite):
    """Direction 2: the declaration cannot go stale behind the script."""
    subjects = set(_declared_subjects(suite))
    exempt = _READ_EXEMPTIONS[suite]

    undeclared = sorted(p for p in _paths_read(suite) if p not in subjects and p not in exempt)
    assert not undeclared, (
        f"{suite} reads {undeclared} but does not declare them in SUBJECT_FILES, so "
        f"{hook_id}'s files: pattern is never checked against them. Declare them (and add "
        f"them to the pattern), or add a written exemption to _READ_EXEMPTIONS."
    )


def test_the_selection_check_actually_fires_on_an_unselected_subject():
    """Must-fire control.

    Without this, both tests above pass identically whether the regex is correct or the
    comparison is broken — the exact "detector that matches nothing" shape this module exists
    to prevent. Uses the REAL pre-#667 pattern, which genuinely failed to select 95-finish.sh.
    """
    stale_pattern = (
        r"^(backend/Dockerfile\.(prod|lite)|scripts/(docker-build-push\.sh|security-scan\.sh"
        r"|lib/manifest_platform_check\.py|release/(80-publish|85-smoke)\.sh"
        r"|tests/(test-publish-platforms\.sh|fixtures/manifest-fixtures\.py)))$"
    )
    compiled = re.compile(stale_pattern)
    subjects = _declared_subjects("scripts/tests/test-publish-platforms.sh")

    missed = [s for s in subjects if not compiled.search(s)]
    assert "scripts/release/95-finish.sh" in missed, (
        "the historical pattern should NOT select 95-finish.sh — if it does, this control is "
        "not reproducing the bug and the checks above prove nothing"
    )

    # ...and the current pattern must select it, or the fix did not land.
    current = _hooks_by_id()["publish-platforms-not-a-pass"]["files"]
    assert re.compile(current).search("scripts/release/95-finish.sh"), (
        "the current pattern still does not select 95-finish.sh"
    )
