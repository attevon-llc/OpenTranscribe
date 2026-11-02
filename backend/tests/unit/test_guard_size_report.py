"""Behavior of ``scripts/guards/size_report.py`` on planted trees and on the real repo."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(os.environ.get("GUARD_REPO_ROOT", Path(__file__).resolve().parents[3]))
_SCRIPT = Path(os.environ.get("GUARD_SCRIPT", _REPO / "scripts" / "guards" / "size_report.py"))


def _load() -> ModuleType:
    sys.path.insert(0, str(_SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("guard_size_report", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()

# One planted file per group, each past its soft target and (for hard) its hard cap.
_ONE_PER_GROUP = {
    "backend/app/a.py": ("Python (non-test)", 1100),
    "frontend/src/lib/a.ts": ("TypeScript/JS (non-test)", 1200),
    "scripts/a.sh": ("Shell (non-test)", 1300),
    "backend/tests/unit/test_a.py": ("Tests (py/ts/js/sh)", 1600),
    "CLAUDE.md": ("CLAUDE.md", 450),
}


def _make_repo(tmp_path: Path, files: dict[str, int]) -> Path:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    for rel, n in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n" * n, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def _svelte(root: Path, rel: str, script_lines: int, other_lines: int) -> None:
    body = "<script>\n" + "let x = 1;\n" * script_lines + "</script>\n" + "<p>x</p>\n" * other_lines
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


def _run(root: Path, snap: Path, *extra: str) -> int:
    code: int = guard.main(["--root", str(root), "--snapshot", str(snap), *extra])
    return code


def _stats(root: Path) -> dict[str, dict[str, object]]:
    stats: dict[str, dict[str, object]] = guard.build_stats(guard.collect(root))
    return stats


@pytest.mark.unit
def test_every_group_is_classified_with_its_debt(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {rel: n for rel, (_, n) in _ONE_PER_GROUP.items()})
    _svelte(root, "frontend/src/lib/a.svelte", script_lines=700, other_lines=1400)
    stats = _stats(root)
    assert set(stats) == set(guard.GROUPS)
    for rel, (group, n) in _ONE_PER_GROUP.items():
        st = stats[group]
        assert (st["files"], st["max"], st["over_hard"]) == (1, n, 1), rel
    assert stats["Svelte <script> logic"]["max"] == 700
    assert stats["Svelte total file"]["max"] == 2102
    assert stats["Svelte total file"]["over_hard"] == 1
    py_debt = stats["Python (non-test)"]["debt"]
    assert py_debt == {"files_over_hard": 1, "lines_above_hard": 100, "lines_above_soft": 600}


@pytest.mark.unit
def test_out_of_scope_files_are_not_counted(tmp_path: Path) -> None:
    root = _make_repo(
        tmp_path,
        {"backend/alembic/versions/v.py": 5000, "tool.py": 5000, "docs/x.py": 5000},
    )
    assert _stats(root) == {}


@pytest.mark.unit
def test_percentiles_use_nearest_rank() -> None:
    sizes = list(range(1, 101))
    assert [guard.percentile(sizes, p) for p in (50, 90, 95)] == [50, 90, 95]
    assert guard.percentile([], 90) == 0
    assert guard.percentile([7], 95) == 7


@pytest.mark.unit
def test_suggestion_rounds_to_fifty_with_hard_double_soft() -> None:
    assert guard.suggest(627) == (650, 1300)
    assert guard.suggest(10) == (50, 100)


@pytest.mark.unit
def test_far_caps_are_flagged_in_the_table(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"CLAUDE.md": 900, "backend/app/a.py": 100})
    groups = guard.collect(root)
    table = guard.format_table(groups, guard.build_stats(groups))
    claude = next(line for line in table.splitlines() if line.startswith("  CLAUDE.md: soft"))
    py = next(line for line in table.splitlines() if line.startswith("  Python (non-test): soft"))
    assert "far from the data" in claude
    assert "soft 100, hard 200" in py


@pytest.mark.unit
def test_snapshot_round_trip_and_stable_date(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"backend/app/a.py": 1100})
    snap = tmp_path / "snap.json"
    assert _run(root, snap, "--write-snapshot") == 0
    first = snap.read_text("utf-8")
    assert json.loads(first)["groups"]["Python (non-test)"]["max"] == 1100
    assert _run(root, snap) == 0
    assert guard.write_snapshot(snap, _stats(root), "1999-01-01") is False
    assert snap.read_text("utf-8") == first


@pytest.mark.unit
def test_date_changes_only_when_figures_change(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"backend/app/a.py": 1100})
    snap = tmp_path / "snap.json"
    guard.write_snapshot(snap, _stats(root), "2026-01-01")
    (root / "backend/app/a.py").write_text("x\n" * 1050, encoding="utf-8")
    assert guard.write_snapshot(snap, _stats(root), "2026-02-02") is True
    assert json.loads(snap.read_text("utf-8"))["date"] == "2026-02-02"


@pytest.mark.unit
def test_growing_lines_above_hard_fails(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"backend/app/a.py": 1100})
    snap = tmp_path / "snap.json"
    assert _run(root, snap, "--write-snapshot") == 0
    (root / "backend/app/a.py").write_text("x\n" * 1101, encoding="utf-8")
    assert _run(root, snap) == 1


@pytest.mark.unit
def test_new_over_hard_file_in_a_new_group_fails(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"backend/app/a.py": 100})
    snap = tmp_path / "snap.json"
    assert _run(root, snap, "--write-snapshot") == 0
    (root / "scripts").mkdir()
    (root / "scripts/big.sh").write_text("x\n" * 1100, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    assert _run(root, snap) == 1


@pytest.mark.unit
def test_shrinking_tree_passes_and_reports_delta(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_repo(tmp_path, {"backend/app/a.py": 1100})
    snap = tmp_path / "snap.json"
    assert _run(root, snap, "--write-snapshot") == 0
    (root / "backend/app/a.py").write_text("x\n" * 1050, encoding="utf-8")
    capsys.readouterr()
    assert _run(root, snap) == 0
    assert "lines_above_hard 100 -> 50 (-50)" in capsys.readouterr().err


@pytest.mark.unit
def test_growth_below_hard_cap_is_not_an_error(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"backend/app/a.py": 600})
    snap = tmp_path / "snap.json"
    assert _run(root, snap, "--write-snapshot") == 0
    (root / "backend/app/a.py").write_text("x\n" * 900, encoding="utf-8")
    assert _run(root, snap) == 0


@pytest.mark.unit
def test_missing_or_malformed_snapshot_fails_closed(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, {"backend/app/a.py": 10})
    assert _run(root, tmp_path / "absent.json") == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", "utf-8")
    assert _run(root, bad) == 2


@pytest.mark.unit
def test_not_a_git_repo_fails_closed(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _run(plain, tmp_path / "s.json") == 2


@pytest.mark.unit
def test_json_output_matches_snapshot_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_repo(tmp_path, {"backend/app/a.py": 1100})
    snap = tmp_path / "snap.json"
    _run(root, snap, "--write-snapshot")
    capsys.readouterr()
    assert _run(root, snap, "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["groups"]["Python (non-test)"]["debt"]["lines_above_hard"] == 100


@pytest.mark.unit
def test_real_repo_matches_committed_snapshot_without_growth() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(_REPO)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
