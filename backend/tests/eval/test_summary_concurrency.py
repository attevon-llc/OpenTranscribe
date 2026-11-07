"""``--concurrency`` for the summary-eval harness: overlap only inside one arm and run block."""

from __future__ import annotations

import dataclasses
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.eval.summary_bench_support import FakeTransport
from tests.eval.summary_bench_support import load_bench
from tests.eval.summary_bench_support import make_context as _context

INPUTS = ("IA", "IB", "IC")


@pytest.fixture(scope="module")
def bench() -> SimpleNamespace:
    return load_bench()


def _cells(bench: SimpleNamespace) -> list[Any]:
    cell = bench.runner.CellId
    return [cell(i, arm, 0) for arm in ("y", "x") for i in INPUTS]


def _ctx(bench: SimpleNamespace):
    ctx, entry = _context(bench, FakeTransport(), mode=None)
    spec = ctx.specs["IX"]
    for name in INPUTS:
        ctx.specs[name] = dataclasses.replace(spec, input_id=name)
        ctx.lock["inputs"][name] = entry
        ctx.rendered[name] = ctx.rendered["IX"]
    return ctx


class _Probe:
    """Wraps ``run_cell`` to see how many cells, and which arms, are in flight at once."""

    def __init__(
        self,
        bench: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        rendezvous: int | None = None,
    ) -> None:
        # With ``rendezvous`` set, a cell waits until that many are in flight together, so overlap
        # is proven by the barrier releasing, not hoped for with a sleep.
        barrier = threading.Barrier(rendezvous, timeout=10) if rendezvous else None
        self.lock = threading.Lock()
        self.inflight: list[str] = []
        self.max_inflight = 0
        self.max_arms = 0
        real = bench.runner.run_cell

        def wrapped(ctx: Any, cell: Any, **kwargs: Any) -> dict[str, Any]:
            with self.lock:
                self.inflight.append(cell.arm)
                self.max_inflight = max(self.max_inflight, len(self.inflight))
                self.max_arms = max(self.max_arms, len(set(self.inflight)))
            if barrier:
                barrier.wait()
            try:
                record: dict[str, Any] = real(ctx, cell, **kwargs)
                return record
            finally:
                with self.lock:
                    self.inflight.remove(cell.arm)

        monkeypatch.setattr(bench.runner, "run_cell", wrapped)


def _run(bench: SimpleNamespace, ctx: Any, cells: list[Any], path: Path, n: int) -> list[Any]:
    return list(bench.runner.run_cells(ctx, cells, path, lambda r: None, concurrency=n))


def _file_ids(path: Path) -> list[str]:
    return [json.loads(line)["cell_id"] for line in path.read_text().splitlines()]


def test_cells_of_one_arm_overlap_but_arms_never_do(
    bench: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _Probe(bench, monkeypatch, rendezvous=3)
    cells = _cells(bench)
    records = _run(bench, _ctx(bench), cells, tmp_path / "records.jsonl", 3)
    assert probe.max_inflight == 3
    assert probe.max_arms == 1
    assert all(r["status"] == "ok" for r in records)
    # The seeded arm order of the block holds: every y cell is written before any x cell.
    arms = [i.split("|")[1] for i in _file_ids(tmp_path / "records.jsonl")]
    assert arms == ["y"] * 3 + ["x"] * 3


def test_every_cell_is_recorded_exactly_once(bench: SimpleNamespace, tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    cells = _cells(bench)
    _run(bench, _ctx(bench), cells, path, 2)
    assert sorted(_file_ids(path)) == sorted(c.key for c in cells)


def test_concurrency_one_stays_strictly_sequential_and_in_plan_order(
    bench: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _Probe(bench, monkeypatch)
    cells = _cells(bench)
    _run(bench, _ctx(bench), cells, tmp_path / "records.jsonl", 1)
    assert probe.max_inflight == 1
    assert _file_ids(tmp_path / "records.jsonl") == [c.key for c in cells]


def test_resume_skips_cells_already_recorded(bench: SimpleNamespace, tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    cells = _cells(bench)
    ctx = _ctx(bench)
    _run(bench, ctx, cells[:4], path, 2)
    todo = bench.runner.pending_cells(
        cells, bench.runner.load_records(path), resume=True, retry_errors=False
    )
    assert [c.key for c in todo] == [c.key for c in cells[4:]]
    _run(bench, ctx, todo, path, 2)
    assert sorted(_file_ids(path)) == sorted(c.key for c in cells)


def test_a_concurrency_below_one_is_refused(bench: SimpleNamespace, tmp_path: Path) -> None:
    with pytest.raises(bench.config.HarnessError):
        _run(bench, _ctx(bench), _cells(bench), tmp_path / "records.jsonl", 0)


def test_runinfo_marks_latency_not_comparable_above_one(
    bench: SimpleNamespace, tmp_path: Path
) -> None:
    records = _run(bench, _ctx(bench), _cells(bench)[:2], tmp_path / "records.jsonl", 2)
    bench.cli._write_runinfo(tmp_path, records, 1.0, 2)
    session = json.loads((tmp_path / "runinfo.json").read_text())["sessions"][0]
    assert session["concurrency"] == 2
    assert session["latency_comparable"] is False
