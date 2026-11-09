"""Tests for ``scripts/guards/check_hidden_mypy.py`` (errors a ``type: ignore`` hides).

A guard that strips nothing or parses nothing reports a clean tree, so each stage has a
must-fire case: the stripper, the output parser, the comparison and the real mypy round trip.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "guards" / "check_hidden_mypy.py"


def _load() -> ModuleType:
    sys.path.insert(0, str(_SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("check_hidden_mypy_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()


@pytest.mark.unit
def test_strip_removes_directive_keeps_line_numbers_and_other_comment_text() -> None:
    src = (
        "a: int = 'x'  # type: ignore[assignment]\n"
        "b = f()  # noqa: E501  # type: ignore[arg-type, misc]  # why\n"
        "c = g()  # type: ignore\n"
        "# type: ignore\n"
    )
    out = guard.strip_type_ignores(src)
    assert out.count("\n") == src.count("\n")
    assert "type:" not in out
    assert out.splitlines()[0] == "a: int = 'x'"
    assert "noqa: E501" in out.splitlines()[1] and "why" in out.splitlines()[1]


@pytest.mark.unit
def test_strip_never_touches_string_literals() -> None:
    src = 's = "x  # type: ignore[arg-type]"\n'
    assert guard.strip_type_ignores(src) == src


@pytest.mark.unit
def test_parse_counts_per_path_and_code_and_skips_notes() -> None:
    out = (
        "backend/a.py:3: error: Bad  [arg-type]\n"
        "backend/a.py:9:5: error: Bad again  [arg-type]\n"
        "backend/a.py:9: note: see docs\n"
        "backend/b.py:1: error: Missing  [attr-defined]\n"
        "backend/c.py:1: error: no code on this one\n"
    )
    assert guard.parse_mypy_output(out) == {
        "backend/a.py::arg-type": 2,
        "backend/b.py::attr-defined": 1,
        "backend/c.py::no-code": 1,
    }


def _repo(tmp_path: Path, body: str, pyproject: str | None = None) -> Path:
    pin = subprocess.run(
        [sys.executable, "-m", "mypy", "--version"], capture_output=True, text=True, check=True
    ).stdout.split()[1]
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "m.py").write_text(body, encoding="utf-8")
    (tmp_path / "backend" / "requirements-dev.txt").write_text(f"mypy=={pin}\n")
    (tmp_path / "pyproject.toml").write_text(
        pyproject or '[tool.mypy]\nmypy_path = "backend"\nwarn_unused_ignores = true\n'
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def _argv(root: Path, baseline: str = "", allow: str | None = None) -> list[str]:
    (root / "b.txt").write_text(baseline)
    argv = ["--root", str(root), "--baseline", str(root / "b.txt")]
    if allow is not None:
        (root / "a.txt").write_text(allow)
        argv += ["--allowlist", str(root / "a.txt")]
    return argv


HIDDEN = "x: int = 'a'  # type: ignore[assignment]\n"


@pytest.mark.unit
def test_real_mypy_round_trip_finds_the_hidden_error(tmp_path: Path) -> None:
    root = _repo(tmp_path, HIDDEN)
    assert guard.hidden_errors(root) == {"backend/m.py::assignment": 1}


@pytest.mark.unit
def test_new_hidden_error_fails_baselined_passes_and_clean_tree_passes(tmp_path: Path) -> None:
    root = _repo(tmp_path, HIDDEN)
    assert guard.main(_argv(root)) == 1
    assert (
        guard.main(_argv(root, "backend/m.py::assignment\t1\tcovered by an existing ignore\n")) == 0
    )


@pytest.mark.unit
def test_two_errors_under_one_ignore_exceed_a_baseline_of_one(tmp_path: Path) -> None:
    root = _repo(
        tmp_path, "def f(a: int, b: int) -> None: ...\nf('a', 'b')  # type: ignore[arg-type]\n"
    )
    assert (
        guard.main(_argv(root, "backend/m.py::arg-type\t1\tcovered by an existing ignore\n")) == 1
    )


@pytest.mark.unit
def test_allowlisted_stub_gap_passes_and_stale_baseline_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path, HIDDEN)
    allow = "backend/m.py::assignment\t1\tstub-gap https://github.com/o/r/issues/1 no stub\n"
    assert guard.main(_argv(root, allow=allow)) == 0
    stale = "backend/gone.py::assignment\t1\tcovered by an existing ignore\n"
    assert guard.main(_argv(root, stale, allow=allow)) == 1


@pytest.mark.unit
def test_mypy_crash_exits_two(tmp_path: Path) -> None:
    root = _repo(tmp_path, HIDDEN, pyproject='[tool.mypy]\nplugins = ["no_such_plugin_xyz"]\n')
    assert guard.main(_argv(root)) == 2


@pytest.mark.unit
def test_wrong_mypy_pin_exits_two(tmp_path: Path) -> None:
    root = _repo(tmp_path, HIDDEN)
    (root / "backend" / "requirements-dev.txt").write_text("mypy==0.0.1\n")
    assert guard.main(_argv(root)) == 2


@pytest.mark.unit
def test_tree_without_hidden_errors_passes(tmp_path: Path) -> None:
    assert guard.main(_argv(_repo(tmp_path, "x: int = 1\n"))) == 0
