"""The trimmed release body must not contain a link that goes nowhere.

REGRESSION, shipped and caught on the real v0.5.0 release (2026-09-14). `95-finish.sh` could
not publish the 350 KB `## [0.5.0]` CHANGELOG section (GitHub caps a release body at ~125,000
characters), so the first fix trimmed it to `### Overview`. That Overview links to
`[Upgrade Notes](#upgrade-notes)` twice — for the AGPL §13 redistribution obligations and for
the breaking-change list — and with the Upgrade Notes section no longer in the body, GitHub
generates no such anchor.

Verified against what was actually published: the body held exactly one heading
(`### Overview`) and referenced exactly one anchor (`#upgrade-notes`). Both links were dead.

That is worse than shipping no link, because a dead anchor is indistinguishable from a live
one until clicked — and these two point at the things a reader must not miss.

`scripts/lib/release_notes_body.py` fixes it twice over: `Upgrade Notes` is kept alongside
`Overview`, and any anchor still referenced with no surviving heading is rewritten to an
absolute CHANGELOG URL. The second rule is the general one — a future Overview may link to
any section at all, so keeping one extra section by name would not have been enough.

These are behavioural tests against the real functions. The sibling
`test_finish_release_body_limits.py` covers the argv/`--notes-file` half statically, which
genuinely cannot be exercised without a 350 KB argv.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "scripts" / "lib" / "release_notes_body.py"

CHANGELOG_URL = "https://github.com/attevon-llc/OpenTranscribe/blob/v0.5.0/CHANGELOG.md"


def _load():
    """Import the real module from `scripts/lib/`, which is not an installed package.

    It must be registered in `sys.modules` BEFORE `exec_module`: `@dataclass` resolves
    `sys.modules[cls.__module__]` while building the class, and an unregistered module makes
    that `None`.
    """
    spec = importlib.util.spec_from_file_location("release_notes_body", MODULE_PATH)
    assert spec and spec.loader, f"cannot load {MODULE_PATH}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rnb = _load()


def make_section(overview_extra: str = "", filler_kb: int = 0) -> str:
    """A changelog entry shaped like the real one: Overview, bulk sections, Upgrade Notes."""
    filler = ("- a fixed thing that is quite wordy indeed\n" * 25 * max(filler_kb, 0)) or "- x\n"
    return (
        "## [9.9.9] - 2026-01-01\n\n"
        "### Overview\n\n"
        "This release does things.\n"
        f"{overview_extra}\n"
        "### Added\n\n"
        f"{filler}\n"
        "### Fixed\n\n"
        f"{filler}\n"
        "### Upgrade Notes\n\n"
        "- **ACTION REQUIRED** the backend will refuse to start without secrets.\n"
    )


class TestTheRegression:
    """A body that references an anchor must provide it, or point somewhere real."""

    def test_the_overviews_upgrade_notes_link_is_not_dead_after_trimming(self):
        """THE bug. Red against the Overview-only trim that shipped."""
        section = make_section(
            overview_extra="\n**Breaking changes** — see [Upgrade Notes](#upgrade-notes).\n",
            filler_kb=40,
        )
        assert len(section) > 1000, "fixture must be big enough to force a trim"

        body, notes = rnb.build_body(section, CHANGELOG_URL, cap=2000)

        assert rnb.dangling_anchors(body) == set(), (
            f"the trimmed body references anchors nothing provides: {rnb.dangling_anchors(body)}"
        )
        assert notes, "a shortened body must report that it was shortened"

    def test_upgrade_notes_survives_the_trim(self):
        """Not merely link-safe — the section itself has to be on the page.

        Rewriting the anchor to the CHANGELOG would satisfy the test above while moving the
        breaking-change list off the release page. Both properties are wanted.
        """
        section = make_section(filler_kb=40)
        body, _ = rnb.build_body(section, CHANGELOG_URL, cap=2000)
        assert "### Upgrade Notes" in body
        assert "ACTION REQUIRED" in body
        assert "### Overview" in body

    def test_a_link_to_a_section_that_did_not_survive_is_rewritten_absolute(self):
        """The GENERAL rule — keeping one section by name does not generalise.

        `### Added` is dropped by design, so a link to it cannot be made live in-page; it
        must resolve to the CHANGELOG instead of dangling.
        """
        section = make_section(
            overview_extra="\nSee [what's new](#added) for the list.\n", filler_kb=40
        )
        body, _ = rnb.build_body(section, CHANGELOG_URL, cap=2000)

        assert "### Added" not in body, "fixture assumption: Added is trimmed away"
        assert f"]({CHANGELOG_URL}#added)" in body, "the dead anchor was not rewritten"
        assert rnb.dangling_anchors(body) == set()


class TestTheUntrimmedPathIsUnchanged:
    """MUST-STAY-CLEAN: a small changelog must publish verbatim."""

    def test_a_section_within_the_cap_is_published_as_is(self):
        section = make_section()
        body, notes = rnb.build_body(section, CHANGELOG_URL, cap=100_000)
        assert body == section, "a body inside the cap must not be rewritten"
        assert notes == [], "nothing happened, so nothing should be reported"

    def test_a_small_section_with_a_live_anchor_keeps_the_in_page_link(self):
        """An anchor that resolves must NOT be rewritten — that would be churn."""
        section = make_section(overview_extra="\nsee [Upgrade Notes](#upgrade-notes).\n")
        body, _ = rnb.build_body(section, CHANGELOG_URL, cap=100_000)
        assert "](#upgrade-notes)" in body
        assert CHANGELOG_URL not in body


class TestTheCapIsAlwaysHonoured:
    def test_the_body_never_exceeds_the_cap(self):
        """The contract the API enforces. Checked even when the kept sections are huge."""
        huge = (
            "## [9.9.9]\n\n### Overview\n\n"
            + ("overview line\n" * 5000)
            + "\n### Upgrade Notes\n\n"
            + ("upgrade line\n" * 5000)
        )
        body, notes = rnb.build_body(huge, CHANGELOG_URL, cap=1500)
        assert len(body) <= 1500, f"body is {len(body)} B, over the 1500 B cap"
        assert any("truncated" in n for n in notes), "a hard truncation must be reported"

    def test_a_section_with_no_recognised_headings_still_fits(self):
        """No Overview, no Upgrade Notes — cut at a boundary, and say so."""
        odd = "## [9.9.9]\n\n### Strange\n\n" + ("x" * 5000)
        body, notes = rnb.build_body(odd, CHANGELOG_URL, cap=1000)
        assert len(body) <= 1000
        assert notes


class TestTheSlugHelperFailsTowardRewriting:
    """Being wrong must cost a redundant rewrite, never a dead link."""

    def test_ordinary_headings_slug_the_way_github_does(self):
        assert rnb.anchor_slug("Upgrade Notes") == "upgrade-notes"
        assert rnb.anchor_slug("Added") == "added"
        assert rnb.anchor_slug("Fixed — testing and release tooling") == (
            "fixed--testing-and-release-tooling"
        )

    def test_an_unmatched_anchor_is_treated_as_dangling(self):
        """The safe direction: unknown => rewrite."""
        text = "### Overview\n\nsee [x](#something-else)\n"
        assert rnb.dangling_anchors(text) == {"something-else"}

    def test_a_matched_anchor_is_not_reported_dangling(self):
        """MUST-STAY-CLEAN control — without it, 'rewrite everything' would pass above."""
        text = "### Overview\n\nsee [x](#overview)\n"
        assert rnb.dangling_anchors(text) == set()


class TestAgainstTheRealChangelog:
    """Anchor the behaviour in the artifact that produced the bug."""

    def test_the_real_v050_section_produces_a_body_with_no_dead_links(self):
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        m = re.search(r"^## \[0\.5\.0\].*?(?=^## \[)", changelog, re.S | re.M)
        assert m, "no ## [0.5.0] section found"
        section = m.group(0)

        body, notes = rnb.build_body(section, CHANGELOG_URL)

        assert len(body) <= rnb.DEFAULT_CAP
        assert rnb.dangling_anchors(body) == set(), (
            f"the real v0.5.0 body still has dead anchors: {rnb.dangling_anchors(body)}"
        )
        assert "### Upgrade Notes" in body, "v0.5.0's breaking changes must be on the page"
        assert "AGPL" in body, "the §13 redistribution notice must survive the trim"
        assert notes, "v0.5.0 is over the cap, so the reduction must be reported"


def test_the_cli_refuses_a_cap_above_githubs_real_limit(tmp_path: Path):
    """A cap that cannot be enforced is not a cap.

    The direction that fails silently: 200000 would satisfy 'a cap exists' while still
    producing a body the API rejects.
    """
    import subprocess

    section = tmp_path / "s.md"
    section.write_text(make_section(), encoding="utf-8")
    proc = subprocess.run(
        [
            "python3",
            str(MODULE_PATH),
            "--section-file",
            str(section),
            "--out",
            str(tmp_path / "out.md"),
            "--cap",
            "200000",
            "--changelog-url",
            CHANGELOG_URL,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 2, "an unenforceable cap must be refused, not accepted"
    assert "GitHub" in proc.stderr
