"""Query-execution trace: the stage vocabulary and a recorder that costs nothing.

The UI half of this — a collapsible panel rendering the trace as a growing,
animated tree — is **GH #514 and is deliberately not built yet**. What lives here
is the seam, and it lives here *now* on purpose: instrumenting a retrieval stage
at the moment it is written is nearly free, while reconstructing the same
boundaries afterwards means reopening every stage. The parallel-leg fan-out is
the clearest example — siblings expanding at once is the whole visual payoff and
the hardest thing to recover after the fact.

**Design rules, in the order they matter:**

1. **The trace never becomes a second source of truth.** It reports what the
   pipeline did. Nothing in the pipeline may read it back, branch on it, or fail
   because of it. Every public function here swallows its own errors: a broken
   trace must never break a turn.
2. **Off by default and genuinely free.** With no recorder attached, `record()`
   is a bound-method call that returns immediately. There is no frame, no Redis
   round trip, and no allocation per stage.
3. **Nothing is pushed to a client yet.** Until #514 lands the client allowlist
   entry, stages are *recorded*, not emitted over SSE. The repo's contract test
   asserts backend frame emitters are a subset of the client's known events, so
   emitting a frame the SPA cannot render would fail it — correctly.
4. **A stage that ran and found nothing is NOT the same as a stage that never
   ran.** `Outcome` makes that distinction explicit, because it is the whole
   reason the panel is worth building: this pipeline keeps producing answers that
   look grounded but ran on less evidence than the reader assumes.
5. **It must not leak.** A stage's `detail` can carry counts and plane names. It
   must never carry file titles, speaker names, or anything else permission
   scoped — the panel shows only what that user could already see, and the safe
   way to guarantee that is for the trace to never hold it in the first place.
   `SAFE_DETAIL_KEYS` is the allowlist and `_scrub` enforces it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from typing import Any
from typing import Protocol

logger = logging.getLogger(__name__)


class QueryStage(StrEnum):
    """The pipeline stages a user can watch advance.

    Ordered as they normally occur, but the trace is a TREE, not a list: a
    fan-out produces several ``FANNED_*`` stages as siblings under one
    ``PLANNED``, and they complete out of order. Do not assume this ordering
    when rendering.

    **Render in EMISSION order, not this order.** This sequence exists so a
    consumer can tell whether a node has advanced (a leg reports ``FANNED_*``
    then ``FOUND`` under one node id); sorting a rendered tree by it would hide
    a stage running out of sequence, which is exactly what is worth seeing.

    ⚠️ **Widening this enum is a deliberate contract change**, pinned by
    ``tests/unit/test_chat_trace_seam.py::test_every_stage_in_the_documented_workflow_exists``.
    The five stages beyond the original eleven (``REWRITTEN``, ``CACHE_LOOKUP``,
    ``SAMPLED``, ``EXPANDED``, ``BUDGETED``) name work the pipeline **already
    did** and never reported: a cache miss was invisible, and "48 candidates
    became 12" — the single most useful fact about where a turn's evidence went
    — had no node at all. The document plane deliberately gets no member of its
    own; it is ``FANNED_VECTOR`` with ``plane="document"``.
    """

    SUBMITTED = "submitted"
    VALIDATED = "validated"
    PARSED_NAMES = "parsed_names"  # speaker-mention resolution
    REWRITTEN = "rewritten"  # follow-up -> standalone query (an LLM round trip)
    CACHE_LOOKUP = "cache_lookup"  # Redis exact/semantic retrieval cache
    PLANNED = "planned"  # rules route, or the LLM planner fanned out
    FANNED_RELATIONAL = "fanned_relational"  # Postgres: scope, facts, aggregates
    FANNED_VECTOR = "fanned_vector"  # OpenSearch: chunk / digest / document plane
    FOUND = "found"  # N candidates, per leg
    RERANKED = "reranked"
    SAMPLED = "sampled"  # diversity sampling: N -> M, capped per file
    EXPANDED = "expanded"  # read-time context expansion of short chunks
    FILTERED = "filtered"  # permissions, quarantine, masking
    BUDGETED = "budgeted"  # what the excerpt budget actually fit
    REVIEWED = "reviewed"  # enrichment / synthesis, when enabled
    PRESENTED = "presented"  # prompt assembled, answer streaming


class Outcome(StrEnum):
    """How a stage ended.

    ``EMPTY`` and ``SKIPPED`` are different answers and must render differently:
    "we looked there and found nothing" versus "we never looked". Collapsing them
    is the exact ambiguity this trace exists to remove.
    """

    OK = "ok"
    EMPTY = "empty"  # ran, found nothing
    SKIPPED = "skipped"  # never ran (flag off, no provider, not applicable)
    CACHED = "cached"  # served from cache, work skipped
    DECLINED = "declined"  # refused on purpose (unbounded scope, truncation, …)
    FAILED = "failed"


#: Detail keys a stage may carry. Deliberately a small allowlist of NON-identifying
#: values: counts, plane names, durations. A file title or speaker name in here
#: would be a permission leak the moment the panel renders it, so the trace never
#: holds one. Anything not listed is dropped by `_scrub`, silently — a dropped key
#: is a missing tooltip; a leaked one is an incident.
SAFE_DETAIL_KEYS = frozenset(
    {
        "plane",  # "chunk" | "digest" | "document"
        "source",  # "postgres" | "opensearch" | "cache"
        "count",  # candidates found / rows returned
        "kept",  # survivors after a filter
        "dropped",  # how many a filter removed
        "leg",  # leg index or kind, for fan-out siblings
        "legs",  # how many legs were dispatched
        "reason",  # short machine code, never free text
        "ms",  # duration
        "limit",  # a CONFIGURED bound (max per file, budget chars) — never content
    }
)


def _scrub(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only allowlisted, non-identifying detail keys."""
    if not detail:
        return {}
    return {k: v for k, v in detail.items() if k in SAFE_DETAIL_KEYS}


@dataclass(frozen=True)
class TraceEvent:
    """One node in the trace tree."""

    stage: QueryStage
    outcome: Outcome = Outcome.OK
    parent: str | None = None  # node id of the parent, for fan-out siblings
    node_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class TraceRecorder(Protocol):
    """What a turn hands to the pipeline. Implementations must not raise."""

    def record(self, event: TraceEvent) -> None: ...


class NullTraceRecorder:
    """The default. Does nothing, allocates nothing, cannot fail."""

    __slots__ = ()

    def record(self, event: TraceEvent) -> None:  # noqa: D102
        return None


class ListTraceRecorder:
    """Collects events in memory. For tests, and for the first UI iteration.

    Bounded on purpose: a pathological fan-out must not grow a turn's memory
    without limit, and a trace is a diagnostic, not a ledger.
    """

    __slots__ = ("events", "_cap", "_truncated")

    def __init__(self, cap: int = 200) -> None:
        self.events: list[TraceEvent] = []
        self._cap = cap
        self._truncated = False

    @property
    def truncated(self) -> bool:
        """True when events were dropped, so a reader is never silently misled."""
        return self._truncated

    def record(self, event: TraceEvent) -> None:  # noqa: D102
        if len(self.events) >= self._cap:
            self._truncated = True
            return
        self.events.append(event)


NULL_RECORDER: TraceRecorder = NullTraceRecorder()


def emit(
    recorder: TraceRecorder | None,
    stage: QueryStage,
    outcome: Outcome = Outcome.OK,
    *,
    parent: str | None = None,
    node_id: str | None = None,
    **detail: Any,
) -> None:
    """Record one stage. Never raises, whatever the recorder does.

    This is the ONLY function pipeline code should call. It takes ``None`` so a
    call site needs no guard, and it swallows recorder failures so an
    instrumentation bug can never take down a chat turn (design rule 1).
    """
    if recorder is None:
        return
    try:
        recorder.record(
            TraceEvent(
                stage=stage,
                outcome=outcome,
                parent=parent,
                node_id=node_id,
                detail=_scrub(detail),
            )
        )
    except Exception:  # noqa: BLE001 - a trace failure must never fail a turn
        logger.debug("query trace recorder raised; continuing", exc_info=True)
