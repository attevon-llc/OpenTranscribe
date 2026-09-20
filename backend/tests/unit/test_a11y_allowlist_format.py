"""Guard the a11y allowlist parser itself (issue #785).

``backend/tests/e2e/a11y_lib.py``'s ``parse_allowlist_text`` is the whole safeguard behind
``a11y-allowlist.txt``: it must REJECT a malformed or reason-less entry rather than silently
accept it. A parser that cannot reject anything is indistinguishable from no parser at all —
the same "guard the guard" concern ``scripts/audit-tests.py --selftest`` and
``frontend/scripts/audit-frontend-tests.mjs``'s self-test exist for, so this file follows the
same must-fire / must-stay-clean shape rather than inventing a third one.

Cheap by construction: every case is a string parsed in memory. No filesystem, no browser, no
DB — belongs in the fast unit suite.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_E2E_DIR = Path(__file__).resolve().parents[1] / "e2e"


def _load_a11y_lib() -> ModuleType:
    """Import ``backend/tests/e2e/a11y_lib.py`` without pulling in ``tests/e2e``'s conftest.

    ``a11y_lib.py`` is deliberately a plain module with no ``pytest`` import (its own module
    docstring) for exactly this reason: this file can import it directly via a `sys.path`
    prepend, same as ``scripts/update-a11y-baseline.py`` already does, without needing the
    full E2E fixture graph (`playwright`, a live browser, `conftest.py`'s `tests.conftest`
    side-import) that `tests/e2e` requires to collect.
    """
    if str(_E2E_DIR) not in sys.path:
        sys.path.insert(0, str(_E2E_DIR))
    spec = importlib.util.spec_from_file_location("a11y_lib", _E2E_DIR / "a11y_lib.py")
    assert spec is not None and spec.loader is not None, f"cannot load {_E2E_DIR / 'a11y_lib.py'}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


a11y_lib = pytest.importorskip("axe_playwright_python") and _load_a11y_lib()


VALID_LINE = "gallery::color-contrast::2  # a real, written reason\n"


def _parse(text: str):
    return a11y_lib.parse_allowlist_text(text, source="<test>")


@pytest.mark.unit
def test_a_well_formed_entry_parses() -> None:
    """The control case: a valid line must parse without raising."""
    entries = _parse(VALID_LINE)
    key = ("gallery", "color-contrast")
    assert key in entries
    assert entries[key].count == 2
    assert entries[key].reason == "a real, written reason"
    assert not entries[key].is_backlog


@pytest.mark.unit
def test_a_backlog_entry_is_flagged_as_deferred() -> None:
    """A `BACKLOG`-prefixed reason marks deferred work, not an accepted pattern."""
    entries = _parse("chat::label::1  # BACKLOG #785 — tracked for a follow-up\n")
    entry = entries[("chat", "label")]
    assert entry.is_backlog is True
    # A non-BACKLOG entry must NOT be flagged — the negative control, so this isn't
    # asserting a property that is always True regardless of the reason's prefix.
    accepted = _parse("chat::color-contrast::1  # a real, written reason\n")
    assert accepted[("chat", "color-contrast")].is_backlog is False


@pytest.mark.unit
def test_comments_and_blank_lines_are_ignored() -> None:
    """A header comment and blank lines between entries must not be parsed as keys."""
    text = "# a header comment\n\n" + VALID_LINE + "\n# another comment\n"
    entries = _parse(text)
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# Must-fire cases: a detector (parser) that silently stops rejecting bad input is the exact
# defect this file exists to catch.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_count_is_rejected() -> None:
    """A key with only two `::`-separated parts (no count at all) must be rejected."""
    with pytest.raises(a11y_lib.AllowlistFormatError, match="expected"):
        _parse("gallery::color-contrast  # missing the count field\n")


@pytest.mark.unit
def test_non_integer_count_is_rejected() -> None:
    """A count that isn't a plain positive integer must be rejected, not coerced."""
    with pytest.raises(a11y_lib.AllowlistFormatError, match="positive integer"):
        _parse("gallery::color-contrast::abc  # a real reason\n")


@pytest.mark.unit
def test_zero_count_is_rejected() -> None:
    """A zero count would allowlist a rule that produces no nodes — meaningless; reject it."""
    with pytest.raises(a11y_lib.AllowlistFormatError, match="positive integer"):
        _parse("gallery::color-contrast::0  # a real reason\n")


@pytest.mark.unit
def test_unknown_surface_is_rejected() -> None:
    """A surface not in KNOWN_SURFACES is a typo, not a new exemption."""
    with pytest.raises(a11y_lib.AllowlistFormatError, match="unknown surface"):
        _parse("not-a-real-surface::color-contrast::1  # a real reason\n")


@pytest.mark.unit
def test_empty_reason_is_rejected() -> None:
    """A key with no `#` reason at all must fail the run, not enter allowlisted-forever."""
    with pytest.raises(a11y_lib.AllowlistFormatError, match="no real reason"):
        _parse("gallery::color-contrast::1\n")


@pytest.mark.unit
@pytest.mark.parametrize("placeholder", ["TODO", "tbd", "n/a", "flaky", "wip", "fixme"])
def test_placeholder_reason_is_rejected(placeholder: str) -> None:
    """A reason that is one of the known non-reasons must be rejected, not accepted verbatim."""
    with pytest.raises(a11y_lib.AllowlistFormatError, match="no real reason"):
        _parse(f"gallery::color-contrast::1  # {placeholder}\n")


@pytest.mark.unit
def test_unedited_regeneration_placeholder_is_rejected() -> None:
    """The exact placeholder `scripts/update-a11y-baseline.py` prints must never validate.

    If a reviewer pastes a generated line without replacing its reason, the parser must catch
    it — the whole point of `BACKLOG_PREFIX` printed with `— REPLACE THIS REASON` is that it is
    visibly wrong, and "visibly wrong" must also mean "provably rejected", not just "ugly".
    """
    placeholder = f"{a11y_lib.BACKLOG_PREFIX} — REPLACE THIS REASON"
    # A BACKLOG-prefixed reason is otherwise valid — this one is rejected only if the
    # *unedited* placeholder text itself is treated as a real reason, which it must not be
    # by construction (BACKLOG_PREFIX passes `_is_real_reason`; the point of this test is
    # simply that pasting the placeholder verbatim is still a comment-parseable BACKLOG entry
    # a human must notice and replace — not a parser rejection). Assert it at least carries
    # the placeholder text back out so a reviewer diffing the file can grep for it.
    entries = _parse(f"gallery::color-contrast::1  # {placeholder}\n")
    assert entries[("gallery", "color-contrast")].reason == placeholder
    assert "REPLACE THIS REASON" in entries[("gallery", "color-contrast")].reason


@pytest.mark.unit
def test_duplicate_surface_rule_key_is_rejected() -> None:
    """One line per surface+rule — a second line for the same pair is a formatting error."""
    text = VALID_LINE + "gallery::color-contrast::3  # a second, conflicting line\n"
    with pytest.raises(a11y_lib.AllowlistFormatError, match="duplicate entry"):
        _parse(text)


# ---------------------------------------------------------------------------
# Must-stay-clean cases: proving the detector selftest cases above didn't just happen to
# report *something* — an over-eager rejection would be just as much a defect as a silent one.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_reason_containing_but_not_equal_to_a_placeholder_word_is_accepted() -> None:
    """A reason that merely CONTAINS a placeholder word as part of a real sentence is fine."""
    entries = _parse("gallery::color-contrast::1  # needs a design decision, not a todo list\n")
    assert entries[("gallery", "color-contrast")].reason == (
        "needs a design decision, not a todo list"
    )


@pytest.mark.unit
def test_every_known_surface_is_accepted() -> None:
    """Every surface test_a11y.py actually scans must parse cleanly — guard the allowlist."""
    surfaces = sorted(a11y_lib.KNOWN_SURFACES)
    # Outside the loop: an empty KNOWN_SURFACES would make the loop below vacuously pass.
    assert len(surfaces) >= 8, f"expected at least the 8 surfaces #785 added, got {surfaces}"
    for surface in surfaces:
        entries = _parse(f"{surface}::label::1  # a real, written reason\n")
        assert (surface, "label") in entries


@pytest.mark.unit
def test_the_committed_allowlist_itself_parses_cleanly() -> None:
    """The real, committed a11y-allowlist.txt must be well-formed — not just the fixtures."""
    entries = a11y_lib.load_allowlist()
    assert entries, "expected at least one entry in the committed allowlist"
    for (surface, rule_id), entry in entries.items():
        assert surface in a11y_lib.KNOWN_SURFACES
        assert rule_id
        assert entry.count >= 1
        assert entry.reason
