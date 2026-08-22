"""The thread->loop bridge that carries trace events to the client (GH #514).

Everything here exists because the producer and the consumer are on different
threads: retrieval runs inside ``run_in_threadpool`` (plus ``legs.py``'s fan-out
pool), while only the event loop can write to the socket.

The properties under test are the ones a chat turn depends on — a trace is a
diagnostic, and a diagnostic that can block, lose events silently, or take down
the turn it describes is worse than no diagnostic at all.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.services.chat.trace import QueryStage
from app.services.chat.trace import TraceEvent
from app.services.chat.trace import emit
from app.services.chat.trace_stream import StreamingTraceRecorder
from app.services.chat.trace_stream import WireEvent
from app.services.chat.trace_stream import drain_available
from app.services.chat.trace_stream import trace_payload

pytestmark = pytest.mark.unit


def _event(count: int = 1) -> TraceEvent:
    return TraceEvent(stage=QueryStage.FOUND, detail={"count": count})


def _drain_sync(queue: asyncio.Queue) -> list[WireEvent]:
    out: list[WireEvent] = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


@pytest.mark.asyncio
async def test_events_from_many_threads_all_arrive_with_unique_sequence_numbers():
    """Proves the lock, not merely that nothing crashed.

    Eight threads recording concurrently must produce exactly 400 events with
    ``seq`` covering 1..400 once each. A missing lock shows up as a duplicated
    or skipped sequence number, which a bare count assertion would not catch.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[WireEvent] = asyncio.Queue()
    recorder = StreamingTraceRecorder(loop, queue, cap=1000)

    def _record_many() -> None:
        for _ in range(50):
            recorder.record(_event())

    threads = [threading.Thread(target=_record_many) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    await asyncio.sleep(0)  # let the scheduled callbacks run
    delivered = _drain_sync(queue)

    assert len(delivered) == 400
    assert sorted(w.seq for w in delivered) == list(range(1, 401))
    assert recorder.truncated is False


@pytest.mark.asyncio
async def test_the_cap_truncates_and_says_so():
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[WireEvent] = asyncio.Queue()
    recorder = StreamingTraceRecorder(loop, queue, cap=5)

    for _ in range(50):
        recorder.record(_event())
    await asyncio.sleep(0)

    assert len(_drain_sync(queue)) == 5
    assert recorder.truncated is True


@pytest.mark.asyncio
async def test_a_trace_within_the_cap_is_not_reported_as_truncated():
    """Control: truncation must not be always-on.

    Without this, ``truncated = True`` unconditionally would pass the test above
    and make the client's banner permanent and meaningless.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[WireEvent] = asyncio.Queue()
    recorder = StreamingTraceRecorder(loop, queue, cap=5)

    for _ in range(5):
        recorder.record(_event())
    await asyncio.sleep(0)

    assert len(_drain_sync(queue)) == 5
    assert recorder.truncated is False


@pytest.mark.asyncio
async def test_a_full_queue_drops_events_without_blocking_the_producer():
    """Backpressure must degrade the trace, never stall retrieval.

    A recorder that blocked when the consumer fell behind would make the
    diagnostic slow down the thing it is describing.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[WireEvent] = asyncio.Queue(maxsize=2)
    recorder = StreamingTraceRecorder(loop, queue, cap=1000)

    started = time.monotonic()
    for _ in range(200):
        recorder.record(_event())
        await asyncio.sleep(0)  # let each _enqueue run, so the queue really fills
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"recording blocked for {elapsed:.2f}s against a full queue"
    assert queue.qsize() == 2, "the queue must not grow past its maxsize"
    assert recorder.truncated is True, "dropped events must be reported, not silent"


@pytest.mark.asyncio
async def test_a_closed_loop_does_not_propagate_to_the_caller():
    """The shutdown race, and the one case ``emit``'s own guard cannot cover.

    A retrieval thread can still be alive when the loop closes.
    ``call_soon_threadsafe`` raises ``RuntimeError`` there — on the CALLING
    thread, inside ``record`` — and a turn must not die of it.
    """
    dead_loop = asyncio.new_event_loop()
    dead_loop.close()
    queue: asyncio.Queue[WireEvent] = asyncio.Queue()
    recorder = StreamingTraceRecorder(dead_loop, queue, cap=10)

    recorder.record(_event())  # must not raise

    assert queue.empty(), "nothing can be delivered through a closed loop"


@pytest.mark.asyncio
async def test_a_broken_queue_cannot_break_the_turn():
    """``_enqueue`` runs later, on the loop thread, past ``emit``'s try/except."""

    class _ExplodingQueue(asyncio.Queue):
        def put_nowait(self, item):
            raise RuntimeError("queue is broken")

    loop = asyncio.get_running_loop()
    recorder = StreamingTraceRecorder(loop, _ExplodingQueue(), cap=10)

    recorder.record(_event())
    await asyncio.sleep(0)  # the callback runs here; an unguarded raise surfaces now

    assert recorder.truncated is False, "a broken queue is not truncation"


@pytest.mark.asyncio
async def test_emit_accepts_the_streaming_recorder_like_any_other():
    """It must satisfy the same protocol every pipeline call site already uses."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[WireEvent] = asyncio.Queue()
    recorder = StreamingTraceRecorder(loop, queue)

    emit(recorder, QueryStage.SAMPLED, kept=12, dropped=36, limit=4)
    await asyncio.sleep(0)

    delivered = _drain_sync(queue)
    assert len(delivered) == 1
    payload = trace_payload(delivered[0])
    assert payload["stage"] == "sampled"
    assert payload["detail"] == {"kept": 12, "dropped": 36, "limit": 4}


@pytest.mark.asyncio
async def test_the_payload_nests_detail_rather_than_spreading_it():
    """A detail key must never be able to collide with a frame-level key.

    Spreading ``detail`` flat would put allowlisted keys in the same namespace
    as ``stage``/``seq``/``node_id``, so widening the allowlist later could
    silently shadow one of them.
    """
    wire = WireEvent(seq=7, event=TraceEvent(stage=QueryStage.FOUND, detail={"count": 3}))

    payload = trace_payload(wire)

    assert payload["detail"] == {"count": 3}
    assert "count" not in {k for k in payload if k != "detail"}
    assert payload["seq"] == 7


@pytest.mark.asyncio
async def test_drain_available_returns_what_is_queued_and_never_waits():
    """The B3 flush. It must also be a no-op without a recorder attached."""
    queue: asyncio.Queue[WireEvent] = asyncio.Queue()
    queue.put_nowait(WireEvent(seq=1, event=_event(count=2)))

    payloads = await drain_available(queue)
    assert [p["seq"] for p in payloads] == [1]

    # Nothing left, and no hang: the queue is empty and nothing will refill it.
    assert await drain_available(queue) == []
    assert await drain_available(None) == [], "flag-off turns pass None here"
