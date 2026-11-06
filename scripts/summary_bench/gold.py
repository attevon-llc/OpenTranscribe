"""Gold decisions and actions for an input: their times shifted into the rendered transcript."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from summary_bench.inputs import CorpusPaths, InputSpec, slice_index_for

_AMI_ID = re.compile(r'^[A-Z]{2}\d{4}[a-z]$')


@dataclass(frozen=True)
class PlacedGold:
    """A gold item with its time in the RENDERED transcript (session offset applied)."""

    meeting_id: str
    layer: str
    sentence_id: str
    text: str
    linked: bool
    time_s: float | None


def corpus_of(meeting_id: str) -> str:
    """AMI ids look like ``ES2008a``/``IS1003a``; ICSI ids like ``Bed003``."""
    return 'ami' if _AMI_ID.match(meeting_id) else 'icsi'


def load_gold(spec: InputSpec, paths: CorpusPaths, offsets: dict[str, float]) -> list[PlacedGold]:
    """Every scored-layer gold item of ``spec``'s meetings, placed in time."""
    from app.scripts.corpus_injection.nxt_summaries import (
        SCORED_LAYERS,
        ami_layout,
        icsi_layout,
        read_gold_items,
    )

    layouts = {'ami': ami_layout(paths.ami_manual), 'icsi': icsi_layout(paths.icsi_plus)}
    placed: list[PlacedGold] = []
    for meeting_id in spec.meetings:
        for item in read_gold_items(layouts[corpus_of(meeting_id)], meeting_id):
            if item.layer not in SCORED_LAYERS:
                continue
            shifted = None if item.start_time is None else item.start_time + offsets[meeting_id]
            placed.append(
                PlacedGold(
                    meeting_id,
                    item.layer,
                    item.sentence_id,
                    item.text,
                    item.link_count > 0,
                    shifted,
                )
            )
    return placed


def gold_counts(gold: list[PlacedGold], chunks: list[str]) -> dict[str, Any]:
    """Counts only (no prose): per layer total / linked / located, and located per slice."""
    layers: dict[str, dict[str, int]] = {}
    for layer in sorted({g.layer for g in gold}):
        items = [g for g in gold if g.layer == layer]
        layers[layer] = {
            'total': len(items),
            'linked': sum(g.linked for g in items),
            'located': sum(g.time_s is not None for g in items),
        }
    per_slice: Counter[int] = Counter(
        slice_index_for(g.time_s, chunks) for g in gold if g.time_s is not None
    )
    return {
        'layers': layers,
        'located_per_slice': {str(i): per_slice.get(i, 0) for i in range(1, len(chunks) + 1)},
        'unlocated': sum(g.time_s is None for g in gold),
    }
