"""Tests for the noUncheckedIndexedAccess ratchet in ``scripts/guards/check_nuia_ratchet.py``."""

from __future__ import annotations

import json
import subprocess
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


def test_unused_symbol_errors_are_not_counted_as_nuia() -> None:
    # The shared tsconfig adds noUnused*, whose errors the unused-symbol ratchet owns.
    text = output(
        *(diagnostic("src/a.ts", code, "ERROR", 1) for code in sorted(guard.sc.UNUSED_CODES))
    )
    assert guard.parse(text) == {}


def test_shared_tsconfig_is_the_union_of_the_three_flags() -> None:
    root = Path(__file__).resolve().parents[3] / "frontend"
    config = json.loads((root / guard.sc.TSCONFIG).read_text(encoding="utf-8"))
    assert config["extends"] == "./tsconfig.json"
    assert config["compilerOptions"] == {
        "noUnusedLocals": True,
        "noUnusedParameters": True,
        "noUncheckedIndexedAccess": True,
    }
    assert not (root / "tsconfig.unused.json").exists()
    assert not (root / "tsconfig.nuia.json").exists()


def test_second_ratchet_replays_the_first_run_until_the_tree_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sc = guard.sc
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    runs: list[int] = []

    def fake(_frontend: Path, _tsconfig: str, _timeout: int) -> str:
        runs.append(1)
        return output()

    monkeypatch.setattr(sc, "_run_uncached", fake)
    sc._run(tmp_path, sc.TSCONFIG, 5)
    sc._run(tmp_path, sc.TSCONFIG, 5)
    assert len(runs) == 1  # the second ratchet reused the first one's output
    (frontend / "a.ts").write_text("export const a = 2\n", encoding="utf-8")
    sc._run(tmp_path, sc.TSCONFIG, 5)
    assert len(runs) == 2  # a content change is a cache miss, never a stale replay
    assert len(list((frontend / ".cache" / "svelte-check").glob("*.txt"))) == 1
