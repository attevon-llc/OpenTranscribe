"""Every masked selector in the visual suite must still exist in the frontend.

The visual-regression suite masks volatile regions (live counters, per-stage
timings, telemetry gauges) out of its screenshots. Playwright treats a locator
that matches nothing as a **silent no-op**, so a renamed CSS class does not
error — it quietly stops masking, and the volatile region drifts back into the
baseline with nothing in the run saying so.

`test_visual_regression.py` used to guard that at runtime, by asserting each
surface masked at least one element. That check could not tell two situations
apart:

* the selector is stale (a real defect), and
* the app correctly chose not to render the element.

`.notification-badge` is `{#if $unreadCount > 0}` in `Navbar.svelte`, so it
renders zero elements on a freshly seeded stack. `file_detail`, whose entire
mask list is that one selector, therefore FAILED both themes on a clean
isolated stack — the exact stack the suite's own docstring requires — while
PASSING on the shared dev stack that happened to carry unread notifications.
The runtime assertion was reporting the state of the data, not the health of
the selector.

This module makes the rename check **static**: every selector the visual suite
declares must appear in the frontend source. It needs no browser, no stack and
no seeded data, and it cannot be fooled by what one capture happened to render.
The runtime assertion still runs, but only for selectors NOT declared
conditional.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VISUAL_TEST = REPO_ROOT / "backend" / "tests" / "e2e" / "test_visual_regression.py"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"


def _assigned_value(tree: ast.Module, name: str) -> ast.expr:
    """Return the value node assigned to a module-level ``name``."""
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                assert node.value is not None
                return node.value
    # `raise`, not `pytest.fail`: this is a module-level helper, and an explicit
    # raise is what makes the no-fall-through path checkable by mypy.
    raise AssertionError(f"{name} not found in {VISUAL_TEST.name} — renamed or removed?")


def _string_literals(node: ast.expr) -> list[str]:
    """Every string constant reachable from *node*."""
    return [
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _declared_selectors() -> list[str]:
    tree = ast.parse(VISUAL_TEST.read_text(encoding="utf-8"))
    return _string_literals(_assigned_value(tree, "_VOLATILE_SELECTORS"))


def _conditional_selectors() -> set[str]:
    tree = ast.parse(VISUAL_TEST.read_text(encoding="utf-8"))
    return set(_string_literals(_assigned_value(tree, "_CONDITIONAL_SELECTORS")))


#: Tokens pulled out of a compound selector, e.g.
#: ``.settings-modal .stats-grid`` -> {"settings-modal", "stats-grid"}.
_CLASS_TOKEN = re.compile(r"\.([A-Za-z][\w-]*)")
_TESTID_TOKEN = re.compile(r'\[data-testid="([^"]+)"\]')


def _tokens(selector: str) -> set[str]:
    return set(_CLASS_TOKEN.findall(selector)) | set(_TESTID_TOKEN.findall(selector))


def _frontend_text() -> str:
    """Concatenated frontend source. Read once per call, small enough to be cheap."""
    parts: list[str] = []
    for path in FRONTEND_SRC.rglob("*"):
        if path.suffix in {".svelte", ".ts", ".js", ".css", ".html"} and path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def test_declared_selectors_are_not_empty() -> None:
    """Guard the guard: a parser that finds nothing would pass every check below."""
    selectors = _declared_selectors()
    assert len(selectors) >= 8, (
        f"only {len(selectors)} selector(s) parsed out of _VOLATILE_SELECTORS — the "
        f"AST extraction is broken, and every assertion in this module is vacuous."
    )


def test_every_masked_selector_still_exists_in_the_frontend() -> None:
    """A renamed class silently stops masking; this is what notices."""
    haystack = _frontend_text()
    assert haystack, f"no frontend sources read from {FRONTEND_SRC} — check the path"

    missing: list[str] = []
    for selector in _declared_selectors():
        for token in _tokens(selector):
            if token not in haystack:
                missing.append(f"{selector}  (token {token!r})")

    assert not missing, (
        "These selectors are masked by backend/tests/e2e/test_visual_regression.py "
        "but no longer appear anywhere in frontend/src. Playwright masks nothing for "
        "an unmatched locator, so the volatile region they were hiding is now being "
        "baked into the committed baseline:\n  " + "\n  ".join(missing)
    )


def test_conditional_selectors_are_a_subset_of_declared_ones() -> None:
    """A conditional entry that masks nothing is a stale exemption.

    `_CONDITIONAL_SELECTORS` suppresses the runtime match requirement. An entry
    naming a selector no surface declares suppresses nothing and only makes the
    exemption list look better maintained than it is.
    """
    declared = set(_declared_selectors())
    orphans = sorted(_conditional_selectors() - declared)
    assert not orphans, (
        "_CONDITIONAL_SELECTORS names selectors that no surface in "
        f"_VOLATILE_SELECTORS declares: {orphans}. Delete them — an exemption "
        "that exempts nothing outlives its subject."
    )


def test_notification_badge_is_genuinely_conditional() -> None:
    """The one exemption we carry must stay justified by the source.

    If the badge ever renders unconditionally, the exemption becomes a hole: a
    rename of `.notification-badge` would then stop masking a region that IS
    always on screen, and nothing would fail.
    """
    if ".notification-badge" not in _conditional_selectors():
        pytest.skip("the badge is no longer exempted — nothing to justify")

    navbar = FRONTEND_SRC / "components" / "Navbar.svelte"
    assert navbar.exists(), f"{navbar} moved — re-verify why the badge is exempt"
    source = navbar.read_text(encoding="utf-8")

    guarded = re.search(
        r"\{#if\s+\$unreadCount\s*>\s*0\s*\}\s*.*?notification-badge",
        source,
        re.S,
    )
    assert guarded, (
        "`.notification-badge` is exempted from the runtime mask check on the "
        "grounds that it is conditionally rendered, but Navbar.svelte no longer "
        "guards it with `{#if $unreadCount > 0}`. Either restore the guard or "
        "drop the exemption — as written the check now passes vacuously."
    )
