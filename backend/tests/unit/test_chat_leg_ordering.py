"""A fan-out leg must report when IT finishes, not when its turn comes round.

``run_legs`` dispatches every leg to a shared thread pool — genuinely parallel —
and then collected the results by iterating ``futures.items()``, which is
**submission order**, calling the **blocking** ``future.result()`` on each. The
``FOUND`` trace event for a leg is emitted only inside that loop.

So a leg submitted first that takes 40 s held back the ``FOUND`` event of a leg
submitted second that finished in 200 ms. The retrieval *results* were unaffected
— every leg is awaited either way, which is why this never surfaced as a bug —
but the reported order was submission order wearing completion order's clothes.

That matters for GH #514, whose whole visual payoff is parallel legs resolving as
they actually resolve. A panel fed by the old loop would animate a lie about
concurrency the pipeline genuinely has.

The timeout tests below are here because the fix changes what the timeout MEANS,
and that is the part most likely to regress silently.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from app.services.chat import legs as legs_mod
from app.services.chat.legs import CHUNK_LEG_KINDS
from app.services.chat.legs import LEG_MAIN
from app.services.chat.legs import LEG_SUBQUESTION
from app.services.chat.legs import Leg
from app.services.chat.trace import ListTraceRecorder
from app.services.chat.trace import Outcome
from app.services.chat.trace import QueryStage
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit

#: Ceiling on how long a deliberately-wedged leg blocks. It is never reached: the
#: fixture releases every wedged leg at teardown. It exists only so a bug in this
#: file cannot hang the suite.
WEDGE_CEILING_SECONDS = 5.0


def _hit(index: int) -> ChunkHit:
    return ChunkHit(
        file_uuid=f"11111111-1111-1111-1111-00000000000{index}",
        file_id=index,
        chunk_index=index,
        content=f"content {index}",
        title="Recording",
        speaker="Dana",
        start_time=float(index),
        end_time=float(index) + 5.0,
    )


@pytest.fixture(autouse=True)
def release():
    """A pool wide enough for real parallelism, and a way to free wedged legs.

    Two things this has to get right:

    - The executor is a **process-wide singleton** whose ``max_workers`` is fixed
      by whichever caller built it first, so a test inheriting a 1-worker pool
      would serialise the legs and pass regardless of collection order.
    - ``reset_executor_for_tests`` calls ``shutdown(cancel_futures=True)``, which
      **cannot cancel an already-running future**. A leg blocking on a bare
      ``time.sleep(5)`` therefore keeps occupying a worker in that shared pool for
      five seconds after its test has finished — bleeding into whatever runs next.
      Every wedged leg blocks on this event instead, and setting it here frees
      them the moment the test ends.
    """
    gate = threading.Event()
    legs_mod.reset_executor_for_tests()
    legs_mod.get_executor(4)
    try:
        yield gate
    finally:
        gate.set()
        legs_mod.reset_executor_for_tests()


def _returning(index: int) -> Callable[[], list[ChunkHit]]:
    """A leg body that returns one hit, as a named closure rather than a lambda.

    ``lambda i=i: [_hit(i)]`` captures correctly but is untypeable — the
    default-argument idiom defeats inference.
    """

    def _run() -> list[ChunkHit]:
        return [_hit(index)]

    return _run


def _found_order(recorder: ListTraceRecorder) -> list[str]:
    """The node ids of every ``FOUND`` event, in emission order.

    Asserts each one names a node instead of filtering the anonymous ones out: a
    ``FOUND`` with no ``node_id`` is unattributable, and dropping it silently
    would hide exactly that.
    """
    ids: list[str] = []
    for event in recorder.events:
        if event.stage is not QueryStage.FOUND:
            continue
        assert event.node_id is not None, "a leg's FOUND event must name its node"
        ids.append(event.node_id)
    return ids


def test_a_fast_leg_reports_before_a_slow_leg_submitted_earlier():
    """The bug, as an ordering fact rather than a result-set fact.

    Both legs succeed on the old code too, so any assertion about *what* was
    found passes either way. Only the order distinguishes them.

    The two legs **handshake** rather than racing on a sleep: the slow leg is
    submitted first and blocks until the fast leg has actually returned. So the
    completion order is a fact of the test, not a timing hope — there is no
    duration to tune and no jitter window to lose on a loaded machine.
    """
    recorder = ListTraceRecorder()
    fast_returned = threading.Event()

    def _slow():
        # Submitted FIRST, finishes SECOND, by construction.
        assert fast_returned.wait(WEDGE_CEILING_SECONDS), (
            "the fast leg never ran — the pool is too small for real parallelism"
        )
        return [_hit(0)]

    def _fast():
        try:
            return [_hit(1)]
        finally:
            fast_returned.set()

    built = [
        Leg(kind=LEG_MAIN, name="slow", run=_slow),
        Leg(kind=LEG_SUBQUESTION, name="fast", run=_fast),
    ]

    outcome = legs_mod.run_legs(built, max_workers=4, recorder=recorder, parent="plan")

    assert _found_order(recorder) == ["fast", "slow"], (
        "legs reported in submission order: the leg that provably finished first "
        "was reported second, so the panel would animate a sequence the pipeline "
        "never ran in"
    )
    # Control: the ordering fix must not change what the fan-out actually returns.
    assert len(outcome.chunk_hits) == 2, "both legs' hits must still reach the merged pool"
    assert not outcome.failed, f"neither leg should have failed, got {outcome.failed}"


def test_every_leg_still_reports_exactly_once():
    """Guard against the fix double-counting or dropping a leg.

    ``as_completed`` yields each future once; a hand-rolled drain that also kept
    the original loop would emit two ``FOUND`` events for the same node.
    """
    recorder = ListTraceRecorder()
    built = [Leg(kind=LEG_MAIN, name=f"leg-{i}", run=_returning(i)) for i in range(4)]

    legs_mod.run_legs(built, max_workers=4, recorder=recorder, parent="plan")

    found = _found_order(recorder)
    assert sorted(found) == ["leg-0", "leg-1", "leg-2", "leg-3"], (
        f"expected exactly one FOUND per leg, got {found}"
    )


def test_a_leg_past_the_deadline_fails_while_the_others_still_report(release):
    """The timeout must still fire, and must not take the healthy legs with it.

    This is the assertion that stops the ordering fix from quietly turning a
    per-leg timeout into something that either never fires or fails the whole
    fan-out.
    """
    recorder = ListTraceRecorder()

    def _wedged():
        release.wait(WEDGE_CEILING_SECONDS)
        return [_hit(9)]

    built = [
        Leg(kind=LEG_MAIN, name="wedged", run=_wedged),
        Leg(kind=LEG_SUBQUESTION, name="healthy", run=lambda: [_hit(1)]),
    ]

    outcome = legs_mod.run_legs(
        built,
        max_workers=4,
        timeout_seconds=0.3,
        recorder=recorder,
        parent="plan",
    )

    assert outcome.failed == ["wedged"], (
        f"the wedged leg should be the only failure, got {outcome.failed}"
    )
    assert len(outcome.chunk_hits) == 1, "the healthy leg's hits must survive the timeout"

    by_node = {e.node_id: e for e in recorder.events if e.stage is QueryStage.FOUND}
    assert by_node["wedged"].outcome is Outcome.FAILED
    assert by_node["wedged"].detail.get("reason") == "timeout"
    assert by_node["healthy"].outcome is Outcome.OK, (
        "a sibling timing out must not be reported as this leg's own failure"
    )


def test_the_whole_fanout_is_bounded_not_each_leg_separately(release):
    """``DEFAULT_LEG_TIMEOUT_SECONDS`` documents a ceiling on the WHOLE fan-out.

    The old loop applied it per future, so N wedged legs waited N x timeout — the
    constant's own docstring says "how long the caller waits for the WHOLE
    fan-out", and the code did not implement that.

    ⚠️ **The threshold has to discriminate.** Four wedged legs at a 0.3 s bound
    cost ~1.2 s under the old per-future shape and ~0.3 s under a single
    deadline, so the bound must sit between them. An earlier draft of this test
    allowed 1.5 s and therefore **passed on the broken code** — it measured
    nothing. Keep this comfortably below N x timeout.
    """
    recorder = ListTraceRecorder()
    legs_count = 4
    bound = 0.3

    def _wedged():
        release.wait(WEDGE_CEILING_SECONDS)
        return []

    built = [Leg(kind=LEG_MAIN, name=f"wedged-{i}", run=_wedged) for i in range(legs_count)]

    started = time.monotonic()
    outcome = legs_mod.run_legs(
        built,
        max_workers=4,
        timeout_seconds=bound,
        recorder=recorder,
        parent="plan",
    )
    elapsed = time.monotonic() - started

    assert sorted(outcome.failed) == [f"wedged-{i}" for i in range(legs_count)]
    ceiling = bound * 2  # generous for jitter, still far under legs_count * bound
    assert elapsed < ceiling, (
        f"{legs_count} wedged legs at a {bound}s whole-fan-out bound took "
        f"{elapsed:.2f}s (ceiling {ceiling:.2f}s); "
        f"~{legs_count * bound:.1f}s is the per-leg-timeout shape rather than one deadline"
    )


def test_chunk_leg_kinds_still_merge_and_others_stay_unmerged():
    """Control for the collection rewrite: the merge rules are unchanged.

    ``as_completed`` changes the ORDER results are collected in, and the branch
    that decides merged-vs-unmerged sits inside that same loop — so it is exactly
    what a careless rewrite would disturb.
    """
    recorder = ListTraceRecorder()
    built = [
        Leg(kind=LEG_MAIN, name="chunky", run=lambda: [_hit(1), _hit(2)]),
        Leg(kind="counted", name="counted", run=lambda: {"count": 7}),
    ]

    outcome = legs_mod.run_legs(built, max_workers=4, recorder=recorder, parent="plan")

    assert LEG_MAIN in CHUNK_LEG_KINDS, "precondition: main is a chunk kind"
    assert len(outcome.chunk_hits) == 2, "chunk-kind hits merge into the pool"
    assert "counted" in outcome.other, "a non-chunk leg stays unmerged in `other`"
    assert outcome.other["counted"].result == {"count": 7}
    assert outcome.chunk_counts_by_leg["chunky"] == 2
