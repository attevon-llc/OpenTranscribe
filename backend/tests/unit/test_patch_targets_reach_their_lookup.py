"""A string patch target on a package re-export must be a name something reads through the package.

``monkeypatch.setattr("app.api.endpoints.files.get_thumbnail_streaming_response", stub)`` replaces
the package's re-export, and nothing else. After the thumbnail route moved to
``files/media_delivery.py``, which imports the function from ``.streaming``, the route looked the
name up in ``media_delivery``; the stub was installed where nobody read it, and the test read real
storage. The target still existed, so a "does the target exist" check cannot see this.

This test flags a ``monkeypatch.setattr`` / ``patch`` string target ``<package>.<name>`` when
``<name>`` is a relative re-export in ``<package>/__init__.py`` and no code under ``app/`` reads it
through the package: not the ``__init__`` itself, not ``from <package> import <name>``, and not
``<package>.<name>`` through an imported package. Patch where the name is looked up instead.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PATCHERS = frozenset({"setattr", "patch"})


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _package_init(root: Path, module: str) -> Path | None:
    init = root.joinpath(*module.split("."), "__init__.py")
    return init if init.is_file() else None


def _relative_reexports(init: Path) -> set[str]:
    return {
        alias.asname or alias.name
        for node in _parse(init).body
        if isinstance(node, ast.ImportFrom) and node.level >= 1
        for alias in node.names
    }


def _loaded_names(tree: ast.Module) -> set[str]:
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _package_aliases(tree: ast.Module, package: str) -> set[str]:
    """Local names that are bound to ``package`` in ``tree``."""
    parent, _, leaf = package.rpartition(".")
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == parent:
            aliases |= {a.asname or a.name for a in node.names if a.name == leaf}
        elif isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == package and a.asname}
    return aliases


def _read_through_package(code_root: Path, package: str, name: str) -> bool:
    init = _package_init(code_root.parent, package)
    if init is not None and name in _loaded_names(_parse(init)):
        return True
    for path in code_root.rglob("*.py"):
        tree = _parse(path)
        aliases = _package_aliases(tree, package)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module == package
                and any(a.name == name for a in node.names)
            ):
                return True
            if (
                isinstance(node, ast.Attribute)
                and node.attr == name
                and isinstance(node.value, ast.Name)
                and node.value.id in aliases
            ):
                return True
    return False


def _string_patch_targets(tests_root: Path, prefix: str) -> Iterator[tuple[str, int, str]]:
    for path in sorted(tests_root.rglob("*.py")):
        for node in ast.walk(_parse(path)):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            first = node.args[0]
            if (
                called in PATCHERS
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value.startswith(prefix)
            ):
                yield str(path.relative_to(tests_root.parent)), node.lineno, first.value


def inert_reexport_patches(code_root: Path, tests_root: Path) -> list[str]:
    """``file:line: target`` for each string patch on a re-export nobody reads via the package."""
    prefix = f"{code_root.name}."
    found: list[str] = []
    reexports: dict[str, set[str]] = {}
    for where, line, target in _string_patch_targets(tests_root, prefix):
        package, _, name = target.rpartition(".")
        if package not in reexports:
            init = _package_init(code_root.parent, package)
            reexports[package] = _relative_reexports(init) if init else set()
        if name in reexports[package] and not _read_through_package(code_root, package, name):
            found.append(f"{where}:{line}: {target}")
    return found


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _world(tmp_path: Path, init_body: str, user: str) -> tuple[Path, Path]:
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/pkg/__init__.py", "from .impl import helper\n" + init_body)
    _write(tmp_path, "app/pkg/impl.py", "def helper():\n    return 1\n")
    _write(tmp_path, "app/pkg/route.py", user)
    _write(
        tmp_path,
        "tests/test_x.py",
        "def test_x(monkeypatch):\n    monkeypatch.setattr('app.pkg.helper', lambda: 2)\n",
    )
    return tmp_path / "app", tmp_path / "tests"


def test_flags_a_patch_on_a_reexport_read_only_from_the_submodule(tmp_path):
    code, tests = _world(tmp_path, "", "from .impl import helper\n\ndef route():\n    helper()\n")
    assert inert_reexport_patches(code, tests) == ["tests/test_x.py:2: app.pkg.helper"]


def test_a_reexport_the_package_itself_calls_is_a_live_target(tmp_path):
    code, tests = _world(tmp_path, "\ndef route():\n    return helper()\n", "")
    assert inert_reexport_patches(code, tests) == []


def test_a_reexport_imported_from_the_package_is_a_live_target(tmp_path):
    code, tests = _world(tmp_path, "", "from app.pkg import helper\n\ndef route():\n    helper()\n")
    assert inert_reexport_patches(code, tests) == []


def test_a_reexport_read_as_a_package_attribute_is_a_live_target(tmp_path):
    user = "from app import pkg\n\ndef route():\n    pkg.helper()\n"
    code, tests = _world(tmp_path, "", user)
    assert inert_reexport_patches(code, tests) == []


def test_no_string_patch_targets_an_unread_reexport():
    found = inert_reexport_patches(BACKEND / "app", BACKEND / "tests")
    assert found == [], (
        "these patches replace a package re-export that no app code reads through the "
        "package, so the code under test never sees the stub; patch the module that looks "
        "the name up:\n" + "\n".join(found)
    )
