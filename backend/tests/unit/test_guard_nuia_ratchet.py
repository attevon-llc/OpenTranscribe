"""Tests for the noUncheckedIndexedAccess ratchet in ``scripts/guards/check_nuia_ratchet.py``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit._svelte_check_guard_harness import diagnostic
from tests.unit._svelte_check_guard_harness import load_guard
from tests.unit._svelte_check_guard_harness import output
from tests.unit._svelte_check_guard_harness import run_guard

REASON = "test files are converted to checked index access in a later pass"
guard = load_guard("check_nuia_ratchet")


def diag(filename: str, kind: str = "ERROR", ts: int = 1) -> str:
    return diagnostic(filename, 2532, kind, ts)


def run(tmp_path: Path, text: str, baseline: str, *extra: str) -> int:
    return run_guard(guard, tmp_path, text, baseline, *extra)


def test_parse_counts_errors_per_file_and_ignores_warnings() -> None:
    text = output(
        diag("src/a.ts"),
        diag("src/a.ts"),
        diag("src/B.svelte"),
        diag("src/c.ts", kind="WARNING"),
    )
    assert guard.parse(text) == {"frontend/src/a.ts": 2, "frontend/src/B.svelte": 1}


def test_parse_fails_closed_without_completed_line() -> None:
    with pytest.raises(guard.sc.bl.ScanError):
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
