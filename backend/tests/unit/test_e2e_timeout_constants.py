"""Guard: every positive wait on an app-shell landmark must use ``APP_SHELL_READY_MS``.

**Why this exists.** ``tests/e2e/timeouts.py`` was created to stop a wait budget being
copy-pasted until it could only drift — the ``#email`` wait had reached **65 literals across
15 files at two different values**. It centralised that one selector as
``LOGIN_FORM_READY_MS`` and, by the time it landed, *the same drift had already recurred on
the other app-shell selectors*: ``.gallery-action-buttons`` / ``.gallery-header-right`` /
``.search-page`` sat at **three different values (5000, 10000, 15000, 30000) across 23 call
sites**, including two sites in one file where one had been raised and the other had not.

A comment asking people to use the constant does not survive that. This test does: it parses
``tests/e2e/*.py`` and fails when a positive wait on one of those landmarks carries a budget
that is not the named constant.

**Design notes, because a guard that becomes a nuisance gets suppressed.**

- **There is no allowlist and no waiver file.** Every exemption this rule needs is part of the
  rule itself (below), so there is nothing to append a line to. The `audit-allowlist.txt`
  experience is that a keyed waiver is the path of least resistance and gets taken.
- **Only *positive* waits are in scope** (``wait_for_selector`` / ``to_be_visible`` /
  ``to_be_attached`` / ``to_be_in_viewport``). An *absence* assertion —
  ``to_have_count(0)``, ``not_to_be_visible``, ``to_be_hidden`` — is not a readiness budget:
  waiting 30 s to confirm something is still missing is wrong, not consistent. Those never
  fire, so nobody is ever pushed to suppress the guard to write one.
- **A correct-valued literal still fires.** ``timeout=30000`` happens to equal the constant
  today; that is precisely the state the ``#email`` sites were in before someone revised one
  of them. The rule is about the *form*, because the form is what makes a future revision
  reach every site.
- **A pass-through parameter is allowed, but only if its default is the constant.** That
  closes the hole this tree actually contained: ``test_mfa.py``'s
  ``_wait_for_gallery(page, timeout=15000)`` waited ``timeout * 2`` — a 30 s budget spelled
  as arithmetic on an unrelated 15 s default, invisible to any grep for ``30000``.
- **Every detector here has a must-fire *and* a must-stay-clean case.** A scanner that
  silently matches nothing reports zero findings, which is indistinguishable from a clean
  tree — that failure mode has shipped in this repo twice.
"""

from __future__ import annotations

import ast
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parents[1] / "e2e"
TIMEOUTS_MODULE = E2E_DIR / "timeouts.py"

#: The name every app-shell wait budget must be spelled as.
CONSTANT = "APP_SHELL_READY_MS"

#: The post-login landmarks ``timeouts.py`` documents. Each one sits behind
#: ``+layout.svelte``'s ``{#if $authReady}``, so reaching it costs the app shell plus
#: ``initAuth()``'s ``GET /auth/session`` — which is why they share one budget.
APP_SHELL_SELECTORS = frozenset(
    {".gallery-action-buttons", ".gallery-header-right", ".search-page"}
)

#: Playwright calls that wait for something to *appear*. Deliberately an explicit set rather
#: than "anything that is not a negative assertion": a novel positive method escapes the
#: guard, which is a miss, whereas guessing wrong produces a false failure on an absence
#: assertion — and a false failure is what teaches people to suppress a guard.
POSITIVE_WAITS = frozenset(
    {"wait_for_selector", "to_be_visible", "to_be_attached", "to_be_in_viewport"}
)


class _Scanner(ast.NodeVisitor):
    """Collect app-shell waits whose ``timeout=`` is not ``APP_SHELL_READY_MS``."""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.findings: list[str] = []
        # Innermost-last stack of {param_name: default_expr_or_None} for enclosing functions.
        self._scopes: list[dict[str, ast.expr | None]] = []

    # -- scope tracking ----------------------------------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scopes.append(_param_defaults(node))
        self.generic_visit(node)
        self._scopes.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scopes.append(_param_defaults(node))
        self.generic_visit(node)
        self._scopes.pop()

    # -- the rule ----------------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in POSITIVE_WAITS:
            return
        selectors = _app_shell_selectors_in(node)
        if not selectors:
            return
        timeout = _timeout_kwarg(node)
        if timeout is None:
            # No budget at all means Playwright's own default. That is a different
            # argument from "wrong budget" and this guard does not make it.
            return
        if self._resolves_to_constant(timeout):
            return
        self.findings.append(
            f"{self.rel}:{node.lineno}: {sorted(selectors)[0]} wait uses "
            f"timeout={ast.unparse(timeout)} — must be {CONSTANT} "
            f"(from tests/e2e/timeouts.py)"
        )

    def _resolves_to_constant(self, expr: ast.expr) -> bool:
        if not isinstance(expr, ast.Name):
            # A literal, or arithmetic on one (`timeout * 2`), or an attribute lookup.
            return False
        if expr.id == CONSTANT:
            return True
        for scope in reversed(self._scopes):
            if expr.id in scope:
                default = scope[expr.id]
                return isinstance(default, ast.Name) and default.id == CONSTANT
        return False


def _param_defaults(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, ast.expr | None]:
    """Map each parameter of ``fn`` to its default expression (``None`` when it has none)."""
    args = fn.args
    positional = [*args.posonlyargs, *args.args]
    defaults: dict[str, ast.expr | None] = {a.arg: None for a in positional}
    with_defaults = positional[len(positional) - len(args.defaults) :]
    for arg, default in zip(with_defaults, args.defaults, strict=True):
        defaults[arg.arg] = default
    for arg, kw_default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        defaults[arg.arg] = kw_default
    return defaults


def _timeout_kwarg(call: ast.Call) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == "timeout":
            return keyword.value
    return None


def _app_shell_selectors_in(call: ast.Call) -> set[str]:
    """Selector literals reachable from ``call``, including through the ``expect(...)`` receiver.

    ``ast.walk`` descends into ``call.func``, so this finds the selector in both shapes the
    suite uses: ``page.wait_for_selector(".search-page", ...)`` (a direct argument) and
    ``expect(page.locator(".search-page")).to_be_visible(...)`` (inside the receiver).
    """
    return {
        node.value
        for node in ast.walk(call)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in APP_SHELL_SELECTORS
    }


def scan_source(source: str, rel: str = "<memory>") -> list[str]:
    """Return one finding string per offending app-shell wait in ``source``."""
    scanner = _Scanner(rel)
    scanner.visit(ast.parse(source))
    return scanner.findings


# ---------------------------------------------------------------------------------------
# Must-fire cases — a detector nobody has watched fire is not a detector.
# ---------------------------------------------------------------------------------------


def test_fires_on_a_raw_numeric_literal() -> None:
    findings = scan_source(
        'def test_x(page):\n    page.wait_for_selector(".gallery-action-buttons", timeout=15000)\n'
    )
    assert len(findings) == 1, findings
    assert "timeout=15000" in findings[0]
    assert CONSTANT in findings[0]


def test_fires_on_an_expect_receiver_selector() -> None:
    findings = scan_source(
        'def test_x(page):\n    expect(page.locator(".search-page")).to_be_visible(timeout=5000)\n'
    )
    assert len(findings) == 1, findings
    assert ".search-page" in findings[0]


def test_fires_on_a_derived_timeout() -> None:
    """``timeout * 2`` is a 30 s budget no grep for ``30000`` can find."""
    findings = scan_source(
        "def _wait(page, timeout=15000):\n"
        '    page.wait_for_selector(".gallery-action-buttons", timeout=timeout * 2)\n'
    )
    assert len(findings) == 1, findings
    assert "timeout * 2" in findings[0]


def test_fires_on_a_parameter_whose_default_is_a_literal() -> None:
    findings = scan_source(
        "def _wait(page, timeout=15000):\n"
        '    page.wait_for_selector(".gallery-header-right", timeout=timeout)\n'
    )
    assert len(findings) == 1, findings
    assert ".gallery-header-right" in findings[0]


def test_fires_on_a_module_level_wait() -> None:
    """A call outside any function has no parameter to hide behind."""
    findings = scan_source('page.wait_for_selector(".search-page", timeout=10000)\n')
    assert len(findings) == 1, findings


# ---------------------------------------------------------------------------------------
# Must-stay-clean cases — the exemptions are part of the rule, not a waiver file.
# ---------------------------------------------------------------------------------------


def test_clean_when_the_constant_is_used() -> None:
    findings = scan_source(
        "def test_x(page):\n"
        '    page.wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)\n'
    )
    assert findings == []


def test_clean_for_a_parameter_defaulting_to_the_constant() -> None:
    findings = scan_source(
        "def _wait(page, timeout=APP_SHELL_READY_MS):\n"
        '    page.wait_for_selector(".gallery-action-buttons", timeout=timeout)\n'
    )
    assert findings == []


def test_clean_for_absence_assertions() -> None:
    """Waiting 30 s to confirm something is still missing is wrong, not consistent."""
    findings = scan_source(
        "def test_x(page):\n"
        '    expect(page.locator(".gallery-action-buttons")).to_have_count(0)\n'
        '    expect(page.locator(".search-page")).not_to_be_visible(timeout=3000)\n'
        '    expect(page.locator(".gallery-header-right")).to_be_hidden(timeout=3000)\n'
    )
    assert findings == []


def test_clean_for_selectors_that_are_not_app_shell_landmarks() -> None:
    findings = scan_source(
        "def test_x(page):\n"
        '    page.wait_for_selector(".file-card", timeout=15000)\n'
        '    expect(page.locator(".select-all-btn")).to_be_visible(timeout=5000)\n'
    )
    assert findings == []


def test_clean_when_no_timeout_is_given() -> None:
    findings = scan_source(
        'def test_x(page):\n    expect(page.locator(".gallery-action-buttons")).to_be_visible()\n'
    )
    assert findings == []


# ---------------------------------------------------------------------------------------
# The gate itself.
# ---------------------------------------------------------------------------------------


def test_timeouts_module_defines_the_constant() -> None:
    """The guard names a constant; if it ever stops existing, every adoption site is wrong."""
    assert TIMEOUTS_MODULE.is_file(), f"{TIMEOUTS_MODULE} is missing"
    tree = ast.parse(TIMEOUTS_MODULE.read_text(encoding="utf-8"))
    assigned = {
        target.id
        for stmt in tree.body
        if isinstance(stmt, ast.Assign)
        for target in stmt.targets
        if isinstance(target, ast.Name)
    }
    assert CONSTANT in assigned, (
        f"tests/e2e/timeouts.py must define {CONSTANT}; found {sorted(assigned)}"
    )


def test_the_scanner_is_pointed_at_real_content() -> None:
    """Guard the guard: zero findings must mean 'clean', never 'scanned nothing'."""
    sources = sorted(E2E_DIR.glob("*.py"))
    assert len(sources) > 10, f"expected the e2e suite under {E2E_DIR}, found {len(sources)} files"
    with_landmarks = [
        path
        for path in sources
        if any(selector in path.read_text(encoding="utf-8") for selector in APP_SHELL_SELECTORS)
    ]
    assert len(with_landmarks) > 5, (
        f"only {len(with_landmarks)} e2e files mention an app-shell landmark — the selector "
        "set is probably stale, so a clean scan would prove nothing"
    )


def test_every_app_shell_wait_in_the_e2e_suite_uses_the_constant() -> None:
    findings: list[str] = []
    for path in sorted(E2E_DIR.glob("*.py")):
        findings.extend(scan_source(path.read_text(encoding="utf-8"), rel=f"tests/e2e/{path.name}"))
    assert findings == [], (
        "app-shell wait budgets must be tests/e2e/timeouts.py::"
        + CONSTANT
        + ", not raw values:\n  "
        + "\n  ".join(findings)
    )
