"""No fan-out dispatch publishes the same whole collection in every message.

The embedding-consistency repair dispatched one batch message per 25 files, and every one
carried ``list(missing_v3)``: the run's ENTIRE missing-speaker set, built outside the loop.
The #1337 load test measured ~4,000 messages of ~117 MB each and Redis at 52 GB. Celery's
task guide (*Data locality*, *State*: https://docs.celeryq.dev/en/stable/userguide/tasks.html)
is to pass a message the identifiers of its own work, and let the task fetch the rest.

The shape is visible in the syntax: inside a loop, a ``.apply_async``/``.delay``/
``.s``/``.si``/``.signature``/``send_task`` call is given a collection BUILT from names
the loop never rebinds (``list(x)``, ``sorted(x)``, ``set(x)``, a comprehension over
``x``). Such an argument is loop-invariant: every message in the fan-out carries all of it,
so the total payload is (messages x collection size). This scans every module under
``backend/app`` for it. The self-tests prove the detector fires on the original defect and
stays quiet on the bounded shapes that replaced it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TypeGuard

import pytest

pytestmark = pytest.mark.unit

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

DISPATCH_ATTRS = frozenset({"apply_async", "delay", "s", "si", "signature", "send_task"})
COLLECTION_BUILDERS = frozenset({"list", "sorted", "tuple", "set", "frozenset"})
LOOPS = (ast.For, ast.AsyncFor, ast.While)

#: ``"<path relative to backend/app>:<function>"`` -> why its loop-invariant collection is
#: bounded. Empty: every current fan-out passes per-message identifiers.
ALLOWED: dict[str, str] = {}


def _bound_names(loop: ast.AST) -> set[str]:
    """Every name the loop (re)binds: its target, and anything assigned in its body.

    Comprehension targets are not included: they live in the comprehension's own scope.
    """
    names: set[str] = set()
    for node in ast.walk(loop):
        targets: list[ast.AST] = []
        if isinstance(node, (ast.For, ast.AsyncFor)):
            targets.append(node.target)
        elif isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
            targets.append(node.target)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            targets.append(node.optional_vars)
        for target in targets:
            names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
    return names


def _is_collection_build(node: ast.AST) -> bool:
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in COLLECTION_BUILDERS
        and bool(node.args)
    )


def _free_names(node: ast.AST) -> set[str]:
    """Names *node* reads that it does not bind itself (comprehension targets)."""
    used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    own = {
        n.id
        for comp in ast.walk(node)
        if isinstance(comp, ast.comprehension)
        for n in ast.walk(comp.target)
        if isinstance(n, ast.Name)
    }
    return used - own - COLLECTION_BUILDERS


def _argument_values(call: ast.Call) -> list[ast.expr]:
    """The call's arguments, looking inside literal ``args=[...]``/``kwargs={...}``."""
    values: list[ast.expr] = list(call.args)
    for kw in call.keywords:
        if isinstance(kw.value, ast.Dict):
            values.extend(v for v in kw.value.values if v is not None)
        elif isinstance(kw.value, (ast.List, ast.Tuple)):
            values.extend(kw.value.elts)
        else:
            values.append(kw.value)
    # ``list(x) if flag else []``: either branch may be what is sent.
    expanded: list[ast.expr] = []
    while values:
        value = values.pop()
        if isinstance(value, ast.IfExp):
            values.extend((value.body, value.orelse))
        else:
            expanded.append(value)
    return expanded


def _is_dispatch(node: ast.AST) -> TypeGuard[ast.Call]:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in DISPATCH_ATTRS
    )


def find_invariant_payloads(source: str) -> list[tuple[str, int]]:
    """``(enclosing function, line)`` of every loop-invariant collection in a fan-out."""
    tree = ast.parse(source)
    found: list[tuple[str, int]] = []
    functions = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for func in functions:
        for loop in (n for n in ast.walk(func) if isinstance(n, LOOPS)):
            bound = _bound_names(loop)
            for call in (n for n in ast.walk(loop) if _is_dispatch(n)):
                for value in _argument_values(call):
                    if not _is_collection_build(value):
                        continue
                    if not _free_names(value) & bound:
                        found.append((func.name, value.lineno))
    return sorted(set(found))


# --- self-tests: the detector must fire on the defect and stay quiet on the fix ---------

MUST_FIRE = {
    "the original repair dispatch": """
def dispatch(batches, missing_v3, missing_v4, v4_exists):
    for batch_idx, batch in enumerate(batches):
        task.apply_async(
            kwargs={
                "file_uuids": batch,
                "missing_v3_uuids": list(missing_v3),
                "missing_v4_uuids": list(missing_v4) if v4_exists else [],
            },
        )
""",
    "only the conditional v4 list of the original": """
def dispatch(batches, missing_v4, v4_exists):
    for batch in batches:
        task.apply_async(kwargs={"files": batch, "v4": list(missing_v4) if v4_exists else []})
""",
    "a sorted copy in positional args": """
def dispatch(chunks, everything):
    for chunk in chunks:
        task.delay(chunk, sorted(everything))
""",
    "a comprehension over an outer collection": """
def dispatch(chunks, users):
    i = 0
    while i < len(chunks):
        task.s(chunks[i], [u.id for u in users]).apply_async()
        i += 1
""",
    "send_task with args list": """
def dispatch(app, chunks, ids):
    for chunk in chunks:
        app.send_task("t", args=[chunk, tuple(ids)])
""",
}

MUST_STAY_CLEAN = {
    "per-batch lists (the fix)": """
def dispatch(batches):
    for idx, batch in enumerate(batches):
        task.apply_async(kwargs={"file_uuids": batch.file_uuids, "v3": list(batch.v3)})
""",
    "a collection built inside the loop": """
def dispatch(files, missing):
    for f in files:
        mine = [s for s in missing if s.file == f]
        task.delay(f, list(mine))
""",
    "a slice of the outer list": """
def dispatch(ids):
    for i in range(0, len(ids), 25):
        task.delay(ids[i : i + 25])
""",
    "one message outside any loop": """
def dispatch(ids):
    task.delay(list(ids))
""",
}


@pytest.mark.parametrize("name", sorted(MUST_FIRE))
def test_the_detector_fires_on_a_loop_invariant_collection(name):
    assert find_invariant_payloads(MUST_FIRE[name]), f"missed: {name}"


@pytest.mark.parametrize("name", sorted(MUST_STAY_CLEAN))
def test_the_detector_stays_quiet_on_bounded_payloads(name):
    assert find_invariant_payloads(MUST_STAY_CLEAN[name]) == [], f"false positive: {name}"


def test_no_fanout_dispatch_in_the_app_carries_a_loop_invariant_collection():
    modules = sorted(APP_ROOT.rglob("*.py"))
    assert len(modules) > 100, f"scanned only {len(modules)} modules under {APP_ROOT}"
    offenders: list[str] = []
    for path in modules:
        rel = path.relative_to(APP_ROOT).as_posix()
        for func, line in find_invariant_payloads(path.read_text(encoding="utf-8")):
            if f"{rel}:{func}" not in ALLOWED:
                offenders.append(f"{rel}:{line} in {func}()")
    assert offenders == [], (
        "a dispatch loop publishes the same whole collection in every message; pass each "
        "message only its own ids (or a pointer it re-reads):\n  " + "\n  ".join(offenders)
    )
