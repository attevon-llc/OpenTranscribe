"""Tests for the noUncheckedIndexedAccess ratchet in ``scripts/guards/check_nuia_ratchet.py``."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "guards" / "check_nuia_ratchet.py"
REASON = "test files are converted to checked index access in a later pass"


def _load():
    spec = importlib.util.spec_from_file_location("_guard_nuia_ratchet_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()


def diag(filename: str, kind: str = "ERROR", ts: int = 1) -> str:
    body = json.dumps(
        {
            "type": kind,
            "filename": filename,
            "message": "Object is possibly 'undefined'.",
            "code": 2532,
        }
    )
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


def test_parse_counts_errors_per_file_and_ignores_warnings() -> None:
    text = output(
        diag("src/a.ts"),
        diag("src/a.ts"),
        diag("src/B.svelte"),
        diag("src/c.ts", kind="WARNING"),
    )
    assert guard.parse(text) == {"frontend/src/a.ts": 2, "frontend/src/B.svelte": 1}


def test_parse_fails_closed_without_completed_line() -> None:
    with pytest.raises(guard.bl.ScanError):
        guard.parse(diag("src/a.ts"))


def test_new_unchecked_access_fails_and_baselined_passes(tmp_path: Path) -> None:
    text = output(diag("src/a.test.ts"))
    assert run(tmp_path, text, "") == 1
    assert run(tmp_path, text, f"frontend/src/a.test.ts\t1\t{REASON}\n") == 0


def test_clean_output_passes_with_empty_baseline(tmp_path: Path) -> None:
    assert run(tmp_path, output(), "") == 0


def test_count_rise_in_one_file_fails(tmp_path: Path) -> None:
    text = output(diag("src/a.test.ts"), diag("src/a.test.ts"))
    assert run(tmp_path, text, f"frontend/src/a.test.ts\t1\t{REASON}\n") == 1


def test_stale_baseline_entry_fails_and_tighten_drops_it(tmp_path: Path) -> None:
    baseline = f"frontend/src/a.test.ts\t1\t{REASON}\n"
    assert run(tmp_path, output(), baseline) == 1
    assert run(tmp_path, output(), baseline, "--tighten") == 0
    assert (tmp_path / "baseline.txt").read_text(encoding="utf-8") == ""
