"""Measurement types shared by the privacy purge and the erasure receipt.

Both answer the same two questions: how many records did each store REPORT as removed
(:data:`GroupedCounts`), and what is still there when measured again in a fresh session
(:class:`Residual`). A measure that could not be taken is ``unverified``, never 0: an unreachable
store must not read as an empty one. Only counts leave this module (no names, no ids).
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

#: Counts of a store group (``postgres``, ``opensearch``, ``s3``, ``redis``, ``fs``, ``mirror``,
#: ``backup``), by the name the audit event and the status response use. The purge emits only the
#: first two; the erasure receipt emits all of them.
GroupedCounts = dict[str, dict[str, int]]


@dataclass
class Residual:
    """What a pass left behind. ``counts`` are measured; ``unverified`` names the measures that
    could not be taken (and so are NOT in ``counts``)."""

    counts: dict[str, int] = field(default_factory=dict)
    unverified: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only when everything was measured and every measure is 0."""
        return not self.unverified and all(v == 0 for v in self.counts.values())

    def merge(self, other: Residual) -> None:
        self.counts.update(other.counts)
        self.unverified.extend(other.unverified)


def merge_grouped(total: GroupedCounts, more: GroupedCounts) -> GroupedCounts:
    """Add ``more`` into ``total`` (pass 2 adds to pass 1)."""
    for store, values in more.items():
        bucket = total.setdefault(store, {})
        for key, amount in values.items():
            bucket[key] = bucket.get(key, 0) + int(amount)
    return total
