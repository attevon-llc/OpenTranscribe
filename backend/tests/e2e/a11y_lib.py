"""Shared a11y-scan helpers for ``test_a11y.py`` and ``scripts/update-a11y-baseline.py``.

Deliberately a plain module with no ``pytest`` import, so the standalone regeneration
script can reuse the exact same scan/allowlist logic the E2E test uses without pulling in
pytest fixture machinery.

Issue #785 replaced the old flat, rule-ID-only ``a11y_baseline.json`` with a per-surface,
count-aware, reason-carrying allowlist (``a11y-allowlist.txt``), modelled on the design
``scripts/audit-tests.py`` and ``frontend/test-audit-allowlist.txt`` already ship: a required
written reason, a ``BACKLOG`` prefix for deferred work (counted and printed separately so a
green gate is never read as a clean tree), and a stale entry that fails the run. The old
baseline's one real ratchet property — failing when a rule id everywhere accepted turns out to
be fixed — is preserved, now scoped per surface+rule rather than globally.

Issue #972 added a second dimension: **theme**. The suite used to scan light theme only, so a
colour that clears WCAG AA in light and fails in dark shipped with a fully green gate
(``TasksGrid.svelte``'s dark ``.status-error`` measured 4.38:1 while its light sibling, at
3.29:1, *was* caught). The allowlist key is now ``<surface>::<theme>::<rule id>::<count>`` —
every existing entry was re-keyed, not just new ones added, because the theme is part of what
makes a finding the SAME finding: a structural rule (missing ``<label>``, missing accessible
name) genuinely doesn't vary by ``data-theme``, so it earns one entry per theme with the same
count; a colour-contrast rule is theme-specific by construction and must never be duplicated
across themes with an unmeasured guess for the second one.

``scripts/audit-tests.py``'s ``load_allowlist``/``apply_allowlist`` (~:1860-1925) is the design
this reuses, and its reason-validation primitives (``_NOT_A_REAL_REASON``/``_is_real_reason``/
``_BACKLOG_PREFIX``) are imported from there directly (via :func:`importlib`, the same
technique ``backend/tests/unit/test_audit_tests_selftest.py`` already uses to load that
hyphenated-named script) rather than copied — one vocabulary of "what counts as a real,
written reason", never two that can drift apart. What is genuinely NOT reused is the finding
container itself: ``audit-tests.py``'s allowlist counts occurrences by REPEATING a key (one
line per occurrence), because its findings have no natural numeric count. An axe violation
does — ``len(violation["nodes"])`` — so this allowlist encodes the count as a fourth field in
the key instead (``<surface>::<theme>::<rule id>::<count>``), which is different enough that
forcing one parser to serve both shapes would produce exactly the "handles both awkwardly"
third design "reuse the parser shape" was written to prevent.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from types import ModuleType
from typing import Any

from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page
from timeouts import LOGIN_FORM_READY_MS

# Impacts we gate on. "minor"/"moderate" are tolerated (legacy debt, low value).
GATED_IMPACTS = frozenset({"serious", "critical"})

# The per-surface, count-aware allowlist. Committed to git so the test is a regression guard,
# not a snapshot of today's debt.
ALLOWLIST_PATH = Path(__file__).parent / "a11y-allowlist.txt"

#: Every surface ``test_a11y.py`` scans (or deliberately deselects by marker). An allowlist
#: entry naming any other string is a typo, not a new exemption — reject it outright rather
#: than let it sit in the file matching nothing forever.
#:
#: ``file-status-badges`` (issue #972) is NOT the live ``/file-status`` page — it is
#: ``/a11y-fixtures/status-badges``, a dedicated fixture route that renders one
#: ``TasksGrid.svelte`` task per status (pending/in_progress/completed/failed)
#: unconditionally. The live page's badge count depends on which task states happen to be
#: queued when the scan runs (that dependency is exactly how ``file-status::color-contrast``
#: was allowlisted at ``1`` and later observed at ``13`` — more failing tasks on screen, same
#: single defect); this surface exists so the badge contrast rules are gated on the CSS, not
#: on fixture data. It does not replace ``file-status``, which still covers the live page's
#: other controls (filters, selects).
KNOWN_SURFACES = frozenset(
    {
        "gallery",
        "settings-modal",
        "speakers",
        "search",
        "chat",
        "file-detail",
        "file-status",
        "file-status-badges",
        "upload",
    }
)

#: The two themes every surface is scanned in (issue #972). ``data-theme`` only ever takes
#: these two values (``src/stores/theme.js``) — a third string in the allowlist is a typo.
KNOWN_THEMES = frozenset({"light", "dark"})


def _load_audit_tests_module() -> ModuleType:
    """Import ``scripts/audit-tests.py`` for its reason-validation primitives.

    Its name is hyphenated, which blocks a normal ``import`` — the same problem
    ``test_audit_tests_selftest.py`` solves with ``importlib.util.spec_from_file_location``.
    Reused here rather than reinvented so both allowlists reject the exact same placeholder
    reasons under the exact same rule, forever, with one change site.
    """
    path = Path(__file__).resolve().parents[3] / "scripts" / "audit-tests.py"
    spec = importlib.util.spec_from_file_location("_a11y_audit_tests_reuse", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec_module: audit-tests.py's `@dataclass` classes resolve their
    # `from __future__ import annotations` string types through `sys.modules[cls.__module__]`,
    # which is None for an unregistered module — an AttributeError deep inside dataclasses.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_audit_tests = _load_audit_tests_module()

#: Reason prefix marking an entry as DEFERRED WORK rather than an accepted pattern. Counted and
#: reported separately on every run.
BACKLOG_PREFIX: str = _audit_tests._BACKLOG_PREFIX

#: Reasons that read as "no justification" rather than a real, written one.
_is_real_reason = _audit_tests._is_real_reason


class AllowlistFormatError(ValueError):
    """An ``a11y-allowlist.txt`` entry is malformed.

    Raised (never silently defaulted) for: a key that isn't exactly
    ``<surface>::<theme>::<rule id>::<count>``, a surface not in :data:`KNOWN_SURFACES`, a
    theme not in :data:`KNOWN_THEMES`, a non-positive or non-integer count, a duplicate
    ``surface::theme::rule_id`` key, or a reason ``_is_real_reason`` (imported from
    ``scripts/audit-tests.py``) rejects. Same philosophy as that module's
    ``AllowlistReasonError``: a bad entry must fail the run rather than sit in the allowlist
    looking reviewed.
    """


@dataclass(frozen=True)
class AllowlistEntry:
    """One accepted (or deferred) ``<surface>, <theme>, <rule id>`` triple and its node count."""

    surface: str
    theme: str
    rule_id: str
    count: int
    reason: str
    lineno: int

    @property
    def is_backlog(self) -> bool:
        """True when this entry marks deferred work rather than an accepted pattern."""
        return self.reason.startswith(BACKLOG_PREFIX)


def parse_allowlist_text(
    text: str, *, source: object = "<text>"
) -> dict[tuple[str, str, str], AllowlistEntry]:
    """Parse allowlist lines, REJECTING any malformed or reason-less entry.

    Split out from :func:`load_allowlist` so both the CLI (given a file on disk) and a
    ``--selftest``-style caller (given an in-memory string) exercise the identical rejection
    path. Returns a map of ``(surface, theme, rule_id)`` to the single :class:`AllowlistEntry`
    for that triple — unlike ``audit-tests.py``'s allowlist, a duplicate key here is a
    formatting error (one line already carries the count), not a second, independent
    occurrence.
    """
    entries: dict[tuple[str, str, str], AllowlistEntry] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key_part, _, reason = line.partition("#")
        reason = reason.strip()
        parts = [p.strip() for p in key_part.strip().split("::")]
        if len(parts) != 4:
            raise AllowlistFormatError(
                f"{source}:{lineno}: expected `<surface>::<theme>::<rule id>::<count>`, "
                f"got {key_part.strip()!r}"
            )
        surface, theme, rule_id, count_str = parts
        if surface not in KNOWN_SURFACES:
            raise AllowlistFormatError(
                f"{source}:{lineno}: unknown surface {surface!r} — must be one of "
                f"{sorted(KNOWN_SURFACES)}"
            )
        if theme not in KNOWN_THEMES:
            raise AllowlistFormatError(
                f"{source}:{lineno}: unknown theme {theme!r} — must be one of "
                f"{sorted(KNOWN_THEMES)}"
            )
        if not rule_id:
            raise AllowlistFormatError(f"{source}:{lineno}: empty axe rule id")
        if not count_str.isdigit() or int(count_str) < 1:
            raise AllowlistFormatError(
                f"{source}:{lineno}: count must be a positive integer, got {count_str!r}"
            )
        if not _is_real_reason(reason):
            raise AllowlistFormatError(
                f"{source}:{lineno}: entry for `{surface}::{theme}::{rule_id}` has no real "
                f"reason (got {reason!r}). A written reason is mandatory."
            )
        key = (surface, theme, rule_id)
        if key in entries:
            raise AllowlistFormatError(
                f"{source}:{lineno}: duplicate entry for `{surface}::{theme}::{rule_id}` "
                f"(already defined at line {entries[key].lineno}) — one line per "
                "surface+theme+rule; raise the count instead of adding a second line."
            )
        entries[key] = AllowlistEntry(surface, theme, rule_id, int(count_str), reason, lineno)
    return entries


def load_allowlist(path: Path = ALLOWLIST_PATH) -> dict[tuple[str, str, str], AllowlistEntry]:
    """Load and parse the a11y allowlist, or ``{}`` if it does not exist."""
    if not path.exists():
        return {}
    return parse_allowlist_text(path.read_text(), source=path)


def gated_violations(results: Any) -> list[dict[str, Any]]:
    """Return only the serious/critical violations from an axe result."""
    return [v for v in results.response["violations"] if v.get("impact") in GATED_IMPACTS]


def run_axe(page: Page) -> Any:
    """Run axe-core against the current page, reporting only violations."""
    axe = Axe()
    return axe.run(page)


def form_login_with_retry(page: Page, base_url: str, attempts: int = 4) -> None:
    """Submit the login form, retrying through transient auth rate-limiting."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(base_url)
            # Already authenticated (cookie still valid) — no form to fill.
            if page.locator(".user-button").count():
                page.wait_for_selector(".user-button", timeout=10000)
                return
            page.wait_for_selector("#email", timeout=LOGIN_FORM_READY_MS)
            page.fill("#email", "admin@example.com")
            page.fill("#password", "password")
            page.click("button[type=submit]")
            page.wait_for_selector(".user-button", timeout=20000)
            return
        except Exception as exc:  # noqa: BLE001 - retry on any login-flow failure
            last_error = exc
            # Kept deliberately: this wait IS the rate-limit backoff, not a settle for
            # something a locator could poll for (issue #431).
            page.wait_for_timeout(5000 * (attempt + 1))
    raise AssertionError(f"Could not log in via form after {attempts} attempts: {last_error}")


def set_theme(page: Page, theme: str) -> None:
    """Force the app's ``data-theme`` for the current page's context, and reload to apply it.

    ``src/stores/theme.js`` reads ``localStorage.getItem('theme')`` SYNCHRONOUSLY at module
    load time — before Svelte mounts anything — and writes ``data-theme`` on ``<html>``
    immediately, specifically to avoid a flash of the wrong theme. Writing ``localStorage``
    or the DOM attribute AFTER the page has already loaded races that early read: the theme
    store's own subscription may not re-fire, and even where it does, axe would be reading
    computed styles that had a moment to settle on the OLD theme first.

    An init script registered on the page's ``BrowserContext`` runs before any of the page's
    own scripts on every subsequent navigation in that context, which is the only point early
    enough to win the race — hence the ``page.reload()`` immediately after registering it (the
    script does not apply retroactively to the already-loaded page). Every test using this
    gets its own fresh context (``authed_page``/``browser.new_context`` are function-scoped),
    so this does not leak into any other test's theme.
    """
    theme = theme.strip()
    if theme not in KNOWN_THEMES:
        raise ValueError(f"set_theme: unknown theme {theme!r} — must be one of {KNOWN_THEMES}")
    page.context.add_init_script(f"window.localStorage.setItem('theme', {theme!r});")
    page.reload()
    page.wait_for_function(
        "expected => document.documentElement.getAttribute('data-theme') === expected",
        arg=theme,
        timeout=10000,
    )


@dataclass
class SurfaceResult:
    """One page's axe results, evaluated against the allowlist for that surface+theme."""

    surface: str
    theme: str
    #: rule_id -> total node count observed on this surface+theme, this run.
    observed: dict[str, int] = field(default_factory=dict)
    #: Human-readable lines for rule ids axe found that the allowlist does not cover at all.
    new_violations: list[str] = field(default_factory=list)
    #: Human-readable lines for rule ids whose observed node count exceeds the allowlisted one.
    exceeded: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[str]:
        """All failure lines for this surface+theme, new violations first."""
        return [*self.new_violations, *self.exceeded]


def evaluate_surface(
    surface: str,
    theme: str,
    results: Any,
    allowlist: dict[tuple[str, str, str], AllowlistEntry],
) -> SurfaceResult:
    """Check one page's axe results against the allowlist: per-surface, per-theme, count-aware.

    A rule id absent from the allowlist for this surface+theme is a regression, exactly like
    the old baseline. Unlike the old baseline, a rule id THAT IS allowlisted but whose observed
    node count exceeds the accepted count is *also* a regression — the ratchet's "one line buys
    N nodes, not a blanket" property (issue #785 §4.2). The theme is part of the key (issue
    #972): a light-theme allowlist entry does NOT cover the same rule id observed in dark, and
    vice versa — that separation is the entire point of scanning both themes.
    """
    observed: dict[str, int] = {}
    for violation in gated_violations(results):
        rule_id = violation["id"]
        observed[rule_id] = observed.get(rule_id, 0) + len(violation.get("nodes", []))

    new_violations: list[str] = []
    exceeded: list[str] = []
    for rule_id, count in sorted(observed.items()):
        entry = allowlist.get((surface, theme, rule_id))
        if entry is None:
            new_violations.append(
                f"  - {rule_id}: {count} node(s) — not allowlisted for surface {surface!r} "
                f"theme {theme!r}"
            )
        elif count > entry.count:
            exceeded.append(
                f"  - {rule_id}: {count} node(s) exceeds the allowlisted {entry.count} "
                f"for surface {surface!r} theme {theme!r} (line {entry.lineno})"
            )
    return SurfaceResult(surface, theme, observed, new_violations, exceeded)
