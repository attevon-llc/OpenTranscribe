"""Every ``except ... HTTPException`` under ``app/`` must also catch ``DomainError``.

A lower layer raises ``DomainError`` where it used to raise ``HTTPException``. A clause that
names only ``HTTPException`` (typically ``except HTTPException: raise`` above an
``except Exception`` that renders a 500) would let a refusal fall through to the wrong branch.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"


def _names(node: ast.expr | None) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, ast.Tuple):
        return set().union(*(_names(e) for e in node.elts)) if node.elts else set()
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    return set()


def unwidened_clauses(source: str) -> list[int]:
    """Line numbers of handlers that name HTTPException but not DomainError."""
    out: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ExceptHandler):
            names = _names(node.type)
            if "HTTPException" in names and "DomainError" not in names:
                out.append(node.lineno)
    return sorted(out)


@pytest.mark.unit
def test_scanner_flags_a_bare_httpexception_clause() -> None:
    src = "try:\n    pass\nexcept HTTPException:\n    raise\n"
    assert unwidened_clauses(src) == [3]


@pytest.mark.unit
def test_scanner_flags_a_tuple_without_domain_error() -> None:
    src = "try:\n    pass\nexcept (fastapi.HTTPException, KeyError) as e:\n    raise\n"
    assert unwidened_clauses(src) == [3]


@pytest.mark.unit
def test_scanner_accepts_widened_and_unrelated_clauses() -> None:
    src = (
        "try:\n    pass\nexcept (HTTPException, DomainError):\n    raise\n"
        "try:\n    pass\nexcept Exception:\n    pass\n"
        "try:\n    pass\nexcept DomainError:\n    pass\n"
        "try:\n    pass\nexcept:\n    pass\n"
    )
    assert unwidened_clauses(src) == []


@pytest.mark.unit
def test_every_httpexception_clause_in_app_also_catches_domain_error() -> None:
    files = sorted(APP.rglob("*.py"))
    assert len(files) > 100, "the scan found no app tree"
    offenders = {
        f"{path.relative_to(APP.parent)}:{line}"
        for path in files
        for line in unwidened_clauses(path.read_text(encoding="utf-8"))
    }
    assert not offenders, sorted(offenders)
