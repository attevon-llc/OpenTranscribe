"""One coherent percentage for a document that OCRs in N shards.

The owner requirement this exists for, verbatim from the #403 comment: *"OCR shards a
big scan ~20 pages at a time — roll shard progress up to the parent task so the modal
shows one coherent percentage, not N mystery tasks."*

Two rules follow from that, and both are structural rather than cosmetic:

1. **A shard never creates its own ``Task`` row.** ``create_task_record`` also rewrites
   ``media_file.active_task_id``, so twenty-five shards would each claim to be *the*
   active task and the status modal would show whichever finished last. Only
   ``documents.parse`` owns a ``Task``; shards report into it through :class:`ShardLedger`.
2. **The parent's progress is a single monotonic 0-100 across the whole pipeline**, not
   per stage. A modal that goes 0→100 for parsing, then 0→100 again for OCR, then 0→100
   for chunking is three progress bars wearing one coat. :data:`STAGE_BANDS` allocates
   one range per stage and :func:`overall_progress` maps within-stage progress into it.

The ledger lives in Redis because shards run in **different worker processes** — an
in-process counter would report each process's own view. ``HINCRBY`` is atomic, so the
aggregate is correct without a lock, and a lost shard costs an under-report rather than
a corrupted one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Stage → (start %, end %). The widths are proportional to measured wall clock on a
#: scanned document, which is what makes the bar move at a roughly constant rate rather
#: than sitting at 8 % for four minutes: OCR dominates everything else by an order of
#: magnitude (~13 s/page against ~4 ms/page for a text layer).
STAGE_BANDS: dict[str, tuple[float, float]] = {
    "download": (0.0, 5.0),
    "parse": (5.0, 15.0),
    "ocr": (15.0, 85.0),
    "chunk": (85.0, 95.0),
    "store": (95.0, 100.0),
}

#: How long a ledger survives with no writes. Longer than any plausible OCR run so a
#: stalled document keeps its partial progress, short enough that abandoned ledgers do
#: not accumulate.
LEDGER_TTL_SECONDS = 24 * 3600

_KEY_PREFIX = "document_ocr_shards"


def overall_progress(stage: str, within_stage: float) -> float:
    """Map ``within_stage`` ∈ [0, 1] into *stage*'s slice of the overall 0-100 bar.

    Args:
        stage: A key of :data:`STAGE_BANDS`.
        within_stage: Fraction of that stage completed, clamped into [0, 1].

    Returns:
        Overall percentage, 0-100.

    Raises:
        KeyError: for an unknown stage — deliberately loud. A typo'd stage silently
            mapping to 0 would make the bar jump backwards, which reads as a hung job.
    """
    start, end = STAGE_BANDS[stage]
    clamped = min(max(within_stage, 0.0), 1.0)
    return start + (end - start) * clamped


@dataclass(frozen=True)
class ShardRollup:
    """The aggregate a shard sees after reporting in."""

    done_shards: int
    total_shards: int
    done_pages: int
    total_pages: int
    failed_shards: int

    @property
    def complete(self) -> bool:
        """Every shard has reported, successfully or not."""
        return self.done_shards + self.failed_shards >= self.total_shards

    @property
    def fraction(self) -> float:
        """Progress within the OCR stage, by **pages**, not by shards.

        Pages, because shards are not equal work: the last shard of a 47-page document
        holds 7 pages, and counting shards would make the bar stall for the duration of
        a full-size shard and then jump. Falls back to shard counting when the page
        total is unknown (a backend that could not report a page count).
        """
        if self.total_pages > 0:
            return min(self.done_pages / self.total_pages, 1.0)
        if self.total_shards > 0:
            return min((self.done_shards + self.failed_shards) / self.total_shards, 1.0)
        return 0.0

    @property
    def overall(self) -> float:
        """The number the status modal shows."""
        return overall_progress("ocr", self.fraction)

    def describe(self) -> str:
        """The user-facing line under the bar. Names the shortfall when there is one."""
        base = f"Reading page {min(self.done_pages + 1, self.total_pages)} of {self.total_pages}"
        if self.total_pages <= 0:
            base = f"Reading section {self.done_shards + 1} of {self.total_shards}"
        if self.failed_shards:
            return f"{base} — {self.failed_shards} section(s) could not be read"
        return base


class ShardLedger:
    """Redis-backed shard accounting for one parent parse task.

    Constructed from the **parent** task id, so a shard needs to carry nothing but that
    id to report in, and two shards finishing at the same instant in two processes
    produce one aggregate rather than two half-views.
    """

    def __init__(self, parent_task_id: str) -> None:
        self.parent_task_id = parent_task_id
        self.key = f"{_KEY_PREFIX}:{parent_task_id}"

    def _client(self):  # noqa: ANN202 - redis.Redis, imported lazily
        from app.core.redis import get_redis

        return get_redis()

    def begin(self, *, total_shards: int, total_pages: int) -> None:
        """Declare the work. Overwrites any previous ledger for this task id.

        Overwriting rather than merging is deliberate: a retried parse re-shards the
        document, and a merged ledger would report progress against the union of two
        different shard plans.
        """
        client = self._client()
        pipe = client.pipeline()
        pipe.delete(self.key)
        pipe.hset(
            self.key,
            mapping={
                "total_shards": int(total_shards),
                "total_pages": int(total_pages),
                "done_shards": 0,
                "done_pages": 0,
                "failed_shards": 0,
            },
        )
        pipe.expire(self.key, LEDGER_TTL_SECONDS)
        pipe.execute()

    def record(self, *, pages_done: int, failed: bool = False) -> ShardRollup:
        """Report one finished shard and return the aggregate across all of them.

        A **failed** shard still counts toward completion. Otherwise one unreadable
        shard leaves the parent at 97 % forever, which is the single most common way a
        progress bar lies.
        """
        client = self._client()
        pipe = client.pipeline()
        pipe.hincrby(self.key, "failed_shards" if failed else "done_shards", 1)
        pipe.hincrby(self.key, "done_pages", max(int(pages_done), 0))
        pipe.expire(self.key, LEDGER_TTL_SECONDS)
        pipe.hgetall(self.key)
        *_, raw = pipe.execute()
        return _rollup_from_hash(raw)

    def read(self) -> ShardRollup:
        """Current aggregate without recording anything."""
        return _rollup_from_hash(self._client().hgetall(self.key))

    def clear(self) -> None:
        """Drop the ledger. Called once the parent task reaches a terminal state."""
        try:
            self._client().delete(self.key)
        except Exception as exc:  # noqa: BLE001 - cleanup must never fail a finished parse
            logger.debug("could not clear shard ledger %s: %s", self.key, exc)


def _rollup_from_hash(raw: dict) -> ShardRollup:
    """Decode a Redis hash. Missing keys read as zero — an absent ledger is 0 %, not a
    crash: the ledger's TTL can expire under a parse that outlived it, and reporting
    "no progress" beats taking down the task that was about to finish."""

    def _get(name: str) -> int:
        value = raw.get(name) or raw.get(name.encode())
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    return ShardRollup(
        done_shards=_get("done_shards"),
        total_shards=_get("total_shards"),
        done_pages=_get("done_pages"),
        total_pages=_get("total_pages"),
        failed_shards=_get("failed_shards"),
    )


def plan_shards(page_count: int, shard_pages: int) -> list[tuple[int, int]]:
    """Split a document into inclusive 1-based ``(first_page, last_page)`` ranges.

    Sharding buys **fairness**, not throughput: with ``worker_prefetch_multiplier=1``
    globally, a 500-page scan submitted as one task occupies the documents worker for
    ~25 minutes and everything queued behind it waits. As ~25 one-minute tasks it
    interleaves.
    """
    if page_count <= 0 or shard_pages <= 0:
        return []
    return [
        (first, min(first + shard_pages - 1, page_count))
        for first in range(1, page_count + 1, shard_pages)
    ]
