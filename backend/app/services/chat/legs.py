"""Parallel leg execution for planner-driven fan-out (#403 W2.6).

**ONE process-wide bounded executor, never per-turn.** A pool created inside
one turn multiplies thread count by concurrent users — the exact resource
leak a bounded pool exists to prevent. :func:`get_executor` is a lazy
singleton, sized once at first use from ``chat.planner.max_parallel_legs``,
matching the trade every other lazy singleton in this package makes
(``reranker.py``'s cross-encoder): a later settings change takes a process
restart, not a resize.

**Each leg owns its own SHORT session.** This package's rule — see
``services/chat/CLAUDE.md``'s "a turn holds NO database session while it
talks to OpenSearch or an LLM" — applies per LEG here, not just per turn: a
fan-out multiplies any violation of it by leg count, so a leg that opened a
session and then made an OpenSearch/LLM call inside it would hold N
transactions concurrently instead of one serial one. A chunk leg
(``retrieve_chunks``) needs no session at all — it is pure OpenSearch. A
leg backed by Postgres (counted, recurrence) is handed a **session
FACTORY**, exactly like ``aggregation_service.answer_aggregation`` and
``answer_recurrence`` already are, and opens/closes its own short session
internally; nothing here ever hands a leg an open ``Session``.

**Merge is chunk-legs only.** Non-chunk legs (counted, recurrence) are
returned unmerged in ``FanOutResult.other`` — the caller decides what to do
with each, exactly like the single-leg pipeline already does for the counted
tier. Chunk legs are unioned, deduped on ``(file_uuid, chunk_index)``
keep-max, and left for the caller to rerank ONCE against the original
question at the normal budget — reranking inside this module would rerank
each leg's output separately, against whatever sub-query produced it, which
is not the same ranking problem.

A failed leg degrades: it is recorded in ``FanOutResult.failed`` and
contributes nothing, matching how a single-leg retrieval already degrades to
``[]`` on failure elsewhere in this package. A turn's answer is never worse
than the flag being off — it is at most missing the extra legs.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import as_completed
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from app.services.chat.trace import Outcome
from app.services.chat.trace import QueryStage
from app.services.chat.trace import TraceRecorder
from app.services.chat.trace import emit
from app.services.search.chunk_retrieval import ChunkHit

logger = logging.getLogger(__name__)

DEFAULT_MAX_PARALLEL_LEGS = 4
#: Ceiling on how long the caller waits for the WHOLE fan-out. A turn is
#: already bounded by the first-token watchdog
#: (``DEFAULT_CHAT_FIRST_TOKEN_TIMEOUT_S``, 90s); this is a second, tighter
#: bound specifically on the fan-out phase so one wedged leg cannot silently
#: consume the whole watchdog budget before the caller even starts
#: generation.
DEFAULT_LEG_TIMEOUT_SECONDS = 45.0

LEG_MAIN = "main"
LEG_SUBQUESTION = "subquestion"
LEG_SPEAKER = "speaker"
LEG_COUNTED = "counted"
LEG_RECURRENCE = "recurrence"
LEG_MAP = "map"

#: Leg kinds merged into one chunk pool. Everything else is returned
#: unmerged — see the module docstring.
CHUNK_LEG_KINDS: frozenset[str] = frozenset({LEG_MAIN, LEG_SUBQUESTION, LEG_SPEAKER})

_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def get_executor(max_workers: int = DEFAULT_MAX_PARALLEL_LEGS) -> ThreadPoolExecutor:
    """The process-wide bounded executor every turn's fan-out shares.

    Double-checked locking so concurrent first callers do not each build one
    and leak the losers. ``max_workers`` on a call after the first has NO
    effect on an already-created pool — see the module docstring.
    """
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=max(1, max_workers), thread_name_prefix="chat-leg"
                )
    return _executor


def reset_executor_for_tests() -> None:
    """Drop the singleton so a test can rebuild it with a different size.

    Never called from production code. Shutting down with ``wait=False``:
    tests that use this are expected to have already drained any futures
    they cared about.
    """
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None


@dataclass(frozen=True)
class Leg:
    """One unit of fan-out work.

    ``run`` takes NO arguments and returns its own result — a leg owns any DB
    session or client it needs INTERNALLY (see the module docstring) rather
    than being handed one, so the executor never becomes a place a session
    could leak across threads. ``kind`` selects the merge behaviour
    (:data:`CHUNK_LEG_KINDS` are unioned into one pool; anything else lands
    in ``FanOutResult.other`` under ``name``, unmerged).
    """

    kind: str
    name: str
    run: Callable[[], Any]


@dataclass
class LegOutcome:
    leg: Leg
    result: Any = None
    error: str | None = None
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class FanOutResult:
    """Everything one turn's parallel fan-out produced."""

    chunk_hits: list[ChunkHit] = field(default_factory=list)
    #: Non-chunk leg name -> its outcome (only successes; see `run_legs`).
    other: dict[str, LegOutcome] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)
    #: Chunk-kind leg name -> how many hits IT contributed, before merge/dedup.
    #: Exists because the merge above is a union: once hits land in
    #: `chunk_hits`, which leg found which is gone. A caller deciding whether
    #: enrichment is worth running (e.g. "did >=2 sub-question legs return
    #: evidence") needs the per-leg count, not the merged total.
    chunk_counts_by_leg: dict[str, int] = field(default_factory=dict)

    def as_metadata(self) -> dict[str, Any]:
        """``msg_metadata`` fragment — only present when there is something to say."""
        payload: dict[str, Any] = {}
        if self.failed:
            payload["legs_failed"] = list(self.failed)
        if self.timings_ms:
            payload["leg_timings_ms"] = dict(self.timings_ms)
            # A live `status` frame cannot carry this: `_prepare_context` runs
            # in a worker thread and the leg count is only known once the
            # fan-out has finished, well after the pre-generation `status`
            # frame already went out. `ChatMessageMeta` shows it instead —
            # `len(timings_ms)` already implies it, but a named field is what
            # a diagnostics panel renders rather than a reader counting dict
            # keys.
            payload["leg_count"] = len(self.timings_ms)
        return payload


def _run_leg(leg: Leg) -> LegOutcome:
    started = time.monotonic()
    try:
        result = leg.run()
        return LegOutcome(
            leg=leg, result=result, duration_ms=int((time.monotonic() - started) * 1000)
        )
    except Exception as exc:  # noqa: BLE001 — a leg failure degrades, never propagates
        logger.warning("Chat leg %r (%s) failed: %s", leg.name, leg.kind, exc)
        return LegOutcome(
            leg=leg, error=str(exc), duration_ms=int((time.monotonic() - started) * 1000)
        )


def _dedup_chunks(hits: list[ChunkHit]) -> list[ChunkHit]:
    """Union chunk-kind legs, keep-max on ``(file_uuid, chunk_index)``.

    "Keep-max" rather than "first-wins": a chunk two legs both surfaced is
    real corroborating signal, and discarding the higher of the two scores in
    favour of whichever leg happened to run first would throw that signal
    away for no reason.
    """
    best: dict[tuple[str, int], ChunkHit] = {}
    for hit in hits:
        key = (hit.file_uuid, hit.chunk_index)
        current = best.get(key)
        if current is None or (hit.score or 0.0) > (current.score or 0.0):
            best[key] = hit
    return list(best.values())


def _leg_plane_or_source(kind: str) -> dict[str, Any]:
    """Trace detail identifying WHERE a leg reads from, never WHAT it reads.

    Chunk-kind legs are OpenSearch (``plane``); every other kind in this
    module goes through a Postgres session factory (``source``) — see the
    module docstring's "each leg owns its own SHORT session" rule, which is
    exactly the boundary this distinguishes.
    """
    if kind in CHUNK_LEG_KINDS:
        return {"plane": "chunk"}
    return {"source": "postgres"}


def run_legs(
    legs: list[Leg],
    *,
    max_workers: int = DEFAULT_MAX_PARALLEL_LEGS,
    timeout_seconds: float = DEFAULT_LEG_TIMEOUT_SECONDS,
    cancel_check: Callable[[], bool] | None = None,
    recorder: TraceRecorder | None = None,
    parent: str | None = None,
) -> FanOutResult:
    """Submit every leg to the shared executor and collect what returns.

    ``cancel_check`` is polled BETWEEN submissions, not from inside a leg —
    a leg already in flight is not interrupted (that would need cooperative
    cancellation inside every retrieval/aggregation call this fans out to,
    which none of them support), but a cancelled turn stops queuing NEW work
    the moment the flag is seen. This is the "between leg submissions"
    cancellation point ``service.py``'s docstring names.

    A leg that raises, or that does not finish within ``timeout_seconds``,
    is recorded in ``FanOutResult.failed`` and contributes nothing — the
    turn degrades rather than fails.

    Args:
        legs: The units of work for this turn's fan-out.
        max_workers: Passed to :func:`get_executor` (only takes effect on
            the first call in the process).
        timeout_seconds: Ceiling on waiting for any ONE leg's future.
        cancel_check: Returns ``True`` once a cancellation has been
            requested for this turn.
        recorder: Optional query-trace sink (GH #514's seam,
            ``services/chat/trace.py``). ``None`` — the default — costs
            nothing: every ``emit`` call below is a no-op guard check.
        parent: Trace node id every leg's ``FANNED_*``/``FOUND`` events are
            attached under (typically the plan's own node id).

    Returns:
        A :class:`FanOutResult`. Empty (all-defaults) when ``legs`` is empty.
    """
    result = FanOutResult()
    if not legs:
        return result

    executor = get_executor(max_workers)
    futures: dict[Future, Leg] = {}
    for leg in legs:
        if cancel_check is not None and cancel_check():
            logger.info("Chat fan-out cancelled before submitting leg %r", leg.name)
            result.failed.append(leg.name)
            emit(
                recorder,
                QueryStage.FOUND,
                Outcome.SKIPPED,
                parent=parent,
                node_id=leg.name,
                reason="cancelled",
            )
            continue
        stage = (
            QueryStage.FANNED_VECTOR
            if leg.kind in CHUNK_LEG_KINDS
            else QueryStage.FANNED_RELATIONAL
        )
        emit(
            recorder,
            stage,
            parent=parent,
            node_id=leg.name,
            leg=leg.name,
            **_leg_plane_or_source(leg.kind),
        )
        futures[executor.submit(_run_leg, leg)] = leg

    chunk_hits: list[ChunkHit] = []
    # `as_completed` yields each future the moment IT finishes, so a leg that
    # returns in 200ms reports immediately instead of queueing behind a slower
    # leg that merely happened to be submitted first. Collecting in submission
    # order produced identical RESULTS — every leg is awaited either way — but
    # reported an order the pipeline never ran in, which GH #514's panel would
    # then animate as fact.
    #
    # It also makes `timeout_seconds` mean what DEFAULT_LEG_TIMEOUT_SECONDS
    # already documented: a ceiling on the WHOLE fan-out. `future.result(timeout=)`
    # per future let N wedged legs wait N x the bound (measured: 4 legs at a 0.3s
    # bound took 1.20s). `as_completed(timeout=)` is one deadline across the set.
    pending: dict[Future, Leg] = dict(futures)
    try:
        for future in as_completed(futures, timeout=timeout_seconds):
            leg = pending.pop(future)
            outcome = future.result()
            result.timings_ms[leg.name] = outcome.duration_ms
            if not outcome.ok:
                result.failed.append(leg.name)
                emit(
                    recorder,
                    QueryStage.FOUND,
                    Outcome.FAILED,
                    parent=parent,
                    node_id=leg.name,
                    reason="error",
                    ms=outcome.duration_ms,
                )
                continue
            if leg.kind in CHUNK_LEG_KINDS:
                count = len(outcome.result or [])
                result.chunk_counts_by_leg[leg.name] = count
                if outcome.result:
                    chunk_hits.extend(outcome.result)
            else:
                result.other[leg.name] = outcome
                count = 1 if outcome.result else 0
            emit(
                recorder,
                QueryStage.FOUND,
                Outcome.OK if count else Outcome.EMPTY,
                parent=parent,
                node_id=leg.name,
                count=count,
                ms=outcome.duration_ms,
            )
    except FutureTimeoutError:
        # The fan-out deadline passed. `as_completed` stops yielding and tells us
        # nothing about which futures are outstanding — `pending` does, because
        # every leg that DID complete was popped from it above.
        pass

    for leg in pending.values():
        logger.warning(
            "Chat leg %r (%s) timed out after %.0fs", leg.name, leg.kind, timeout_seconds
        )
        result.failed.append(leg.name)
        emit(
            recorder,
            QueryStage.FOUND,
            Outcome.FAILED,
            parent=parent,
            node_id=leg.name,
            reason="timeout",
        )

    result.chunk_hits = _dedup_chunks(chunk_hits)
    return result
