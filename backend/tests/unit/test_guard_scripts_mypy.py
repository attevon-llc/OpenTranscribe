"""Tests for ``scripts/guards/check_scripts_mypy.py`` (real mypy over a planted tree)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "guards" / "check_scripts_mypy.py"


def _load() -> ModuleType:
    sys.path.insert(0, str(_SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("check_scripts_mypy_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()


def _repo(tmp_path: Path, body: str | None, package: str = "guards") -> Path:
    pin = subprocess.run(
        [sys.executable, "-m", "mypy", "--version"], capture_output=True, text=True, check=True
    ).stdout.split()[1]
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "requirements-dev.txt").write_text(f"mypy=={pin}\n")
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\ncheck_untyped_defs = true\n")
    if body is not None:
        pkg = tmp_path / "scripts" / package
        pkg.mkdir(parents=True)
        (pkg / "m.py").write_text(body, encoding="utf-8")
    return tmp_path


BAD = "x: int = 'a'\n"


def _argv(root: Path, baseline: str = "") -> list[str]:
    (root / "b.txt").write_text(baseline)
    return ["--root", str(root), "--baseline", str(root / "b.txt")]


@pytest.mark.unit
def test_new_error_fails_baselined_passes_and_clean_passes(tmp_path: Path) -> None:
    root = _repo(tmp_path, BAD)
    assert guard.scripts_errors(root) == {"scripts/guards/m.py::assignment": 1}
    assert guard.main(_argv(root)) == 1
    assert guard.main(_argv(root, "scripts/guards/m.py::assignment\t1\tfirst measurement\n")) == 0
    (root / "scripts/guards/m.py").write_text("x: int = 1\n")
    assert guard.main(_argv(root)) == 0


@pytest.mark.unit
def test_growth_fails_and_stale_entry_fails(tmp_path: Path) -> None:
    (tmp_path / "s").mkdir()
    root = _repo(tmp_path, "x: int = 'a'\ny: int = 'b'\n")
    assert guard.main(_argv(root, "scripts/guards/m.py::assignment\t1\tfirst measurement\n")) == 1
    clean = _repo(tmp_path / "s", "x: int = 1\n")
    stale = "scripts/guards/m.py::assignment\t1\tfirst measurement\n"
    assert guard.main(_argv(clean, stale)) == 1


@pytest.mark.unit
def test_missing_packages_fail_closed(tmp_path: Path) -> None:
    assert guard.main(_argv(_repo(tmp_path, None))) == 2


@pytest.mark.unit
def test_package_outside_the_tooling_set_is_not_scanned(tmp_path: Path) -> None:
    assert guard.main(_argv(_repo(tmp_path, BAD, package="benchmarks"))) == 2


@pytest.mark.unit
def test_warm_cache_still_catches_a_cross_module_type_change(tmp_path: Path) -> None:
    root = _repo(tmp_path, "def f() -> int:\n    return 1\n")
    (root / "scripts/guards/user.py").write_text("from m import f\n\nx: int = f()\n")
    assert guard.scripts_errors(root) == {}
    assert (root / ".cache/ot-guards/scripts-mypy/mypy").is_dir()
    (root / "scripts/guards/m.py").write_text("def f() -> str:\n    return 'a'\n")
    assert guard.scripts_errors(root) == {"scripts/guards/user.py::assignment": 1}
    (root / "scripts/guards/m.py").write_text("def f() -> int:\n    return 1\n")
    assert guard.scripts_errors(root) == {}
