"""No test module may invoke the ``docker`` CLI at import (i.e. collection) time.

WHY THIS EXISTS
---------------
``tests/integration/test_scheduled_backup_restore_roundtrip.py`` carried::

    @pytest.mark.skipif(not _backend_image_has_gpg(), reason=_GPG_SKIP_REASON)
    def test_gpg_encrypted_scheduled_artifact_round_trips(...):

A decorator's arguments are evaluated **while the module is being imported**, and pytest
imports every test module in order to collect it. ``_backend_image_has_gpg()`` shells out to
``docker run --rm --entrypoint gpg opentranscribe-backend:latest --version`` — starting a
container from a 9.6 GB image. So every pytest process paid a container start before a single
test ran, *including* the fast suite, which deselects that whole file via
``pytest.mark.integration`` and never runs one line of it.

Measured with ``python -m cProfile -o out.prof -m pytest tests/ --collect-only -n0``: that one
call was **48.9 s of an 88 s serial collection** — ``select.poll`` blocking inside
``subprocess.communicate``. The project runs ``-n auto`` (48 workers on this host), and each
xdist worker collects the whole tree independently, so the real shape was ~48 concurrent
``docker run`` invocations of the same enormous image, every run.

The fix is not to delete the probe — the skip it produces is real information. It is to let
pytest evaluate the condition when the condition is *needed*: ``skipif`` accepts a **string**
condition, which ``_pytest.skipping.evaluate_condition`` ``eval()``s during
``pytest_runtest_setup`` (still before any fixture is created), instead of a pre-computed bool
that the decorator line forces at import.

THE RULE
--------
Anything a test module evaluates at import time is paid by every pytest process — one per
xdist worker, on every phase of the gate, whether or not that module's tests are selected.
Starting a container there is never worth it. Cheap module-scope shell-outs are *not* flagged:
``tests/unit/test_diar_engine_verdict_db_backed.py`` runs ``sed`` at import to extract two
shell functions and that costs ~2.5 ms, which is why this detector keys on ``docker``
specifically rather than on ``subprocess`` generally.

WHAT "IMPORT TIME" COVERS HERE
------------------------------
Top-level statements, decorator arguments, default argument values, and class bodies — every
construct whose expressions run as a side effect of ``import``. Function *bodies* are exempt by
construction: that is where this work belongs.

LIMITS, STATED PLAINLY
----------------------
Static analysis over one file plus the ``tests.*`` modules it imports names from. It will not
see ``docker`` reached through a variable holding a callable, through ``getattr``, or through a
non-``tests`` third-party helper. It catches the shape that actually shipped.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest

#: backend/tests — the tree this rule governs.
TESTS_ROOT = Path(__file__).resolve().parents[1]
#: backend/ — the import root that `tests.a.b` module paths resolve against.
BACKEND_ROOT = TESTS_ROOT.parent

_SUBPROCESS_ARGV_HEAD = "docker"


def _is_docker_argv(node: ast.AST) -> bool:
    """True if *node* contains a list/tuple literal whose first element is ``"docker"``.

    That is the argv shape every docker call in this tree uses
    (``_run(["docker", "run", ...])``). Keying on the argv head rather than on the bare
    string ``"docker"`` anywhere keeps a docstring or a skip *reason* that merely mentions
    docker from being flagged.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.List | ast.Tuple) and sub.elts:
            first = sub.elts[0]
            if isinstance(first, ast.Constant) and first.value == _SUBPROCESS_ARGV_HEAD:
                return True
    return False


def _called_names(node: ast.AST) -> set[str]:
    """Every bare-name call (``foo(...)``) appearing anywhere under *node*."""
    return {
        sub.func.id
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
    }


@cache
def _docker_touching_names(path: Path) -> frozenset[str]:
    """Names in *path* that reach a ``docker`` argv, directly or through another local name.

    Resolved to a fixed point, and extended across ``from tests.x.y import name`` so that a
    helper imported from a sibling test module is analysed in the module that defines it —
    which is exactly how ``_run`` reaches this file's callers.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return frozenset()

    functions: dict[str, ast.AST] = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    touching: set[str] = {name for name, node in functions.items() if _is_docker_argv(node)}

    # Names imported from a sibling `tests.*` module, carrying that module's verdict.
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("tests.") or node.level:
            continue
        sibling = BACKEND_ROOT / (node.module.replace(".", "/") + ".py")
        if not sibling.is_file() or sibling == path:
            continue
        inherited = _docker_touching_names(sibling)
        touching |= {alias.name for alias in node.names if alias.name in inherited}

    # Fixed point: a function calling a docker-touching name is itself docker-touching.
    while True:
        grown = {
            name
            for name, node in functions.items()
            if name not in touching and _called_names(node) & touching
        }
        if not grown:
            return frozenset(touching)
        touching |= grown


def _import_time_expressions(tree: ast.Module) -> list[ast.AST]:
    """Every expression a bare ``import`` of the module would evaluate.

    Top-level statements, decorators, argument defaults and class bodies — but never a
    function body, which is the whole point: that is where an expensive probe belongs.
    """
    found: list[ast.AST] = []

    def visit(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                found.extend(stmt.decorator_list)
                found.extend(d for d in stmt.args.defaults if d is not None)
                found.extend(d for d in stmt.args.kw_defaults if d is not None)
            elif isinstance(stmt, ast.ClassDef):
                found.extend(stmt.decorator_list)
                visit(stmt.body)
            else:
                found.append(stmt)

    visit(tree.body)
    return found


def _findings(path: Path) -> list[str]:
    """``file:line`` for every import-time expression in *path* that reaches docker."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    touching = _docker_touching_names(path)
    out: list[str] = []
    for node in _import_time_expressions(tree):
        if _is_docker_argv(node):
            out.append(f"{path}:{getattr(node, 'lineno', 0)} runs a docker argv at import time")
        for name in sorted(_called_names(node) & touching):
            out.append(f"{path}:{getattr(node, 'lineno', 0)} calls {name}() at import time")
    return out


def _test_modules() -> list[Path]:
    return sorted(TESTS_ROOT.rglob("*.py"))


def test_the_scan_actually_sees_this_tree():
    """Non-vacuity: every assertion below iterates these, so an empty scan passes everything."""
    modules = _test_modules()
    assert len(modules) > 500, f"expected the whole backend/tests tree, found {len(modules)} files"


def test_the_detector_still_finds_docker_helpers_in_this_tree():
    """Non-vacuity for the *detector*, not just the file list.

    ``_docker_touching_names`` returning empty everywhere would make the real check below pass
    unconditionally — a green vacuum, and indistinguishable from a clean tree. The
    backup/restore round-trip modules genuinely define docker helpers; they must simply not be
    *called* at import time.
    """
    known = TESTS_ROOT / "integration" / "test_scheduled_backup_restore_roundtrip.py"
    if not known.is_file():
        pytest.skip("test_scheduled_backup_restore_roundtrip.py is not in this checkout")
    names = _docker_touching_names(known)
    assert "_backend_image_has_gpg" in names, (
        f"the detector no longer recognises a known docker helper; it found {sorted(names)}. "
        "Fix the detector — do not trust the clean result below until it does."
    )


def test_no_test_module_invokes_docker_at_import_time():
    findings = [line for path in _test_modules() for line in _findings(path)]
    assert not findings, (
        "these expressions shell out to `docker` while the module is being IMPORTED, so every "
        "pytest process pays them during collection — one per xdist worker (-n auto = 48 here), "
        "on every gate phase, even phases that deselect the file entirely:\n  "
        + "\n  ".join(findings)
        + "\n\nMove the probe to where the answer is needed. For a `skipif`, pass the condition "
        'as a STRING — `@pytest.mark.skipif("not _probe()", reason=...)` — which pytest eval()s '
        "in pytest_runtest_setup, before any fixture is created, instead of a bool the decorator "
        "line forces at import. Add functools.cache to the probe so it still runs at most once "
        "per process."
    )


def test_a_module_probing_docker_from_a_decorator_would_be_detected(tmp_path: Path):
    """Must-fire case, in the exact shape that shipped.

    A detector that matches nothing reports zero findings, which looks identical to a clean
    tree. This pins the positive.
    """
    offender = tmp_path / "test_offender.py"
    offender.write_text(
        "import subprocess\n"
        "import pytest\n"
        "\n"
        "def _run(cmd):\n"
        "    return subprocess.run(cmd, capture_output=True)\n"
        "\n"
        "def _image_has_gpg():\n"
        '    return _run(["docker", "run", "--rm", "img", "gpg", "--version"]).returncode == 0\n'
        "\n"
        '@pytest.mark.skipif(not _image_has_gpg(), reason="no gpg")\n'
        "def test_something():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    findings = _findings(offender)
    assert findings, "the detector missed a docker probe evaluated in a decorator argument"
    assert any("_image_has_gpg()" in line for line in findings), findings


def test_the_fixed_shape_and_ordinary_docker_helpers_stay_clean(tmp_path: Path):
    """Must-stay-clean case: the string-condition fix, plus a helper only tests call.

    Without this, a detector that flagged every mention of docker anywhere would "pass" the
    must-fire case above while making the rule unusable.
    """
    fixed = tmp_path / "test_fixed.py"
    fixed.write_text(
        "import functools\n"
        "import subprocess\n"
        "import pytest\n"
        "\n"
        "def _run(cmd):\n"
        "    return subprocess.run(cmd, capture_output=True)\n"
        "\n"
        "@functools.cache\n"
        "def _image_has_gpg():\n"
        '    return _run(["docker", "run", "--rm", "img", "gpg", "--version"]).returncode == 0\n'
        "\n"
        'REASON = "the docker image has no gpg"\n'
        "\n"
        '@pytest.mark.skipif("not _image_has_gpg()", reason=REASON)\n'
        "def test_something():\n"
        '    assert _run(["docker", "ps"]).returncode == 0\n',
        encoding="utf-8",
    )
    assert _findings(fixed) == [], (
        "the string-condition form defers the probe to pytest_runtest_setup and a call inside a "
        "test body is not import-time work; neither may be flagged"
    )
