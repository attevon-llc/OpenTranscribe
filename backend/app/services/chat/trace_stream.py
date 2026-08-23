"""Bridge trace events from worker threads onto the turn's event loop (GH #514).

``trace.py`` holds the vocabulary and is deliberately stdlib-light — importing it
costs nothing. This module holds the *transport*, which needs asyncio and
threading, and keeping the two apart means a caller that only wants the stage
enum does not drag a queue implementation in with it.

**Why a bridge is needed at all.** ``stream_reply`` runs the whole retrieval
phase inside one ``run_in_threadpool`` call, so the async generator is blocked
for its duration and cannot yield. Trace events are produced *inside* that call
— on the threadpool worker, and on ``legs.py``'s fan-out threads — while the only
thing able to write to the socket is the loop. ``loop.call_soon_threadsafe`` is
the documented hand-off for exactly that, and it never blocks the caller.

**Not Redis, and the reason is worth recording.** The issue specifies Redis
pub/sub, which predates knowing the pipeline's shape: retrieval runs in the same
process and the same request as the SSE generator, so Redis would add a network
hop and a lost-wakeup race to cross a boundary that does not exist. If a
retrieval stage ever moves to Celery, the templates to copy are
``endpoints/files/__init__.py``'s ``download_stream`` and
``endpoints/files/subtitles.py``'s ``bulk_export_stream``, both of which
subscribe properly with the check -> subscribe -> re-check ordering.

**Live-only.** Nothing here is persisted. A trace exists for the turn that
produced it and is gone on reload — measured trade: a typical trace is 1.5-2.5 KB
against a whole chat message row of ~1.5 KB, so storing it would roughly double
every conversation-load payload for diagnostics shown one turn at a time.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any

from app.services.chat.trace import TraceEvent

logger = logging.getLogger(__name__)

#: Matches ``ListTraceRecorder``'s default. Far above any real turn — a fully
#: fanned-out turn produces roughly 22 events — so reaching it means something
#: is looping, and the client is told rather than shown a silently short tree.
DEFAULT_TRACE_CAP = 200

#: Bound on how far the consumer may fall behind. Distinct from the cap above:
#: that one protects memory when nobody drains at all, this one protects against
#: a drain that is merely slower than the producer.
DEFAULT_QUEUE_MAXSIZE = 512


@dataclass(frozen=True)
class WireEvent:
    """One trace event, stamped with its delivery order."""

    seq: int
    event: TraceEvent


def trace_payload(wire: WireEvent) -> dict[str, Any]:
    """The JSON body of one ``trace`` SSE frame.

    ``detail`` stays a NESTED object rather than being spread flat. That keeps
    the wire shape and ``SAFE_DETAIL_KEYS`` identical, and makes it structurally
    impossible for a future detail key to collide with ``stage``/``outcome``/
    ``parent``/``node_id``/``seq``.
    """
    return {
        "seq": wire.seq,
        "stage": wire.event.stage.value,
        "outcome": wire.event.outcome.value,
        "parent": wire.event.parent,
        "node_id": wire.event.node_id,
        "detail": dict(wire.event.detail),
    }


class StreamingTraceRecorder:
    """A ``TraceRecorder`` that hands events to an asyncio queue, thread-safely.

    Satisfies the ``TraceRecorder`` protocol in ``trace.py``. Four properties,
    each of which the turn depends on:

    - **Never blocks the caller.** ``call_soon_threadsafe`` schedules and
      returns. A retrieval worker is never made to wait on the SSE consumer.
    - **Thread-safe.** A lock guards the counter, the cap and the truncation
      flag. Today every ``legs.py`` emit happens on the single thread that calls
      ``run_legs`` — ``_run_leg`` itself emits nothing — so the lock is currently
      defensive rather than load-bearing. It becomes load-bearing the moment an
      emit moves inside a leg, which is a one-line change someone will make.
    - **Doubly bounded**, by ``cap`` and by the queue's own ``maxsize``. Both set
      the same ``truncated`` flag, so one boolean describes either.
    - **Cannot fail a turn.** ``trace.emit`` already wraps ``record``, but
      ``_enqueue`` runs LATER, on the loop thread, where that wrapper cannot
      reach — so this guards itself as well.

    Chosen over ``queue.SimpleQueue`` because its ``get`` has no async form,
    which would force the consumer to poll on a timer and wake up for nothing.
    """

    __slots__ = ("_cap", "_count", "_lock", "_loop", "_queue", "_truncated")

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[WireEvent],
        cap: int = DEFAULT_TRACE_CAP,
    ) -> None:
        self._loop = loop
        self._queue = queue
        self._cap = cap
        self._lock = threading.Lock()
        self._count = 0
        self._truncated = False

    @property
    def truncated(self) -> bool:
        """True once anything was dropped, so a short tree is never silent."""
        with self._lock:
            return self._truncated

    def record(self, event: TraceEvent) -> None:
        """Queue one event for delivery. Never raises, never blocks."""
        try:
            with self._lock:
                if self._count >= self._cap:
                    self._truncated = True
                    return
                self._count += 1
                seq = self._count
            # Raises RuntimeError if the loop has already closed — a real race
            # during process shutdown, while a retrieval thread is still alive.
            self._loop.call_soon_threadsafe(self._enqueue, WireEvent(seq, event))
        except Exception:  # noqa: BLE001 — a trace failure must never fail a turn
            logger.debug("query-trace event could not be scheduled", exc_info=True)

    def _enqueue(self, wire: WireEvent) -> None:
        """Runs ON THE LOOP THREAD, scheduled by :meth:`record`."""
        try:
            self._queue.put_nowait(wire)
        except asyncio.QueueFull:
            with self._lock:
                self._truncated = True
        except Exception:  # noqa: BLE001 — see the class docstring
            logger.debug("query-trace event could not be queued", exc_info=True)


async def drain_available(queue: asyncio.Queue[WireEvent] | None) -> list[dict[str, Any]]:
    """Every event currently queued, without waiting for more.

    ⚠️ **Call this at every point that yields after the retrieval drain ends.**
    ``PRESENTED`` — the last and most meaningful stage — is emitted after
    ``build_messages``, which runs past the drain loop; without a flush there it
    is queued and never delivered, and with a live-only trace there is no
    persistence to paper over the gap.

    Never awaits ``get()``: past the retrieval phase nothing guarantees another
    event will ever arrive, so a blocking read could hang the turn.

    ⚠️ **The ``sleep(0)`` is load-bearing, not politeness.** ``record`` hands off
    via ``call_soon_threadsafe``, which SCHEDULES ``_enqueue`` rather than running
    it — even when the caller is already on the loop thread. Draining without
    first yielding therefore inspects a queue the pending callbacks have not
    reached yet and finds it empty. The live drain during retrieval hid this,
    because ``asyncio.wait`` yields anyway; the post-retrieval flush does not,
    so ``BUDGETED`` and ``PRESENTED`` were emitted, queued, and never delivered
    — silently, with no error, exactly the class of failure this panel exists to
    expose.
    """
    if queue is None:
        return []
    await asyncio.sleep(0)
    payloads: list[dict[str, Any]] = []
    while not queue.empty():
        try:
            payloads.append(trace_payload(queue.get_nowait()))
        except asyncio.QueueEmpty:  # pragma: no cover — guarded by `empty()`
            break
    return payloads
