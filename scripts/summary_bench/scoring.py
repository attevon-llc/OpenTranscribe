"""Glue between run records, the slice lock and the pure scorers in `tests.eval.harness`."""

from __future__ import annotations

from typing import Any

from tests.eval.harness.summary_scoring import CellScore, GoldRef, score_cell

from summary_bench.gold import load_gold
from summary_bench.inputs import CorpusPaths, InputSpec


def gold_refs(spec: InputSpec, paths: CorpusPaths, entry: dict[str, Any]) -> list[GoldRef]:
    """The gold items of one input, each placed in its locked slice (None when unlocated)."""
    slice_of = {g['id']: g['slice'] for g in entry['gold_items']}
    return [
        GoldRef(g.meeting_id, g.layer, g.sentence_id, g.text, slice_of.get(g.sentence_id))
        for g in load_gold(spec, paths, entry['offsets'])
    ]


def score_record(record: dict[str, Any], gold: list[GoldRef], entry: dict[str, Any]) -> CellScore:
    """Score one record against its input's gold; an error record scores zero everywhere."""
    return score_cell(
        cell_id=record['cell_id'],
        input_id=record['input'],
        arm=record['arm'],
        run=record['run'],
        status=record['status'],
        void_reason=record.get('void_reason'),
        merged=record.get('merged'),
        slice_outputs=record.get('slices') or [],
        gold=gold,
        tail_index=entry.get('tail_index'),
    )


def score_records(
    records: dict[str, dict[str, Any]],
    lock: dict[str, Any],
    specs: dict[str, InputSpec],
    paths: CorpusPaths,
) -> list[tuple[dict[str, Any], CellScore]]:
    """Every record with its score, in cell-id order. Gold is loaded once per input."""
    cache: dict[str, list[GoldRef]] = {}
    out: list[tuple[dict[str, Any], CellScore]] = []
    for cell_id in sorted(records):
        record = records[cell_id]
        input_id = record['input']
        if input_id not in cache:
            cache[input_id] = gold_refs(specs[input_id], paths, lock['inputs'][input_id])
        out.append((record, score_record(record, cache[input_id], lock['inputs'][input_id])))
    return out
