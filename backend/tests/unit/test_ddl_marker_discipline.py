"""Every test that executes schema DDL must be isolated from the other xdist workers.

Postgres takes an ``ACCESS EXCLUSIVE`` lock for ``ALTER TABLE`` / ``DROP TABLE``, and that
lock is not confined to the table named in the statement — dropping a foreign key also
locks the *referenced* table. Under ``-n auto`` nearly every other worker is inserting
``user`` rows at the same moment, so unisolated DDL deadlocks against unrelated tests
(issue #389).

``tests/conftest.py``'s ``db_session`` gives that isolation: ordinary tests take a DB-wide
advisory lock in SHARED mode, and a ``@pytest.mark.ddl_exclusive`` test takes it EXCLUSIVE.
The mechanism only works if the marker is actually applied, and only for connections that
come from ``db_session`` — which is exactly what this module enforces.

It also enforces the *converse*, because the marker is expensive: an EXCLUSIVE acquisition
drains every other worker, so a suite that applies ``ddl_exclusive`` at module scope to
tests that merely *read* the schema turns each of them into a full-suite barrier. That is
what made ``migration_ddl`` 414 s of a 511 s wall clock.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]

#: Statements that take ACCESS EXCLUSIVE. ``CREATE INDEX`` (without CONCURRENTLY) and
#: ``TRUNCATE`` are included because they block DML on the target just as hard.
_DDL = re.compile(
    r"\b(ALTER\s+TABLE|DROP\s+TABLE|CREATE\s+TABLE|TRUNCATE|CREATE\s+INDEX|DROP\s+INDEX)\b",
    re.IGNORECASE,
)

#: ``CREATE TEMP TABLE`` puts the table in a session-private ``pg_temp_*`` schema, so DDL on
#: it is invisible to every other connection and needs no isolation at all. Flagging it would
#: be a false finding — and "fixing" a false finding by taking a suite-wide EXCLUSIVE lock
#: would slow the suite to protect nothing.
_TEMP_TABLE = re.compile(r"\bCREATE\s+(GLOBAL\s+|LOCAL\s+)?TEMP(ORARY)?\s+TABLE\b", re.IGNORECASE)

#: A test that opens its own connection can still be protected — by taking the same lock
#: explicitly. ``tests/db_locks.py`` exposes the helpers; naming one is the opt-in.
_EXPLICIT_LOCK_HELPERS = frozenset({"acquire_ddl_lock_exclusive", "acquire_ddl_lock_exclusive_raw"})

#: Tests that execute DDL with no isolation at all, and the reason it is accepted. Empty is
#: the correct state: an entry here says "I accept a deadlock that can wedge the suite".
#: It is deliberately NOT a place to park work — prefer routing through ``db_session`` or
#: calling a ``tests/db_locks.py`` helper.
_UNPROTECTED_ALLOWLIST: dict[str, str] = {}


def _iter_test_modules() -> list[Path]:
    """Every module pytest would collect, excluding the separate e2e rootdir."""
    return sorted(p for p in _TESTS_ROOT.rglob("*.py") if "e2e" not in p.parts)


def _module_marker_names(tree: ast.Module) -> set[str]:
    """Marker names applied to the whole module via ``pytestmark``."""
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            continue
        for marker in ast.walk(node.value):
            if isinstance(marker, ast.Attribute):
                names.add(marker.attr)
    return names


def _decorator_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for dec in fn.decorator_list:
        for node in ast.walk(dec):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
    return names


def _ddl_constants(tree: ast.Module) -> set[str]:
    """Module-level names bound to a string containing DDL (e.g. ``_GUARD_SQL``)."""
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str) or not _DDL.search(node.value.value):
            continue
        found.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return found


def _executes_ddl(fn: ast.AST, ddl_consts: set[str]) -> bool:
    """True when a DDL string is *passed to* ``.execute(...)``.

    The distinction matters. Three suites assert on the migration file's *source text*
    (``assert 'CREATE TABLE IF NOT EXISTS chat_project' in source``) and one passes
    ``"'; DROP TABLE media_file; --"`` as a SQL-injection payload expecting a 422. None of
    those execute anything, and marking them would reintroduce the barrier this guards
    against.
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "execute"):
            continue
        for arg in node.args:
            for inner in ast.walk(arg):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    if _DDL.search(inner.value) and not _TEMP_TABLE.search(inner.value):
                        return True
                elif isinstance(inner, ast.JoinedStr):
                    joined = "".join(
                        v.value
                        for v in inner.values
                        if isinstance(v, ast.Constant) and isinstance(v.value, str)
                    )
                    if _DDL.search(joined) and not _TEMP_TABLE.search(joined):
                        return True
                elif isinstance(inner, ast.Name) and inner.id in ddl_consts:
                    return True
    return False


def _takes_lock_explicitly(fn: ast.AST) -> bool:
    """True when the test calls a ``tests/db_locks.py`` helper itself."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if name in _EXPLICIT_LOCK_HELPERS:
                return True
    return False


def _uses_db_session(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the test takes the ``db_session`` fixture (directly or via ``client``)."""
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    return bool(params & {"db_session", "client"})


def _collect() -> tuple[list[str], list[str], list[str]]:
    """Return (unmarked_ddl, unprotected_ddl, over_marked) test identifiers."""
    unmarked: list[str] = []
    unprotected: list[str] = []
    over_marked: list[str] = []

    for path in _iter_test_modules():
        source = path.read_text()
        if not _DDL.search(source):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a syntax error fails collection anyway
            continue

        rel = path.relative_to(_TESTS_ROOT).as_posix()
        module_markers = _module_marker_names(tree)
        ddl_consts = _ddl_constants(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue

            ident = f"{rel}::{node.name}"
            marked = "ddl_exclusive" in (module_markers | _decorator_names(node))
            runs_ddl = _executes_ddl(node, ddl_consts)

            if not runs_ddl:
                if marked:
                    over_marked.append(ident)
            elif _takes_lock_explicitly(node):
                pass  # opted in by hand — the strongest form of protection available
            elif not _uses_db_session(node):
                if ident not in _UNPROTECTED_ALLOWLIST:
                    unprotected.append(ident)
            elif not marked:
                unmarked.append(ident)

    return unmarked, unprotected, over_marked


_UNMARKED, _UNPROTECTED, _OVER_MARKED = _collect()


def test_every_ddl_test_carries_the_marker() -> None:
    """DDL through ``db_session`` without ``ddl_exclusive`` can deadlock any worker."""
    assert not _UNMARKED, (
        "These tests execute schema DDL through db_session but do not carry "
        "@pytest.mark.ddl_exclusive, so nothing stops them running beside another "
        "worker's INSERT on the same table (issue #389):\n  " + "\n  ".join(_UNMARKED)
    )


def test_no_ddl_runs_outside_the_advisory_lock() -> None:
    """DDL on a self-opened connection cannot be protected by the marker at all."""
    assert not _UNPROTECTED, (
        "These tests execute schema DDL on a connection that does not come from "
        "db_session, so the advisory lock never covers them. Either route them through "
        "db_session or take the lock explicitly and add an allowlist entry:\n  "
        + "\n  ".join(_UNPROTECTED)
    )


def test_the_marker_is_not_applied_to_tests_that_only_read() -> None:
    """Each EXCLUSIVE acquisition drains every other worker — do not spend it on a read.

    This is the regression guard for the 414 s ``migration_ddl`` critical path: applying
    ``ddl_exclusive`` at module scope turns every read-only schema assertion in the module
    into a full-suite barrier.
    """
    assert not _OVER_MARKED, (
        "These tests carry ddl_exclusive but never execute DDL. Each one is a "
        "stop-the-world barrier for no reason — move the marker onto the specific tests "
        "that run ALTER/DROP/CREATE:\n  " + "\n  ".join(_OVER_MARKED)
    )


def test_the_unprotected_allowlist_is_honest() -> None:
    """A stale allowlist grants an exemption to nothing while reading as deliberate.

    Written as one test rather than a ``parametrize`` over the allowlist so that the empty
    case — the correct one — passes instead of reporting a permanent skip.
    """
    stale: list[str] = []
    unexplained: list[str] = []
    for ident, reason in sorted(_UNPROTECTED_ALLOWLIST.items()):
        rel, _, name = ident.partition("::")
        path = _TESTS_ROOT / rel
        if not path.exists() or f"def {name}(" not in path.read_text():
            stale.append(ident)
        if not reason.strip():
            unexplained.append(ident)

    assert not stale, f"allowlist entries point at tests that no longer exist: {stale}"
    assert not unexplained, f"allowlist entries need a written reason: {unexplained}"
