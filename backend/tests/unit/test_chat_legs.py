"""Tests for parallel leg execution (#403 W2.6): merge, dedup, degrade, cancel.

Nothing here needs Postgres/Redis/OpenSearch — `legs.py` itself makes no I/O;
every leg in these tests is a plain Python callable.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.services.chat import legs
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_executor():
    """Each test gets its own executor so tests cannot see each other's threads."""
    legs.reset_executor_for_tests()
    yield
    legs.reset_executor_for_tests()


def _hit(uuid: str, index: int, score: float = 1.0) -> ChunkHit:
    return ChunkHit(file_uuid=uuid, file_id=1, chunk_index=index, content=f"c{index}", score=score)


# --------------------------------------------------------------------------- merge / dedup


def test_chunk_legs_merge_into_one_pool():
    a = legs.Leg(kind=legs.LEG_MAIN, name="main", run=lambda: [_hit("f1", 0), _hit("f1", 1)])
    b = legs.Leg(kind=legs.LEG_SUBQUESTION, name="subquestion-0", run=lambda: [_hit("f2", 0)])
    result = legs.run_legs([a, b])
    keys = {(h.file_uuid, h.chunk_index) for h in result.chunk_hits}
    assert keys == {("f1", 0), ("f1", 1), ("f2", 0)}


def test_duplicate_chunks_are_deduped_keeping_the_higher_score():
    a = legs.Leg(kind=legs.LEG_MAIN, name="main", run=lambda: [_hit("f1", 0, score=0.4)])
    b = legs.Leg(
        kind=legs.LEG_SUBQUESTION, name="subquestion-0", run=lambda: [_hit("f1", 0, score=0.9)]
    )
    result = legs.run_legs([a, b])
    assert len(result.chunk_hits) == 1
    assert result.chunk_hits[0].score == 0.9


def test_non_chunk_legs_are_never_merged_into_the_chunk_pool():
    """Only chunk-kind legs are merged — counted/recurrence stay separate."""
    chunk_leg = legs.Leg(kind=legs.LEG_MAIN, name="main", run=lambda: [_hit("f1", 0)])
    counted_leg = legs.Leg(kind=legs.LEG_COUNTED, name="counted", run=lambda: {"count": 3})
    result = legs.run_legs([chunk_leg, counted_leg])
    assert len(result.chunk_hits) == 1
    assert result.other["counted"].result == {"count": 3}
    assert "counted" not in {(h.file_uuid) for h in result.chunk_hits}


def test_chunk_counts_by_leg_survives_the_merge():
    """The merge destroys per-leg attribution for `chunk_hits`; this doesn't."""
    a = legs.Leg(kind=legs.LEG_MAIN, name="main", run=lambda: [_hit("f1", 0), _hit("f1", 1)])
    b = legs.Leg(kind=legs.LEG_SUBQUESTION, name="subquestion-0", run=list)
    result = legs.run_legs([a, b])
    assert result.chunk_counts_by_leg == {"main": 2, "subquestion-0": 0}


# --------------------------------------------------------------------------- degradation


def test_a_failing_leg_is_recorded_and_contributes_nothing():
    def _boom():
        raise RuntimeError("leg exploded")

    ok = legs.Leg(kind=legs.LEG_MAIN, name="main", run=lambda: [_hit("f1", 0)])
    bad = legs.Leg(kind=legs.LEG_SUBQUESTION, name="subquestion-0", run=_boom)
    result = legs.run_legs([ok, bad])
    assert result.failed == ["subquestion-0"]
    assert len(result.chunk_hits) == 1
    assert result.as_metadata()["legs_failed"] == ["subquestion-0"]


def test_a_timed_out_leg_is_recorded_as_failed():
    def _slow():
        time.sleep(0.5)
        return [_hit("f1", 0)]

    leg = legs.Leg(kind=legs.LEG_MAIN, name="main", run=_slow)
    result = legs.run_legs([leg], timeout_seconds=0.01)
    assert result.failed == ["main"]
    assert result.chunk_hits == []


def test_empty_leg_list_returns_an_empty_result():
    result = legs.run_legs([])
    assert result.chunk_hits == []
    assert result.other == {}
    assert result.failed == []


def test_a_turn_with_the_flag_off_is_never_worse_than_a_single_leg():
    """A failed leg degrades a fan-out turn; it never makes it WORSE than the
    single-leg pipeline would have answered — the main leg's own hits are
    unaffected by a sibling leg failing."""
    main = legs.Leg(kind=legs.LEG_MAIN, name="main", run=lambda: [_hit("f1", 0), _hit("f1", 1)])

    def _boom():
        raise RuntimeError("boom")

    bad = legs.Leg(kind=legs.LEG_SPEAKER, name="speaker", run=_boom)
    result = legs.run_legs([main, bad])
    assert len(result.chunk_hits) == 2


# --------------------------------------------------------------------------- cancellation


def test_cancel_check_stops_new_submissions_between_legs():
    """'Between leg submissions' — a leg already running is not interrupted,
    but no NEW leg is submitted once the flag is seen."""
    submitted: list[str] = []
    cancel_after_first = {"seen": 0}

    def _cancel_check() -> bool:
        return cancel_after_first["seen"] >= 1

    def _make(name):
        def _run():
            submitted.append(name)
            cancel_after_first["seen"] += 1
            return [_hit("f1", 0)]

        return legs.Leg(kind=legs.LEG_MAIN, name=name, run=_run)

    result = legs.run_legs(
        [_make("leg-0"), _make("leg-1"), _make("leg-2")], cancel_check=_cancel_check
    )
    # leg-0 always submits (checked BEFORE it, cancel flag starts False); at
    # least one later leg must have been skipped rather than submitted.
    assert "leg-0" in submitted
    assert len(submitted) < 3
    assert len(result.failed) >= 1


def test_cancel_check_before_any_leg_submits_none():
    leg = legs.Leg(kind=legs.LEG_MAIN, name="main", run=lambda: [_hit("f1", 0)])
    result = legs.run_legs([leg], cancel_check=lambda: True)
    assert result.chunk_hits == []
    assert result.failed == ["main"]


# --------------------------------------------------------------------------- the executor itself


def test_get_executor_is_a_process_wide_singleton():
    """ONE executor, not per-turn — a per-turn pool multiplies threads by users."""
    first = legs.get_executor()
    second = legs.get_executor()
    assert first is second


def test_get_executor_is_thread_safe_under_concurrent_first_use():
    legs.reset_executor_for_tests()
    seen: list[object] = []
    barrier = threading.Barrier(8)

    def _grab():
        barrier.wait()
        seen.append(legs.get_executor())

    threads = [threading.Thread(target=_grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(id(e) for e in seen)) == 1


def test_a_leg_runs_on_the_shared_executor_not_synchronously():
    """Sanity check that legs actually execute concurrently, not one at a time."""
    start = time.monotonic()

    def _sleepy():
        time.sleep(0.15)
        return [_hit("f1", 0)]

    built = [legs.Leg(kind=legs.LEG_MAIN, name=f"leg-{i}", run=_sleepy) for i in range(4)]
    legs.run_legs(built, max_workers=4)
    elapsed = time.monotonic() - start
    # Serial would take >= 0.6s; concurrent should comfortably finish well
    # under that. Generous bound to avoid flaking under CI load.
    assert elapsed < 0.5
