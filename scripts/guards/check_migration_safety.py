#!/usr/bin/env python3
"""Ratchet guard: lock-heavy operations in new Alembic revisions.

Scans ``backend/alembic/versions/*.py`` (stdlib ``ast``). Only code reachable from
``upgrade()`` / ``upgrade_<engine>()`` (sync or async, any signature, plus same-module helpers
they call) is checked; ``downgrade*`` may drop things. SQL is read from the string literals
passed to ``op.execute`` / ``conn.execute`` / ``exec_driver_sql`` / ``sa.text`` / ``text``,
following module-level and local name assignments (``op.execute(ADD_COLUMN_SQL)``), and split
into statements on ``;``. Findings are counted per ``<revision path>::<rule>``:

* ``R1`` ``CREATE [UNIQUE] INDEX`` without ``CONCURRENTLY``, or ``create_index`` without
  ``postgresql_concurrently=True``. Exempt: a table created in the same revision.
* ``R2`` ``DROP TABLE`` / ``DROP COLUMN`` / ``drop_table`` / ``drop_column`` (expand/contract).
* ``R3`` ``ADD COLUMN ... NOT NULL`` without ``DEFAULT``, or ``add_column(nullable=False)``
  without a ``server_default`` (an explicit ``server_default=None`` is no default).
* ``R4`` ``ALTER COLUMN ... SET NOT NULL`` / ``... TYPE``, or ``alter_column(nullable=False)``
  / ``alter_column(type_=...)``.
* ``R5`` ``ADD [CONSTRAINT x] FOREIGN KEY | CHECK`` without ``NOT VALID``, or
  ``create_foreign_key`` / ``create_check_constraint``.
* ``R6`` any R1-R5 finding or a ``LOCK TABLE`` in a revision that never sets ``lock_timeout``
  (counted once per revision).
* ``DYNAMIC-SQL`` SQL built with an f-string, ``+``, ``%`` or ``.format``: the literal parts are
  still checked, but a human has to read what the variables add.

R3-R5 on a table created in the same revision (``op.create_table`` or ``CREATE TABLE``) are
exempt: nothing else can be touching it yet. Applied history is frozen in the baseline
(``scripts/guards/baselines/migration_safety.txt``), so only new revisions can fail. A missing or
empty versions directory, or a revision that does not parse, exits 2: a guard that scanned
nothing must not look green.

HEURISTIC, not proof. Invisible to it: SQL returned by a function or loaded from a file, helpers
imported from another module, long data backfills, locks taken by Python loops, real table
sizes. A clean run means "no known footgun spelled literally".

Usage::

    check_migration_safety.py [--root DIR] [--baseline FILE]
    check_migration_safety.py --tighten
    check_migration_safety.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

_HERE = Path(__file__).resolve().parent
_VERSIONS = Path('backend') / 'alembic' / 'versions'
_DEFAULT_BASELINE = _HERE / 'baselines' / 'migration_safety.txt'


def _load_baseline_lib() -> ModuleType:
    path = _HERE / '_baseline.py'
    spec = importlib.util.spec_from_file_location('_guard_baseline_migration_safety', path)
    if spec is None or spec.loader is None:
        msg = f'cannot load {path}'
        raise RuntimeError(msg)
    module = sys.modules.get(spec.name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


bl = _load_baseline_lib()

FuncDef = ast.FunctionDef | ast.AsyncFunctionDef

_SQL_CALLS = frozenset({'execute', 'exec_driver_sql', 'text'})
_PASS_THROUGH = frozenset(
    {'strip', 'lstrip', 'rstrip', 'lower', 'upper', 'replace', 'bindparams', 'execution_options'}
)
_I = re.IGNORECASE
_NAME = r'"?[\w.]+"?'
_RX_CREATE_TABLE = re.compile(
    rf'\bCREATE\s+(?:\w+\s+)*TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_NAME})', _I
)
_RX_ALTER_TABLE = re.compile(rf'\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?({_NAME})', _I)
_RX_INDEX = re.compile(r'\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!CONCURRENTLY\b)', _I)
_RX_INDEX_ON = re.compile(rf'\bON\s+(?:ONLY\s+)?({_NAME})', _I)
_RX_DROP = re.compile(r'\bDROP\s+(?:TABLE|COLUMN)\b', _I)
_RX_ADD_COLUMN = re.compile(r'\bADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+\s+([^;]*)', _I)
_RX_NEXT_CLAUSE = re.compile(r',\s*(?:ADD|ALTER|DROP)\b', _I)
_RX_SET_NOT_NULL = re.compile(r'\bSET\s+NOT\s+NULL\b', _I)
_RX_ALTER_TYPE = re.compile(r'\bALTER\s+(?:COLUMN\s+)?"?\w+"?\s+(?:SET\s+DATA\s+)?TYPE\b', _I)
_RX_ADD_CONSTRAINT = re.compile(
    r'\bADD\s+(?:CONSTRAINT\s+"?\w+"?\s+)?(?:FOREIGN\s+KEY|CHECK)\b', _I
)
_RX_NOT_VALID = re.compile(r'\bNOT\s+VALID\b', _I)
_RX_LOCK_TABLE = re.compile(r'\bLOCK\s+TABLE\b', _I)
_RX_LOCK_TIMEOUT = re.compile(r'lock_timeout', _I)
_RX_COMMENTS = re.compile(r'--[^\n]*|/\*.*?\*/', re.DOTALL)


def _table(raw: str) -> str:
    name = raw.replace('"', '').lower()
    return name.removeprefix('public.')


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_const(node: ast.expr | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _str_arg(call: ast.Call, idx: int, kw: str) -> str | None:
    node = call.args[idx] if len(call.args) > idx else _kw(call, kw)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _table(node.value)
    return None


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ''


def _column_calls(node: ast.expr) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and _call_name(n) == 'Column']


def _reachable(tree: ast.Module) -> list[ast.AST]:
    """Nodes to scan: upgrade functions plus same-module helpers they call (transitively)."""
    funcs = {n.name: n for n in tree.body if isinstance(n, FuncDef)}
    roots = [f for name, f in funcs.items() if name == 'upgrade' or name.startswith('upgrade_')]
    if not roots:  # no upgrade(): scan module-level code, minus downgrade*
        return [
            n for n in tree.body if not (isinstance(n, FuncDef) and n.name.startswith('downgrade'))
        ]
    seen: dict[str, FuncDef] = {f.name: f for f in roots}
    queue = list(roots)
    while queue:
        for node in ast.walk(queue.pop()):
            if (
                isinstance(node, ast.Name)
                and node.id in funcs
                and node.id not in seen
                and not node.id.startswith('downgrade')
            ):
                seen[node.id] = funcs[node.id]
                queue.append(funcs[node.id])
    return list(seen.values())


def _assignments(tree: ast.Module, scanned: list[ast.AST]) -> dict[str, ast.expr]:
    """``NAME = expr`` at module level and inside the scanned code (last assignment wins)."""
    env: dict[str, ast.expr] = {}
    for root in (tree, *scanned):
        for node in ast.walk(root):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    env[target.id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.value is not None:
                    env[node.target.id] = node.value
    return env


class _Sql:
    """Evaluates the SQL text an expression spells, recording which literals produced it."""

    def __init__(self, env: dict[str, ast.expr]) -> None:
        self.env = env
        self.leaves: set[int] = set()
        self.dynamic: set[int] = set()

    def text(self, node: ast.expr, depth: int = 0) -> str:
        if depth > 8:
            return ''
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                self.leaves.add(id(node))
                return node.value
            return ''
        if isinstance(node, ast.JoinedStr):
            self.dynamic.add(id(node))
            parts = []
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    parts.append(self.text(part.value, depth + 1) or ' _ ')
                else:
                    parts.append(self.text(part, depth + 1))
            return ''.join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
            self.dynamic.add(id(node))
            return self.text(node.left, depth + 1) + self.text(node.right, depth + 1)
        if isinstance(node, ast.Name):
            target = self.env.get(node.id)
            return self.text(target, depth + 1) if target is not None else ''
        if isinstance(node, ast.Call):
            return self._call_text(node, depth)
        return ''

    def _call_text(self, node: ast.Call, depth: int) -> str:
        name = _call_name(node)
        if name == 'format' and isinstance(node.func, ast.Attribute):
            self.dynamic.add(id(node))
            return self.text(node.func.value, depth + 1)
        if name in {'text', 'dedent'} and node.args:
            return self.text(node.args[0], depth + 1)
        if name in _PASS_THROUGH and isinstance(node.func, ast.Attribute):
            return self.text(node.func.value, depth + 1)
        return ''


def _statements(sql: str) -> list[str]:
    cleaned = _RX_COMMENTS.sub(' ', sql)
    return [' '.join(s.split()) for s in cleaned.split(';') if s.strip()]


def _add_column_not_null(stmt: str) -> int:
    hits = 0
    for match in _RX_ADD_COLUMN.finditer(stmt):
        clause = _RX_NEXT_CLAUSE.split(match.group(1), maxsplit=1)[0]
        if re.search(r'\bNOT\s+NULL\b', clause, _I) and not re.search(r'\bDEFAULT\b', clause, _I):
            hits += 1
    return hits


def _sql_rules(stmt: str, new_tables: set[str]) -> tuple[list[str], bool]:
    """``(rules, takes_lock_table)`` for one SQL statement."""
    rules: list[str] = []
    altered = _RX_ALTER_TABLE.search(stmt)
    on_new = altered is not None and _table(altered.group(1)) in new_tables
    if _RX_INDEX.search(stmt):
        target = _RX_INDEX_ON.search(stmt)
        if not (target and _table(target.group(1)) in new_tables):
            rules.append('R1')
    rules.extend('R2' for _ in _RX_DROP.finditer(stmt))
    if not on_new:
        rules.extend('R3' for _ in range(_add_column_not_null(stmt)))
        rules.extend('R4' for _ in _RX_SET_NOT_NULL.finditer(stmt))
        rules.extend('R4' for _ in _RX_ALTER_TYPE.finditer(stmt))
        if _RX_ADD_CONSTRAINT.search(stmt) and not _RX_NOT_VALID.search(stmt):
            rules.append('R5')
    return rules, bool(_RX_LOCK_TABLE.search(stmt))


def _op_rules(calls: list[ast.Call], new_tables: set[str]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for c in calls:
        attr = _call_name(c)
        if attr in {'drop_table', 'drop_column'}:
            out.append(('R2', c.lineno))
        elif attr == 'create_index':
            table = _str_arg(c, 1, 'table_name')
            if table not in new_tables and not _is_const(_kw(c, 'postgresql_concurrently'), True):
                out.append(('R1', c.lineno))
        elif attr == 'add_column':
            if _str_arg(c, 0, 'table_name') in new_tables:
                continue
            for arg in (*c.args, *(k.value for k in c.keywords)):
                for col in _column_calls(arg):
                    default = _kw(col, 'server_default')
                    if _is_const(_kw(col, 'nullable'), False) and (
                        default is None or _is_const(default, None)
                    ):
                        out.append(('R3', c.lineno))
        elif attr == 'alter_column':
            if _str_arg(c, 0, 'table_name') in new_tables:
                continue
            if _kw(c, 'type_') is not None or _is_const(_kw(c, 'nullable'), False):
                out.append(('R4', c.lineno))
        elif attr in {'create_foreign_key', 'create_check_constraint'}:
            source = _str_arg(
                c, 1, 'source_table' if attr == 'create_foreign_key' else 'table_name'
            )
            if source not in new_tables:
                out.append(('R5', c.lineno))
    return out


def findings_for(tree: ast.Module) -> list[tuple[str, int]]:
    """``(rule, lineno)`` for every risky operation reachable from ``upgrade()``."""
    scanned = _reachable(tree)
    sql = _Sql(_assignments(tree, scanned))
    calls = [n for root in scanned for n in ast.walk(root) if isinstance(n, ast.Call)]
    attr_calls = [c for c in calls if isinstance(c.func, ast.Attribute)]
    new_tables = {
        t
        for c in attr_calls
        if _call_name(c) == 'create_table' and (t := _str_arg(c, 0, 'table_name'))
    }

    statements: list[tuple[str, int]] = []
    out: list[tuple[str, int]] = []
    lock_timeout = any(
        isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and _RX_LOCK_TIMEOUT.search(n.value)
        for root in scanned
        for n in ast.walk(root)
    )
    for c in calls:
        if _call_name(c) not in _SQL_CALLS:
            continue
        first = c.args[0] if c.args else next((k.value for k in c.keywords), None)
        if first is None:
            continue
        before_leaves, before_dynamic = set(sql.leaves), set(sql.dynamic)
        body = sql.text(first)
        fresh_leaves = sql.leaves - before_leaves
        fresh_dynamic = sql.dynamic - before_dynamic
        if fresh_dynamic:
            out.append(('DYNAMIC-SQL', c.lineno))
        if not fresh_leaves:
            continue
        lock_timeout = lock_timeout or bool(_RX_LOCK_TIMEOUT.search(body))
        statements.extend((s, c.lineno) for s in _statements(body))
    for stmt, _line in statements:
        new_tables.update(_table(m.group(1)) for m in _RX_CREATE_TABLE.finditer(stmt))

    out.extend(_op_rules(calls, new_tables))
    takes_lock = False
    for stmt, line in statements:
        rules, lock = _sql_rules(stmt, new_tables)
        takes_lock = takes_lock or lock
        out.extend((rule, line) for rule in rules)
    risky = takes_lock or any(rule.startswith('R') for rule, _ in out)
    if risky and not lock_timeout:
        out.append(('R6', 0))
    return out


def scan(root: Path) -> dict[str, int]:
    """Map ``<repo-relative revision path>::<rule>`` -> hits for every revision with a finding."""
    versions = root / _VERSIONS
    if not versions.is_dir():
        msg = f'{versions} is not a directory; pass --root explicitly'
        raise bl.ScanError(msg)
    paths = sorted(versions.glob('*.py'))
    if not paths:
        msg = f'no revisions in {versions} (wrong --root?)'
        raise bl.ScanError(msg)
    findings: dict[str, int] = {}
    for path in paths:
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            msg = f'cannot parse {rel}: {exc}'
            raise bl.ScanError(msg) from exc
        for rule, _line in findings_for(tree):
            key = f'{rel}::{rule}'
            findings[key] = findings.get(key, 0) + 1
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0] if __doc__ else None)
    parser.add_argument(
        '--root', type=Path, default=None, help='repository root (default: found from this file)'
    )
    parser.add_argument('--baseline', type=Path, default=None, help='baseline file')
    parser.add_argument('--tighten', action='store_true', help='lower values / drop stale entries')
    bl.add_baseline_args(parser)
    args = parser.parse_args(argv)
    try:
        root = args.root or bl.find_root(_HERE, ('backend/alembic',))
        baseline_path = args.baseline or _DEFAULT_BASELINE
        findings = scan(root)
        if args.write_baseline:
            written: int = bl.run_write_baseline(args, baseline_path, findings)
            return written
        baseline = bl.load(baseline_path)
    except (bl.ScanError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    if args.tighten:
        changed = bl.tighten(baseline_path, findings, baseline) if baseline_path.exists() else 0
        print(f'tightened {changed} baseline lines in {baseline_path}')
        baseline = bl.load(baseline_path)
    errors = bl.compare(findings, baseline, unit='risky ops')
    if errors:
        print(
            'migration safety: use CONCURRENTLY, NOT VALID, a server_default, expand/contract and a\n'
            "short SET LOCAL lock_timeout (see v449), or baseline the line with a reason that says why it's safe:",
            file=sys.stderr,
        )
        for line in errors:
            print(f'  {line}', file=sys.stderr)
        return 1
    print(
        f'migration safety: {sum(findings.values())} findings in {len(findings)} keys, within the baseline'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
