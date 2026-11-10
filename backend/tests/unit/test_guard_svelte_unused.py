"""Tests for the .svelte unused-symbol ratchet in ``scripts/guards/check_svelte_unused.py``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit._svelte_check_guard_harness import diagnostic
from tests.unit._svelte_check_guard_harness import load_guard
from tests.unit._svelte_check_guard_harness import output
from tests.unit._svelte_check_guard_harness import run_guard

REASON = "pre-existing unused locals in svelte files, removed as each file is touched"
guard = load_guard("check_svelte_unused")


def diag(filename: str, code: int, ts: int = 1) -> str:
    return diagnostic(filename, code, "ERROR", ts)


def run(tmp_path: Path, text: str, baseline: str, *extra: str) -> int:
    return run_guard(guard, tmp_path, text, baseline, *extra)


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
    with pytest.raises(guard.sc.bl.ScanError):
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
