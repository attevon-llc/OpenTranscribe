"""scripts/check-venv-drift.py reports installed-vs-pinned mismatches and exits accordingly."""

from __future__ import annotations

import importlib.metadata as md
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-venv-drift.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_mismatched_pin_is_reported_and_exits_1(tmp_path: Path) -> None:
    installed = md.version("pytest")
    req = tmp_path / "r.txt"
    req.write_text("pytest==0.0.1\n")
    result = _run("--requirements", str(req))
    assert result.returncode == 1
    assert f"pytest {installed} != 0.0.1" in result.stdout


def test_matching_pin_and_normalised_name_exit_0(tmp_path: Path) -> None:
    req = tmp_path / "r.txt"
    req.write_text(
        f"PyTest[extra]=={md.version('pytest')}  # comment\n"
        "# a comment\n-r other.txt\n--index-url https://example.invalid\n"
        "pytest-cov>=1\n"
    )
    result = _run("--requirements", str(req))
    assert result.returncode == 0, result.stdout
    assert result.stdout == ""


def test_marker_line_is_skipped_with_note(tmp_path: Path) -> None:
    req = tmp_path / "r.txt"
    req.write_text("pytest==0.0.1 ; sys_platform == 'nonexistent'\n")
    result = _run("--requirements", str(req))
    assert result.returncode == 0
    assert "skipped pytest" in result.stdout


def test_uninstalled_package_is_drift(tmp_path: Path) -> None:
    req = tmp_path / "r.txt"
    req.write_text("definitely-not-a-real-pkg-xyz==1.0\n")
    result = _run("--requirements", str(req))
    assert result.returncode == 1
    assert "not installed" in result.stdout


def test_unreadable_file_exits_2(tmp_path: Path) -> None:
    result = _run("--requirements", str(tmp_path / "missing.txt"))
    assert result.returncode == 2


def test_local_version_label_satisfies_unlabelled_pin(tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPT.parent))
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_venv_drift", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.satisfies("2.11.0+cu128", "2.11.0")
    assert not mod.satisfies("2.11.0+cu128", "2.11.0+cpu")
    assert not mod.satisfies("2.11.1", "2.11.0")


def test_real_requirements_have_no_drift() -> None:
    result = _run()
    assert result.returncode == 0, result.stdout
