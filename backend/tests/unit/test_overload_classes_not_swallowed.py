"""A broad ``except`` on the request path must not swallow a database overload.

Pool timeout, statement timeout and the request deadline are answered 503 with
``Retry-After`` by ``app.core.db_overload``. A broad handler around session use turns them
into something else first: before lane A, a pool timeout during the revocation lookup
answered **401** (the caller signed out for a busy database), and the optional-auth path
answered the caller as **anonymous**.

This scans ``backend/app/api`` and ``backend/app/auth`` for ``except`` clauses broad enough
to catch an overload (bare, ``Exception``, ``BaseException``, ``SQLAlchemyError``,
``OperationalError``, ``DBAPIError``) around code that uses a session, and requires each
to do one of:

* call ``reraise_db_overload(exc)`` in the handler;
* end in a bare ``raise`` (the overload propagates unchanged);
* follow an earlier clause of the same ``try`` that catches an overload class and re-raises;
* end by raising a 5xx ``HTTPException`` / ``ErrorHandler.internal_error()``: the app's
  HTTPException handler finds the overload on the exception chain and answers 503.

Anything else needs an entry in ``overload_swallow_allowlist.txt`` (``path::function  #
reason``). A stale entry fails too, so the list only shrinks.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND = Path(__file__).resolve().parents[2]
SCANNED = ("app/api", "app/auth")
ALLOWLIST = Path(__file__).with_name("overload_swallow_allowlist.txt")

_BROAD = {"Exception", "BaseException", "SQLAlchemyError", "OperationalError", "DBAPIError"}
_OVERLOAD_NAMES = {
    "TimeoutError",
    "PoolTimeoutError",
    "RequestDeadlineExceededError",
    "OVERLOAD_EXCEPTION_CLASSES",
}
_SESSION_NAMES = {"db", "session", "db_session"}
_SESSION_FACTORIES = {"SessionLocal", "session_scope"}
_REASON_SEP = "#"


@dataclass(frozen=True)
class Finding:
    path: str
    function: str
    line: int

    @property
    def key(self) -> str:
        return f"{self.path}::{self.function}"


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _names(type_node: ast.expr | None) -> set[str]:
    if type_node is None:
        return {"BaseException"}
    nodes = type_node.elts if isinstance(type_node, ast.Tuple) else [type_node]
    return {_dotted(n).rsplit(".", 1)[-1] for n in nodes}


def _uses_session(statements: list[ast.stmt]) -> bool:
    for statement in statements:
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            func = _dotted(node.func)
            root = func.split(".", 1)[0]
            if root in _SESSION_NAMES or func.startswith("self.db.") or func in _SESSION_FACTORIES:
                return True
            arguments = [*node.args, *(k.value for k in node.keywords)]
            if any(isinstance(a, ast.Name) and a.id in _SESSION_NAMES for a in arguments):
                return True
    return False


def _is_5xx_raise(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Raise) or not isinstance(statement.exc, ast.Call):
        return False
    call = statement.exc
    func = _dotted(call.func)
    if func.endswith("internal_error"):
        return True
    if func.rsplit(".", 1)[-1] != "HTTPException":
        return False
    status = next((k.value for k in call.keywords if k.arg == "status_code"), None)
    if status is None and call.args:
        status = call.args[0]
    if isinstance(status, ast.Constant) and isinstance(status.value, int):
        return bool(status.value >= 500)
    return _dotted(status).rsplit(".", 1)[-1].startswith("HTTP_5") if status else False


def _handler_complies(handler: ast.ExceptHandler, earlier: list[ast.ExceptHandler]) -> bool:
    for node in ast.walk(handler):
        if isinstance(node, ast.Call) and _dotted(node.func).endswith("reraise_db_overload"):
            return True
    last = handler.body[-1]
    if isinstance(last, ast.Raise) and last.exc is None:
        return True
    if _is_5xx_raise(last):
        return True
    return any(
        _names(e.type) & _OVERLOAD_NAMES
        and isinstance(e.body[-1], ast.Raise)
        and e.body[-1].exc is None
        for e in earlier
    )


def scan_source(source: str, path: str) -> list[Finding]:
    """Findings for one module's source (exposed for the must-fire / must-stay-clean cases)."""
    tree = ast.parse(source)
    owner: dict[int, str] = {}
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                owner[id(node)] = func.name  # innermost wins: inner defs are walked later
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not _uses_session(node.body):
            continue
        for index, handler in enumerate(node.handlers):
            if not _names(handler.type) & _BROAD:
                continue
            if _handler_complies(handler, node.handlers[:index]):
                continue
            findings.append(Finding(path, owner.get(id(node), "<module>"), handler.lineno))
    return findings


def scan_tree() -> list[Finding]:
    findings = []
    for directory in SCANNED:
        for file in sorted((BACKEND / directory).rglob("*.py")):
            rel = file.relative_to(BACKEND).as_posix()
            findings.extend(scan_source(file.read_text(), rel))
    return findings


def _allowlist() -> dict[str, str]:
    entries: dict[str, str] = {}
    for raw in ALLOWLIST.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, reason = line.partition(_REASON_SEP)
        entries[key.strip()] = reason.strip()
    return entries


# ── the scanner itself must fire, and must stay quiet on compliant code ─────────────────

_SWALLOWS = """
def lookup(db):
    try:
        return db.query(User).first()
    except Exception:
        return None
"""

_SWALLOWS_VIA_ARGUMENT = """
def verify(db, token):
    try:
        return is_revoked_from_database(db, token)
    except Exception:
        raise HTTPException(status_code=401)
"""

_COMPLIANT = """
def reraises(db):
    try:
        return db.query(User).first()
    except Exception as exc:
        reraise_db_overload(exc)
        return None

def bare_raise(db):
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

def maps_to_500(db):
    try:
        db.commit()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR) from exc

def internal(db):
    try:
        db.commit()
    except Exception as exc:
        raise ErrorHandler.internal_error() from exc

def earlier_clause(db):
    try:
        db.commit()
    except OVERLOAD_EXCEPTION_CLASSES:
        raise
    except Exception:
        return None

def no_session(client):
    try:
        client.get("/x")
    except Exception:
        return None
"""


@pytest.mark.parametrize("source", [_SWALLOWS, _SWALLOWS_VIA_ARGUMENT], ids=["call", "argument"])
def test_the_scanner_fires_on_a_swallowing_handler(source):
    assert len(scan_source(source, "x.py")) == 1


def test_the_scanner_stays_quiet_on_compliant_handlers():
    assert scan_source(_COMPLIANT, "x.py") == []


# ── the tree ────────────────────────────────────────────────────────────────────────────


def test_no_request_path_handler_swallows_an_overload():
    allowed = _allowlist()
    found = scan_tree()
    unlisted = sorted({f"{f.key}  (line {f.line})" for f in found if f.key not in allowed})

    assert len(found) > 0  # the scan saw the tree; the allowlist below is not empty by accident
    assert unlisted == [], "\n".join(
        [
            "A broad except around session use swallows a DB overload. Call",
            "reraise_db_overload(exc) first, or allowlist it with a reason:",
            *unlisted,
        ]
    )


def test_every_allowlist_entry_is_still_a_finding_and_has_a_reason():
    allowed = _allowlist()
    found = {f.key for f in scan_tree()}

    stale = sorted(key for key in allowed if key not in found)
    reasonless = sorted(key for key, reason in allowed.items() if not reason)

    assert len(allowed) > 0
    assert stale == []
    assert reasonless == []
