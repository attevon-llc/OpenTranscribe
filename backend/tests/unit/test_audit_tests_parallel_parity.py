"""The audit-tests scan must give identical output serial, parallel and with the walk memo."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "audit-tests.py"
sys.path.insert(0, str(_REPO / "scripts"))

from audit_tests import cli
from audit_tests.scan import scan_file
from audit_tests.selftest_cases import SELFTEST_CASES


def _plant(root: Path) -> None:
    for i, (_category, source) in enumerate(SELFTEST_CASES):
        (root / f"test_case_{i:03d}.py").write_text(source, encoding="utf-8")


def _run(root: Path, workers: int) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), str(root), "--json"],
        env={**os.environ, "OT_AUDIT_WORKERS": str(workers)},
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, json.loads(proc.stdout)


@pytest.mark.unit
def test_serial_and_parallel_json_identical_with_every_category_firing(tmp_path: Path) -> None:
    _plant(tmp_path)
    serial_rc, serial = _run(tmp_path, 1)
    parallel_rc, parallel = _run(tmp_path, 4)
    fired = {f["category"] for f in serial["findings"]}
    assert len(serial["findings"]) >= len(SELFTEST_CASES) // 2
    assert {c for c, _ in SELFTEST_CASES} <= fired
    assert (serial_rc, serial) == (parallel_rc, parallel)


@pytest.mark.unit
def test_memoized_walk_matches_ast_walk_node_for_node() -> None:
    tree = ast.parse(SELFTEST_CASES[0][1] + SELFTEST_CASES[5][1])
    expected = [id(n) for n in cli._ORIG_WALK(tree)]
    assert [id(n) for n in cli._memoized_walk(tree)] == expected
    assert [id(n) for n in cli._memoized_walk(tree)] == expected


@pytest.mark.unit
def test_scan_tree_serial_equals_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plant(tmp_path)
    paths = sorted(tmp_path.glob("*.py"))
    direct = [f for p in paths for f in scan_file(p, tmp_path)]
    monkeypatch.setenv("OT_AUDIT_WORKERS", "3")
    assert cli.scan_tree(paths, tmp_path) == direct
