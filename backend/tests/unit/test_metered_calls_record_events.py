"""Every call that settles token budget also records a usage event (issue #790).

Derived from the code (AST): the token limit and the usage panel must never diverge, so the
one place budget is settled for a whole-answer call (``LLMMeteringMixin._account_call``) must
also record that call, and no module may settle budget around the seam.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
SEAM = "services/llm_metering.py"


def _settle_sites() -> list[tuple[str, str]]:
    """``(module, enclosing function)`` of every ``settle(...)`` call under app/."""
    sites: list[tuple[str, str]] = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "settle"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "llm_budget"
                ):
                    sites.append((path.relative_to(APP).as_posix(), fn.name))
    return sorted(set(sites))


def _calls(path: str, function: str) -> set[str]:
    tree = ast.parse((APP / path).read_text())
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == function:
            return {
                n.func.attr
                for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            }
    raise AssertionError(f"{function} not found in {path}")


def test_budget_is_settled_only_at_the_metering_seam():
    modules = {module for module, _ in _settle_sites()}
    assert modules == {SEAM}, f"budget settled outside the seam: {sorted(modules - {SEAM})}"


def test_the_whole_answer_settle_site_also_records_the_call():
    sites = dict.fromkeys(fn for _, fn in _settle_sites())
    assert "_account_call" in sites
    assert "record_unscoped_call" in _calls(SEAM, "_account_call")


def test_the_streamed_settle_site_is_recorded_by_the_chat_hook():
    # chat_completion_stream settles without recording; its event is chat.tokens, written per
    # assistant message by record_chat_usage. That holds only while chat is its sole caller.
    callers = sorted(
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        if any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "chat_completion_stream"
            for n in ast.walk(ast.parse(path.read_text()))
        )
    )
    outside = [c for c in callers if not c.startswith(("services/chat/", "services/llm_"))]
    assert outside == [], f"streamed LLM calls outside chat have no usage event: {outside}"
