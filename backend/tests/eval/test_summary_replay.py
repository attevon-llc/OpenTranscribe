"""``replay-merge``: per-arm isolation under ``--concurrency``, checkpointing, and resume."""

from __future__ import annotations

import importlib
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.eval.summary_bench_support import load_bench

SOURCES = [f"src{i}" for i in range(6)]
JOBS = [(s, a) for s in SOURCES for a in ("old", "new")]
RULES = {
    "old": (" S-OLD.", " M-OLD."),
    "new": (" S-NEW.", " M-NEW."),
}


def _stub(
    bench: Any, seen: list[tuple[str, str]], barrier: threading.Barrier | None = None
) -> tuple[Callable[[str, str], dict[str, Any]], Callable[[str], Any]]:
    module = importlib.import_module(bench.service._MODULE)
    lock = threading.Lock()

    def do_merge(source: str, arm: str) -> dict[str, Any]:
        if barrier is not None:
            barrier.wait()  # proves same-arm jobs really overlap
        observed = module.SECTION_MERGE_RULES  # what the merge call would have read
        with lock:
            seen.append((arm, observed))
        return {
            "source": source,
            "merge_arm": arm,
            "carry_recall": 0.5 if arm == "old" else 0.75,
            "observed_rules": observed,
        }

    def scope(arm: str):
        return bench.service.inject_arm(bench.arms.Arm(arm, *RULES[arm]))

    return do_merge, scope


@pytest.fixture(scope="module")
def bench():
    return load_bench()


def test_concurrent_merges_never_see_the_other_arms_rules(bench, tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []
    barrier = threading.Barrier(3, timeout=10)
    do_merge, scope = _stub(bench, seen, barrier)
    rows = bench.replay.replay_merges(JOBS, do_merge, scope, tmp_path / "r.jsonl", concurrency=3)
    assert len(rows) == len(JOBS)
    assert {(a, o) for a, o in seen} == {("old", " M-OLD."), ("new", " M-NEW.")}
    assert len(seen) == len(JOBS)


def test_concurrent_result_equals_serial_result(bench, tmp_path: Path) -> None:
    do_merge, scope = _stub(bench, [])
    serial = bench.replay.replay_merges(JOBS, do_merge, scope, tmp_path / "a.jsonl")
    parallel = bench.replay.replay_merges(
        JOBS, do_merge, scope, tmp_path / "b.jsonl", concurrency=4
    )
    assert parallel == serial
    assert [(r["source"], r["merge_arm"]) for r in serial] == JOBS


def test_rows_are_checkpointed_and_resume_skips_done(bench, tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    calls: list[tuple[str, str]] = []
    do_merge, scope = _stub(bench, [])

    def dies_on_fourth(source: str, arm: str) -> dict[str, Any]:
        if len(calls) == 3:
            raise RuntimeError("interrupted")
        calls.append((source, arm))
        return do_merge(source, arm)

    with pytest.raises(RuntimeError):
        bench.replay.replay_merges(JOBS, dies_on_fourth, scope, path)
    assert len(path.read_text().splitlines()) == 3  # kept, not lost until the end

    path.write_text(path.read_text() + '{"source": "torn')  # torn trailing write
    resumed: list[tuple[str, str]] = []

    def record(source: str, arm: str) -> dict[str, Any]:
        resumed.append((source, arm))
        return do_merge(source, arm)

    rows = bench.replay.replay_merges(JOBS, record, scope, path)
    assert sorted(resumed) == sorted(set(JOBS) - set(calls))
    assert [(r["source"], r["merge_arm"]) for r in rows] == JOBS
    assert all(json.loads(line) for line in path.read_text().splitlines()[:3])


def test_concurrency_below_one_is_refused(bench, tmp_path: Path) -> None:
    do_merge, scope = _stub(bench, [])
    with pytest.raises(bench.config.HarnessError):
        bench.replay.replay_merges(JOBS, do_merge, scope, tmp_path / "r.jsonl", concurrency=0)
