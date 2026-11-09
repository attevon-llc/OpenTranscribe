"""Tests for the .svelte unused-symbol ratchet in ``scripts/guards/check_svelte_unused.py``."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "guards" / "check_svelte_unused.py"
REASON = "pre-existing unused locals in svelte files, removed as each file is touched"


def _load():
    spec = importlib.util.spec_from_file_location("_guard_svelte_unused_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()


def diag(filename: str, code: int, ts: int = 1) -> str:
    body = json.dumps({"type": "ERROR", "filename": filename, "message": "m", "code": code})
    return f"{ts} {body}"


def output(*lines: str) -> str:
    return "\n".join(
        ['1 START "/x"', *lines, "9 COMPLETED 5 FILES 1 ERRORS 0 WARNINGS 1 FILES_WITH_PROBLEMS"]
    )


def run(tmp_path: Path, text: str, baseline: str, *extra: str) -> int:
    out = tmp_path / "out.txt"
    out.write_text(text, encoding="utf-8")
    base = tmp_path / "baseline.txt"
    base.write_text(baseline, encoding="utf-8")
    return int(guard.main(["--svelte-check-output", str(out), "--baseline", str(base), *extra]))


def test_parse_counts_only_unused_codes_in_svelte_files() -> None:
    text = output(
        diag("src/A.svelte", 6133),
        diag("src/A.svelte", 6133),
        diag("src/A.svelte", 6192),
        diag("src/B.svelte", 2322),  # a real type error, not this guard's business
        diag("src/c.ts", 6133),  # .ts is eslint's job
    )
    assert guard.parse(text) == {
        "frontend/src/A.svelte::TS6133": 2,
        "frontend/src/A.svelte::TS6192": 1,
    }


def test_parse_fails_closed_without_completed_line() -> None:
    with pytest.raises(guard.bl.ScanError):
        guard.parse(diag("src/A.svelte", 6133))


def test_new_unused_symbol_fails_and_baselined_passes(tmp_path: Path) -> None:
    text = output(diag("src/A.svelte", 6133))
    assert run(tmp_path, text, "") == 1
    assert run(tmp_path, text, f"frontend/src/A.svelte::TS6133\t1\t{REASON}\n") == 0


def test_count_rise_in_one_file_fails(tmp_path: Path) -> None:
    text = output(diag("src/A.svelte", 6133), diag("src/A.svelte", 6133))
    assert run(tmp_path, text, f"frontend/src/A.svelte::TS6133\t1\t{REASON}\n") == 1


def test_tighten_drops_fixed_file(tmp_path: Path) -> None:
    baseline = f"frontend/src/A.svelte::TS6133\t1\t{REASON}\n"
    assert run(tmp_path, output(), baseline, "--tighten") == 0
    assert (tmp_path / "baseline.txt").read_text(encoding="utf-8") == ""
