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

Whether a test runs DDL is decided by *where its SQL comes from*, and the sources are not
interchangeable: a literal in the test, a module-level constant in the test, a constant read
off an ``alembic/versions`` revision (``conn.execute(text(module.UPGRADE_SQL))`` — the shape
this scanner was blind to, which left v381's double replay of ``ALTER TABLE "user"``
unmarked), and a call to a revision's own ``upgrade()``/``downgrade()``.
``test_ddl_marker_discipline_selftest.py`` holds a must-fire and a must-stay-clean case for
each: a detector that silently matches nothing is indistinguishable from a clean suite.
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]

#: The revision files a migration-consistency suite replays. Their ``*_SQL`` constants are
#: the DDL those suites execute, so the scanner has to read them to see it at all.
_VERSIONS_DIR = _TESTS_ROOT.parent / "alembic" / "versions"

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

#: Calling a revision's own entry point runs every statement in it. ``module.downgrade()``
#: (``test_v386_migration_consistency.py``) and ``command.upgrade(config, "head")`` execute
#: real DDL with no SQL string anywhere in the test file, so the string-based rules below
#: cannot see them.
_MIGRATION_ENTRYPOINTS = frozenset({"upgrade", "downgrade"})

#: File-level pre-filter companion to ``_DDL``: a module whose DDL arrives entirely through
#: a revision entry point contains no DDL keyword of its own.
_MIGRATION_CALL = re.compile(r"\.(upgrade|downgrade)\s*\(")

#: ``REVISION = "v381_approval_state"`` — the revision a consistency suite replays, and so
#: the file its ``*_SQL`` attributes resolve against.
_REVISION_ID = re.compile(r"^v\d+_[a-z0-9_]+$")

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


def _resolve_string(node: ast.AST, known: dict[str, str]) -> str | None:
    """Best-effort static value of a string expression in a revision module.

    Covers the three shapes the revisions actually use: a literal, an f-string, and
    ``CONSTRAINT_SQL = _CONSTRAINT_TEMPLATE.format(statuses=…)`` (v381) — a ``.format``
    call whose *template* carries the ``ALTER TABLE … ADD CONSTRAINT``. Missing the last
    one would leave ``module.CONSTRAINT_SQL`` invisible even after the attribute lookup
    below was fixed.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve_string(node.left, known)
        right = _resolve_string(node.right, known)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "format":
            return _resolve_string(node.func.value, known)
        return None
    if isinstance(node, ast.Name):
        return known.get(node.id)
    return None


@functools.cache
def _revision_ddl_constants(revision: str) -> frozenset[str]:
    """``*_SQL`` constants in one ``alembic/versions`` revision whose value is DDL."""
    path = _VERSIONS_DIR / f"{revision}.py"
    if not path.exists():
        return frozenset()
    values: dict[str, str] = {}
    ddl: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if not isinstance(node, ast.Assign):
            continue
        resolved = _resolve_string(node.value, values)
        if resolved is None:
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            values[target.id] = resolved
            if (
                target.id.endswith("_SQL")
                and _DDL.search(resolved)
                and not _TEMP_TABLE.search(resolved)
            ):
                ddl.add(target.id)
    return frozenset(ddl)


@functools.cache
def _any_revision_ddl_constants() -> frozenset[str]:
    """The union across every revision — the fallback for a suite that names none."""
    names: set[str] = set()
    for path in sorted(_VERSIONS_DIR.glob("v*.py")):
        names |= _revision_ddl_constants(path.stem)
    return frozenset(names)


def _ddl_attributes(tree: ast.Module) -> frozenset[str]:
    """``*_SQL`` attribute names that hold DDL, e.g. ``module.UPGRADE_SQL``.

    A consistency suite loads its revision by path and replays a constant from it, so the
    SQL is nowhere in the test file. Resolution prefers the module-level ``REVISION``
    constant, which is exact; a suite that names no revision falls back to the union over
    all revisions, because guessing wrong in *that* direction only costs a marker,
    while missing the DDL costs a wedged suite.
    """
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "REVISION" for t in node.targets):
            continue
        value = _resolve_string(node.value, {})
        if value is not None and _REVISION_ID.match(value):
            return _revision_ddl_constants(value)
    return _any_revision_ddl_constants()


def _executes_ddl(fn: ast.AST, ddl_consts: set[str], ddl_attrs: frozenset[str]) -> bool:
    """True when a DDL string is *passed to* ``.execute(...)``, or a revision is replayed.

    The distinction matters. Three suites assert on the migration file's *source text*
    (``assert 'CREATE TABLE IF NOT EXISTS chat_project' in source``) and one passes
    ``"'; DROP TABLE media_file; --"`` as a SQL-injection payload expecting a 422. None of
    those execute anything, and marking them would reintroduce the barrier this guards
    against. By the same token ``module.RENAME_SQL`` (v379) and
    ``module.RETIRED_AUTH_CONFIG_KEYS_SQL`` (v377) are UPDATE/DELETE, so resolving an
    attribute has to read the constant's *value* rather than assume ``_SQL`` means DDL.
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _MIGRATION_ENTRYPOINTS:
            return True
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
                # A local constant (``_GUARD_SQL``) or one read off a revision module
                # (``module.UPGRADE_SQL``). Both are already known to hold DDL.
                elif (isinstance(inner, ast.Name) and inner.id in ddl_consts) or (
                    isinstance(inner, ast.Attribute) and inner.attr in ddl_attrs
                ):
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
        if not (_DDL.search(source) or _MIGRATION_CALL.search(source)):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a syntax error fails collection anyway
            continue

        rel = path.relative_to(_TESTS_ROOT).as_posix()
        module_markers = _module_marker_names(tree)
        ddl_consts = _ddl_constants(tree)
        ddl_attrs = _ddl_attributes(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue

            ident = f"{rel}::{node.name}"
            marked = "ddl_exclusive" in (module_markers | _decorator_names(node))
            runs_ddl = _executes_ddl(node, ddl_consts, ddl_attrs)

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
