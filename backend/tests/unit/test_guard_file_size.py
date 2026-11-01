"""Behavior of ``scripts/guards/check_file_size.py`` on planted trees and on the real repo."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(os.environ.get("GUARD_REPO_ROOT", Path(__file__).resolve().parents[3]))
_SCRIPT = Path(os.environ.get("GUARD_SCRIPT", _REPO / "scripts" / "guards" / "check_file_size.py"))


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard_check_file_size", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()


def _make_repo(tmp_path: Path, files: dict[str, int]) -> Path:
    """A git repo whose tracked files hold the given number of lines each."""
    root = tmp_path / "repo"
    root.mkdir()
    for rel, n in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n" * n, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def _run(root: Path, baseline: Path, *extra: str) -> int:
    code: int = guard.main(["--root", str(root), "--baseline", str(baseline), *extra])
    return code


def _baseline(tmp_path: Path, *rows: tuple[str, int]) -> Path:
    path = tmp_path / "baseline.txt"
    path.write_text("".join(f"{k}\t{v}\tcohesive: test fixture\n" for k, v in rows), "utf-8")
    return path


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rel", "over"),
    [
        ("backend/app/a.py", 1001),
        ("frontend/src/lib/a.ts", 1001),
        ("frontend/scripts/a.js", 1001),
        ("frontend/scripts/a.mjs", 1001),
        ("scripts/a.sh", 1001),
        ("top.sh", 1001),
        ("frontend/src/lib/a.svelte", 2001),
        ("docs-site/src/a.ts", 1001),
        ("backend/tests/unit/test_a.py", 1501),
        ("backend/tests/conftest.py", 1501),
        ("frontend/src/lib/a.test.ts", 1501),
        ("scripts/sub/CLAUDE.md", 401),
        ("CLAUDE.md", 401),
    ],
)
def test_over_cap_file_fails(tmp_path: Path, rel: str, over: int) -> None:
    root = _make_repo(tmp_path, {rel: over})
    assert _run(root, _baseline(tmp_path)) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rel", "at_cap"),
    [
        ("backend/app/a.py", 1000),
        ("frontend/src/lib/a.svelte", 2000),
        ("backend/tests/unit/test_a.py", 1500),
        ("frontend/src/lib/a.test.ts", 1500),
        ("scripts/sub/CLAUDE.md", 400),
        ("backend/app/long.svelte", 2000),
    ],
)
def test_file_at_cap_passes(tmp_path: Path, rel: str, at_cap: int) -> None:
    root = _make_repo(tmp_path, {rel: at_cap})
    assert _run(root, _baseline(tmp_path)) == 0


@pytest.mark.unit
def test_svelte_markup_and_css_length_is_not_a_violation(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"frontend/src/lib/a.svelte": 1900})
    violations, _ = guard.scan(root)
    assert violations == {}


def _write_svelte(root: Path, rel: str, script_lines: int, other_lines: int) -> None:
    body = "<script>\n" + "let x = 1;\n" * script_lines + "</script>\n" + "<p>x</p>\n" * other_lines
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


@pytest.mark.unit
def test_svelte_script_logic_over_cap_fails_under_its_own_key(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"README.txt": 1})
    _write_svelte(root, "frontend/src/lib/a.svelte", script_lines=601, other_lines=10)
    violations, _ = guard.scan(root, ["frontend/src/lib/a.svelte"])
    assert violations == {"frontend/src/lib/a.svelte::script": 601}


@pytest.mark.unit
def test_svelte_script_at_cap_with_long_markup_passes(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"README.txt": 1})
    _write_svelte(root, "frontend/src/lib/a.svelte", script_lines=600, other_lines=1200)
    violations, _ = guard.scan(root, ["frontend/src/lib/a.svelte"])
    assert violations == {}


@pytest.mark.unit
def test_soft_target_is_reported_and_never_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_repo(tmp_path, {"backend/app/a.py": 650, "scripts/CLAUDE.md": 370})
    assert _run(root, _baseline(tmp_path)) == 0
    err = capsys.readouterr().err
    assert "above the soft size target" in err
    assert "backend/app/a.py: 650" in err
    assert "scripts/CLAUDE.md: 370" in err


@pytest.mark.unit
def test_clean_tree_passes_silently(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _make_repo(tmp_path, {"backend/app/a.py": 100, "frontend/src/a.svelte": 100})
    assert _run(root, _baseline(tmp_path)) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.unit
def test_baselined_file_passes_and_growth_fails(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"backend/app/big.py": 1200})
    assert _run(root, _baseline(tmp_path, ("backend/app/big.py", 1200))) == 0
    assert _run(root, _baseline(tmp_path, ("backend/app/big.py", 1199))) == 1


@pytest.mark.unit
def test_stale_baseline_entry_fails(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"backend/app/small.py": 10})
    baseline = _baseline(tmp_path, ("backend/app/small.py", 1200))
    assert _run(root, baseline) == 1


@pytest.mark.unit
def test_loosened_entry_fails_and_tighten_repairs_it(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"backend/app/big.py": 1100})
    baseline = _baseline(tmp_path, ("backend/app/big.py", 1300))
    assert _run(root, baseline) == 1
    assert _run(root, baseline, "--tighten") == 0
    assert baseline.read_text("utf-8").split("\t")[1] == "1100"
    assert _run(root, baseline) == 0


@pytest.mark.unit
def test_tighten_never_adds_a_new_violation(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"backend/app/big.py": 1100})
    baseline = _baseline(tmp_path)
    assert _run(root, baseline, "--tighten") == 1
    assert baseline.read_text("utf-8") == ""


@pytest.mark.unit
@pytest.mark.parametrize(
    "rel",
    [
        "frontend/src/lib/i18n/locales/en.ts",
        "backend/alembic/versions/v1_big.py",
        "backend/mutants/m.py",
        "node_modules/pkg/a.js",
        "docs/other.py",
        "frontend/src/lib/a.json",
        "frontend/src/lib/a.py.txt",
    ],
)
def test_excluded_or_out_of_scope_path_is_ignored(tmp_path: Path, rel: str) -> None:
    root = _make_repo(tmp_path, {rel: 5000})
    assert _run(root, _baseline(tmp_path)) == 0


@pytest.mark.unit
def test_root_non_shell_file_is_out_of_scope(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"tool.py": 5000})
    assert _run(root, _baseline(tmp_path)) == 0


@pytest.mark.unit
def test_not_a_git_repo_fails_closed(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _run(plain, _baseline(tmp_path)) == 2


@pytest.mark.unit
def test_malformed_baseline_fails_closed(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"backend/app/a.py": 10})
    bad = tmp_path / "bad.txt"
    bad.write_text("backend/app/a.py\tnot-a-number\treason\n", "utf-8")
    assert _run(root, bad) == 2


@pytest.mark.unit
def test_unreadable_file_fails_closed(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"backend/app/a.py": 10})
    (root / "backend/app/a.py").unlink()
    (root / "backend/app/a.py").mkdir()  # a directory where a tracked file was: read raises OSError
    assert _run(root, _baseline(tmp_path)) == 2


@pytest.mark.unit
def test_write_baseline_records_findings_and_round_trips(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"backend/app/big.py": 1100})
    out = tmp_path / "written.txt"
    assert (
        _run(root, out, "--write-baseline", "--reason", "oversize: split by concern planned") == 0
    )
    assert (
        out.read_text("utf-8") == "backend/app/big.py\t1100\toversize: split by concern planned\n"
    )
    assert _run(root, out) == 0


@pytest.mark.unit
def test_real_repo_passes_against_committed_baseline() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(_REPO)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
