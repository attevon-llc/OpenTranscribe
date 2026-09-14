"""The GitHub Release body must survive TWO size limits, and v0.5.0 broke both.

FOUND ON THE REAL v0.5.0 FINISH RUN (2026-09-14). `95-finish.sh` built the release body by
lifting the `## [X.Y.Z]` section out of `CHANGELOG.md` and passing it as:

    gh release create "$VERSION" --title "$VERSION" "$latest_flag" \\
        --notes "$notes" "${assets[@]}"

v0.5.0's section is **2,917 lines / ~350 KB**, and that hits two independent ceilings:

1. **argv.** Linux caps a SINGLE argv entry at `MAX_ARG_STRLEN` = 128 KiB. It is a compile-time
   constant (`PAGE_SIZE * 32`), NOT covered by `ulimit -s` and not tunable. So execve failed
   with `E2BIG` and the stage died on `/usr/bin/gh: Argument list too long` — before any HTTP
   request existed. Nothing was created; the failure was loud and clean.

2. **GitHub.** The API rejects a release body over ~125,000 characters. `--notes-file` fixes
   limit 1 and does nothing for limit 2 — it is the server's limit, not the shell's. A fix that
   stopped at `--notes-file` would have turned a clear local error into a 422 from the API.

The fix therefore has to do both: write the body to a file (correct at any size, so it is
unconditional), and, when the section exceeds the cap, publish the `### Overview` — the readable
summary a release page should carry anyway — plus a link to the full CHANGELOG entry.

⚠️ **The trim is announced and recorded, never silent.** A body quietly cut mid-sentence at
125,000 bytes is worse than either the whole text or an honest excerpt: it looks like the
complete notes, and the reader has no way to know the rest existed. That is why this is a
declared criterion (`changelog-body-within-github-limit`) and not a quiet `head -c`.

These are static/structural checks on purpose: reproducing limit 1 needs a 350 KB argv and
limit 2 needs a real GitHub API call, while the property — what the body is and how it gets to
`gh` — is decidable from the source.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
FINISH = REPO_ROOT / "scripts" / "release" / "95-finish.sh"
CRITERIA = REPO_ROOT / "scripts" / "release" / "release-criteria.yaml"

#: GitHub's documented cap on a release body.
GITHUB_BODY_CAP = 125_000

#: Linux's cap on a single argv entry (PAGE_SIZE * 32). Not tunable, not `ulimit -s`.
MAX_ARG_STRLEN = 128 * 1024


def finish_source() -> str:
    return FINISH.read_text(encoding="utf-8")


def test_the_stage_and_criteria_file_still_exist():
    """GUARD THE GUARD: a move would make every check below vacuous."""
    assert FINISH.is_file(), f"missing {FINISH}"
    assert CRITERIA.is_file(), f"missing {CRITERIA}"


class TestLimitOneArgv:
    def test_the_body_is_passed_by_file_not_on_the_command_line(self):
        """RED against pre-fix HEAD, which passed `--notes "$notes"`.

        This is the one that actually fired: `gh` never ran, so the release was not created
        and the stage failed cleanly. A body larger than MAX_ARG_STRLEN cannot be an argv
        entry at all, no matter what the receiving program does with it.
        """
        src = finish_source()
        # Bound the window at the command's own terminator (`; then`). Splitting on a bare
        # "fi" instead cuts inside `--notes-file` and hides the very token being asserted —
        # a first draft of this test did exactly that and failed against the correct code.
        create = src[src.index("gh release create") :]
        create = create[: create.index("; then")]

        assert "--notes-file" in create, (
            "the release body is not passed as a file. A CHANGELOG section larger than "
            f"MAX_ARG_STRLEN ({MAX_ARG_STRLEN} bytes) makes execve fail with E2BIG and gh "
            "dies with 'Argument list too long' before making any request."
        )
        assert not re.search(r"--notes\s+\"\$notes\"", create), (
            "the body is still passed inline via --notes; that is the E2BIG path"
        )


class TestLimitTwoGitHubsCap:
    def test_an_over_long_section_is_trimmed_rather_than_sent_whole(self):
        """RED against pre-fix HEAD, which had no notion of the server's cap.

        `--notes-file` alone would have replaced a clean local failure with a 422.
        """
        src = finish_source()
        assert "GH_RELEASE_BODY_MAX" in src, (
            "no cap on the body actually sent to GitHub — the API rejects bodies over "
            f"~{GITHUB_BODY_CAP} characters, and --notes-file does not change that"
        )

    def test_the_configured_cap_is_at_or_below_githubs_real_limit(self):
        """A cap above the real limit is not a cap.

        Guards the direction that silently fails: setting 200000 would satisfy the check
        above while still producing a body the API refuses.
        """
        src = finish_source()
        m = re.search(r'GH_RELEASE_BODY_MAX="\$\{GH_RELEASE_BODY_MAX:-(\d+)\}"', src)
        assert m, "GH_RELEASE_BODY_MAX is not given a concrete default"
        configured = int(m.group(1))
        assert configured <= GITHUB_BODY_CAP, (
            f"the configured cap ({configured}) exceeds GitHub's real limit "
            f"({GITHUB_BODY_CAP}), so an over-long body would still be rejected"
        )
        assert configured < MAX_ARG_STRLEN, (
            "a cap at or above MAX_ARG_STRLEN would reintroduce the argv failure for any "
            "caller that still built an inline argument"
        )

    def test_the_trim_keeps_the_overview_and_links_the_full_changelog(self):
        """An excerpt is only honest if it says it is one and points at the rest."""
        src = finish_source()
        assert "### Overview" in src, (
            "the trim does not lift the Overview section — a head -c cut lands mid-sentence"
        )
        assert "CHANGELOG.md" in src and "blob/" in src, (
            "a trimmed body must link to the full CHANGELOG entry, or the omitted content "
            "is simply lost to the reader"
        )


class TestTheTrimIsNeverSilent:
    def test_the_substitution_is_recorded_as_a_criterion(self):
        """The whole point: a shortened body is visible in criteria[], not inferred."""
        src = finish_source()
        assert "record changelog-body-within-github-limit" in src, (
            "the body-size decision is not recorded, so a release whose notes were "
            "replaced by an excerpt is indistinguishable from one with complete notes"
        )

    def test_the_criterion_is_declared_in_the_criteria_file(self):
        """criteria-lib.sh exits 2 on a recorded id the YAML does not declare.

        That exit 2 is pipeline-misuse and deliberately distinct from a gate failure, so an
        undeclared id would abort `finish` in a way that reads as neither.
        """
        assert "changelog-body-within-github-limit" in CRITERIA.read_text(encoding="utf-8"), (
            "the criterion is recorded by the stage but not declared in "
            "release-criteria.yaml — criteria-lib.sh will exit 2"
        )

    def test_both_branches_record_the_criterion(self):
        """MUST-STAY-CLEAN: the fits-fine path must record too.

        If only the trimming branch recorded, `criteria_assert_all_checked` would exit 2 on
        every ordinary release whose changelog happens to be small — i.e. the fix would
        break every release except the one it was written for.
        """
        src = finish_source()
        assert src.count("record changelog-body-within-github-limit") >= 2, (
            "only one branch records the criterion; the other path will trip "
            "criteria_assert_all_checked with exit 2"
        )


def test_the_script_is_syntactically_valid():
    """Cheap, and it is the failure mode an editing mistake here actually produces."""
    proc = subprocess.run(
        ["bash", "-n", str(FINISH)], capture_output=True, text=True, check=False, timeout=30
    )
    assert proc.returncode == 0, f"bash -n failed:\n{proc.stderr}"


def test_v050s_own_section_would_have_tripped_both_limits():
    """Anchor the story in the real artifact, so the numbers above are not folklore.

    If someone later trims the v0.5.0 entry, this fails and points at the docstring rather
    than leaving a rationale nobody can check.
    """
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = re.search(r"^## \[0\.5\.0\].*?(?=^## \[)", changelog, re.S | re.M)
    assert section, "no ## [0.5.0] section found"
    size = len(section.group(0))
    assert size > MAX_ARG_STRLEN, (
        f"v0.5.0's section is {size} B, no longer over MAX_ARG_STRLEN — update this test's "
        "docstring if the entry was deliberately shortened"
    )
    assert size > GITHUB_BODY_CAP, f"v0.5.0's section is {size} B, no longer over GitHub's cap"
