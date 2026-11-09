"""Tests for the duplicate-code ratchet in ``scripts/guards/check_duplicates.py``."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "guards" / "check_duplicates.py"
REASON = "pre-existing duplicated blocks, merged in the duplicate-code lanes"


def _load():
    spec = importlib.util.spec_from_file_location("_guard_duplicates_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()


def _side(name: str) -> dict:
    return {"name": f"/r/{name}", "start": 1, "end": 9}


def clone(a: str, b: str, tokens: int = 120) -> dict:
    return {
        "firstFile": _side(a),
        "secondFile": _side(b),
        "tokens": tokens,
        "lines": 9,
        "format": "python",
    }


def report(*clones: dict) -> str:
    return json.dumps({"duplicates": list(clones), "statistics": {}})


def run(tmp_path: Path, text: str, baseline: str, *extra: str) -> int:
    rep = tmp_path / "report.json"
    rep.write_text(text, encoding="utf-8")
    base = tmp_path / "baseline.txt"
    base.write_text(baseline, encoding="utf-8")
    argv = ["--root", "/r", "--jscpd-json", str(rep), "--baseline", str(base), *extra]
    return int(guard.main(argv))


def test_parse_counts_each_side_and_sorts_largest_first() -> None:
    counts, clones = guard.parse(
        report(clone("a.py", "b.py", 100), clone("a.py", "frontend/src/c.ts", 300)), Path("/r")
    )
    assert counts == {"a.py": 2, "b.py": 1, "frontend/src/c.ts": 1}
    assert [c["tokens"] for c in clones] == [300, 100]
    assert clones[0]["area"] == "backend+frontend"


def test_parse_fails_closed_on_malformed_report() -> None:
    with pytest.raises(guard.bl.ScanError):
        guard.parse("{}", Path("/r"))
    with pytest.raises(guard.bl.ScanError):
        guard.parse(report({"firstFile": {}}), Path("/r"))


def test_new_clone_fails_and_baselined_passes(tmp_path: Path) -> None:
    text = report(clone("a.py", "b.py"))
    assert run(tmp_path, text, "") == 1
    assert run(tmp_path, text, f"a.py\t1\t{REASON}\nb.py\t1\t{REASON}\n") == 0


def test_second_clone_in_baselined_file_fails(tmp_path: Path) -> None:
    text = report(clone("a.py", "b.py"), clone("a.py", "c.py"))
    base = f"a.py\t1\t{REASON}\nb.py\t1\t{REASON}\nc.py\t1\t{REASON}\n"
    assert run(tmp_path, text, base) == 1


def test_tighten_and_report(tmp_path: Path) -> None:
    out = tmp_path / "groups.json"
    assert run(tmp_path, report(), f"a.py\t1\t{REASON}\n", "--tighten", "--report", str(out)) == 0
    assert (tmp_path / "baseline.txt").read_text(encoding="utf-8") == ""
    assert json.loads(out.read_text(encoding="utf-8"))["clones"] == []
