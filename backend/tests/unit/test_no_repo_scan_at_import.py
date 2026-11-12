"""No test module may scan the repo (rglob / os.walk / ast.parse / read_text) at import time.

Every pytest process (one per xdist worker, the determinism collectors, the subprocess
meta-tests) imports every test module to collect it, so a module-level
``_X = _scan_something()`` is paid by every process whether or not that module is selected.
Put the scan behind ``functools.cache`` and call it from inside the test
(https://docs.python.org/3/library/functools.html#functools.cache).

A scan that feeds ``pytest.mark.parametrize`` cannot be deferred (the ids come from it); none
does today (a one-directory ``glob`` feeding ids is cheap and is not flagged).

Limits: static, one file. A module-local function is "scanning" if it (transitively, through
bare-name calls in the same module) calls ``rglob``/``os.walk`` (a recursive traversal; a one-directory ``glob`` is cheap and not flagged).
A ``functools.cache``-decorated function is still scanning; the finding is its
call at module level.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]

_SCAN_ATTRS = frozenset({"rglob"})


def _is_scan_call(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    func = node.func
    if func.attr in _SCAN_ATTRS:
        return True
    # ``os.walk`` only: ``ast.walk`` walks a parsed tree in memory, not the filesystem.
    return func.attr == "walk" and isinstance(func.value, ast.Name) and func.value.id == "os"


def scanning_functions(tree: ast.Module) -> set[str]:
    """Module-local function names that reach a scan call, to a fixed point."""
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
    scanning = {name for name, fn in funcs.items() if any(_is_scan_call(s) for s in ast.walk(fn))}
    changed = True
    while changed:
        changed = False
        for name, fn in funcs.items():
            if name in scanning:
                continue
            for sub in ast.walk(fn):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id in scanning
                ):
                    scanning.add(name)
                    changed = True
                    break
    return scanning


def _import_time_nodes(node: ast.AST):
    """Yield nodes whose expressions run while the module imports (function bodies pruned).

    A function's decorators and default values run at import; its body does not. Class bodies
    run at import, their methods' bodies do not.
    """
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        exprs: list[ast.AST] = [*node.decorator_list, *node.args.defaults]
        exprs += [d for d in node.args.kw_defaults if d is not None]
        for child in exprs:
            yield from _import_time_nodes(child)
        return
    if isinstance(node, ast.Lambda):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _import_time_nodes(child)


def import_time_scans(source: str) -> list[str]:
    """Names of scanning module-local functions (or direct scans) run while the module imports."""
    tree = ast.parse(source)
    scanning = scanning_functions(tree)
    found: list[str] = []
    for stmt in tree.body:
        for sub in _import_time_nodes(stmt):
            if not isinstance(sub, ast.Call):
                continue
            if isinstance(sub.func, ast.Name) and sub.func.id in scanning:
                found.append(sub.func.id)
            elif isinstance(sub.func, ast.Attribute) and _is_scan_call(sub):
                found.append(sub.func.attr)
    return found


def test_no_test_module_scans_the_repo_at_import() -> None:
    offenders: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        rel = path.relative_to(TESTS_ROOT).as_posix()
        names = import_time_scans(path.read_text(encoding="utf-8"))
        offenders += [f"{rel}::{n}" for n in sorted(set(names))]
    assert not offenders, (
        "module-level repo scan runs in every pytest process at collection time; wrap it in "
        "functools.cache and call it from "
        f"the test body: {offenders}"
    )


def test_detector_fires_on_a_module_level_scan() -> None:
    src = (
        "from pathlib import Path\n"
        "def _scan():\n    return [p for p in Path('.').rglob('*.py')]\n"
        "def _outer():\n    return _scan()\n"
        "_X = _outer()\n"
    )
    assert import_time_scans(src) == ["_outer"]


def test_detector_stays_clean_on_a_deferred_scan() -> None:
    src = (
        "from functools import cache\nfrom pathlib import Path\n"
        "@cache\ndef _scan():\n    return list(Path('.').rglob('*.py'))\n"
        "def test_a():\n    assert _scan() is not None\n"
    )
    assert import_time_scans(src) == []


def test_collecting_a_unit_module_does_not_import_the_app() -> None:
    """tests/conftest.py imports the app lazily: collection alone must not load ``app.main``."""
    import subprocess
    import sys

    code = (
        "import sys, pytest;"
        "rc = pytest.main(['--collect-only', '-q', '-n0', '-p', 'no:cacheprovider', '-o', 'addopts=',"
        " 'tests/unit/test_no_docker_at_collection_time.py']);"
        "print('APP_MAIN_LOADED=' + str('app.main' in sys.modules)); sys.exit(rc)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=TESTS_ROOT.parent,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "APP_MAIN_LOADED=False" in proc.stdout, proc.stdout
