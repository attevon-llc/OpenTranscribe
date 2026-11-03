"""No two audit emitters may write the same field with clashing types.

Every month's audit events share ONE OpenSearch index, and ``details`` is a dynamic object
mapping: the first event to write ``details.scope`` fixes its type for the month, and every later
event that writes an incompatible value is REJECTED (``mapper_parsing_exception``) and falls to the
fallback file, outside the searchable trail. That already happened once: privacy purges wrote
``details.scope`` as an object while content holds wrote it as a string, and the purge events
vanished from the index (``services/privacy/purge.py`` renamed theirs to ``purge_scope``).

This derives, by AST over ``app/``, the kind of every value each emitter passes, and fails on:

* a ``details`` key written as an object somewhere and as a scalar elsewhere, or as text in one
  emitter and a number in another;
* an integer-mapped top-level field (``user_id``, ``target_user_id``, ``organization_id``) given a
  value that is visibly a string (a ``str()`` call, an f-string, a ``.uuid`` / ``.email``).

Values whose kind a static read cannot tell (a variable, a call) are not judged.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
EMITTERS = {"log", "record_content_event", "record_file_event", "audit_takedown", "audit_purge"}
INT_FIELDS = {"user_id", "target_user_id", "organization_id"}
STRINGY_ATTRS = {"uuid", "email", "username", "name"}

#: Clashes that cannot occur, with the reason. Keyed ``details.<key>``.
ALLOWED: dict[str, str] = {}


def _kind(node: ast.AST) -> str | None:
    if isinstance(node, ast.Dict | ast.DictComp):
        return "object"
    if isinstance(node, ast.List | ast.ListComp | ast.Tuple | ast.Set):
        return None  # arrays take their elements' type; not judged
    if isinstance(node, ast.JoinedStr):
        return "text"
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return "bool"
        if isinstance(node.value, int | float):
            return "number"
        if isinstance(node.value, str):
            return "text"
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return {"str": "text", "int": "number", "float": "number", "len": "number"}.get(
            node.func.id
        )
    return None


def _emitter_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in EMITTERS:
                yield node


def _local_dict_entries(func: ast.AST) -> dict[str, list[tuple[str, ast.AST]]]:
    """``name -> [(key, value)]`` for every dict a function builds in a local variable.

    Covers ``details = {...}`` and ``details["k"] = v``; most emitters build their ``details``
    that way before the call, so reading only call-site literals misses most of them.
    """
    entries: dict[str, list[tuple[str, ast.AST]]] = defaultdict(list)
    for node in ast.walk(func):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Dict):
                    for key, value in zip(node.value.keys, node.value.values, strict=True):
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            entries[target.id].append((key.value, value))
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                    and node.value is not None
                ):
                    entries[target.value.id].append((target.slice.value, node.value))
    return entries


def _details_entries(arg: ast.AST, local: dict[str, list[tuple[str, ast.AST]]]):
    if isinstance(arg, ast.Dict):
        for key, value in zip(arg.keys, arg.values, strict=True):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                yield key.value, value
            elif key is None and isinstance(value, ast.Name):  # {**extra, ...}
                yield from local.get(value.id, [])
    elif isinstance(arg, ast.Name):
        yield from local.get(arg.id, [])


def scan(paths: list[Path]) -> tuple[dict[str, dict[str, set[str]]], list[str]]:
    """``details`` key -> kind -> sites, and the integer-field findings."""
    kinds: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    int_findings: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for scope in [tree, *scopes]:
            local = _local_dict_entries(scope) if scope is not tree else {}
            calls = (
                _emitter_calls(scope)
                if scope is not tree
                else (c for c in _emitter_calls(tree) if not any(c in ast.walk(s) for s in scopes))
            )
            for call in calls:
                _record(call, local, path, kinds, int_findings)
    return kinds, int_findings


def _record(call, local, path, kinds, int_findings) -> None:
    site = f"{path.relative_to(APP.parent)}:{call.lineno}"
    for kw in call.keywords:
        if kw.arg in ("details", "extra"):
            for key, value in _details_entries(kw.value, local):
                kind = _kind(value)
                if kind is not None:
                    kinds[key][kind].add(site)
        if kw.arg in INT_FIELDS:
            stringy = _kind(kw.value) == "text" or (
                isinstance(kw.value, ast.Attribute) and kw.value.attr in STRINGY_ATTRS
            )
            if stringy:
                int_findings.append(f"{site}: {kw.arg}={ast.unparse(kw.value)}")


def clashes(kinds: dict[str, dict[str, set[str]]]) -> list[str]:
    found = []
    for key, by_kind in sorted(kinds.items()):
        present = set(by_kind)
        bad = ("object" in present and len(present) > 1) or {"text", "number"} <= present
        if bad and f"details.{key}" not in ALLOWED:
            found.append(f"details.{key}: {dict((k, sorted(v)) for k, v in by_kind.items())}")
    return found


def test_no_audit_field_is_written_with_clashing_types():
    kinds, int_findings = scan(sorted(APP.rglob("*.py")))
    assert len(kinds) > 30, f"the scan found only {len(kinds)} details keys; it is not reading"
    assert not clashes(kinds), clashes(kinds)
    assert not int_findings, int_findings


def test_the_scan_catches_the_scope_clash_that_shipped(tmp_path):
    """Guard the guard: the purge/hold ``details.scope`` clash, reproduced, must fire."""
    (tmp_path / "a.py").write_text(
        "audit_logger.log(event_type=E, details={'scope': 'file', 'n': 1})\n"
        "audit_purge(details={'scope': {'kind': 'user'}})\n"
        "audit_logger.log(event_type=E, user_id=str(u.id), details={'n': 'x'})\n"
    )
    global APP
    real, APP = APP, tmp_path.parent
    try:
        kinds, int_findings = scan([tmp_path / "a.py"])
    finally:
        APP = real
    assert [c.split(":")[0] for c in clashes(kinds)] == ["details.n", "details.scope"]
    assert len(int_findings) == 1 and "user_id=str(u.id)" in int_findings[0]
