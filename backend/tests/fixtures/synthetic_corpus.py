"""A miniature corpus in the synthetic generator's exact on-disk format.

Shared by the adapter's unit suite and its OpenSearch-backed integration suite,
because a second copy of this layout is a second thing that can drift from
``backend/tests/eval/synthetic/corpus.py``.

The query set is deliberately shaped to make the selection tests falsifiable:
``ag-00000``'s gold spans both the first and the last meeting in sorted-key
order, which is what a first-N-by-key subset cannot close, and ``ag-00001``
carries ``related_files`` so R7's filtered-count structure is exercised.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Eight meetings over two teams, four series sessions each.
MEETING_KEYS = [f"T00{team}-S0-{index:04d}" for team in (0, 1) for index in range(4)]

SPEAKERS = ("Ada Vance", "Bo Ruiz", "Cy Nkemi")

QUERIES: list[dict[str, Any]] = [
    {
        "query_id": "mf-00000",
        "query_class": "multi_file",
        "text": "Across the planning syncs for team 0, what did we agree for the ingest gateway?",
        "scored_on": "retrieval",
        "gold_files": ["corpusuuid-T000-S0-0000", "corpusuuid-T000-S0-0001"],
        "gold_turns": {
            "corpusuuid-T000-S0-0000": [[2, 3]],
            "corpusuuid-T000-S0-0001": [[4, 4]],
        },
        "related_files": [],
    },
    {
        "query_id": "ag-00000",
        "query_class": "aggregation",
        "text": "How many meetings discussed the Cedar Lantern compliance audit?",
        "scored_on": "answer",
        # First and last in sorted-key order, plus one in between: the shape
        # rng.sample(org.sessions, k) produces on the real corpus.
        "gold_files": [
            "corpusuuid-T000-S0-0000",
            "corpusuuid-T001-S0-0002",
            "corpusuuid-T001-S0-0003",
        ],
        "gold_turns": {
            "corpusuuid-T000-S0-0000": [[1, 1]],
            "corpusuuid-T001-S0-0002": [[3, 3]],
            "corpusuuid-T001-S0-0003": [[0, 0]],
        },
        "related_files": [],
    },
    {
        "query_id": "ag-00001",
        "query_class": "aggregation",
        "text": "How many meetings in March 2025 discussed the Slate Viaduct review?",
        "scored_on": "answer",
        "gold_files": ["corpusuuid-T000-S0-0002"],
        "gold_turns": {"corpusuuid-T000-S0-0002": [[5, 5]]},
        # R7's out-of-month mentions: not gold, but the query is only a
        # *filtered* count while they are also in the index.
        "related_files": ["corpusuuid-T001-S0-0000"],
    },
    {
        "query_id": "lk-00000",
        "query_class": "lookup",
        "text": "What was the agreed cost for the ingest gateway?",
        "scored_on": "retrieval",
        "gold_files": ["corpusuuid-T001-S0-0001"],
        "gold_turns": {"corpusuuid-T001-S0-0001": [[6, 6]]},
        "related_files": [],
    },
]


def turn(index: int, speaker: str, content: str, start: float, end: float) -> dict[str, Any]:
    """One turn in the generator's shape (``content``, not ``text``)."""
    return {
        "index": index,
        "speaker": speaker,
        "speaker_id": speaker.replace(" ", ""),
        "content": content,
        "start": start,
        "end": end,
    }


def meeting(key: str, n_turns: int = 8, marker: str = "") -> dict[str, Any]:
    """One meeting record in the generator's exact on-disk shape."""
    turns = []
    clock = 0.0
    for index in range(n_turns):
        text = f"{key} turn {index}: we reviewed the ingest gateway and the rollout plan."
        if marker and index == 1:
            text = f"{key} turn {index}: {marker} was raised and deferred to next session."
        turns.append(
            turn(index, SPEAKERS[index % len(SPEAKERS)], text, round(clock, 2), clock + 4.0)
        )
        clock += 5.0
    return {
        "corpus_id": "otsynth-fixture-v1",
        # Deliberately not derivable from meeting_key: the harness's join has to
        # go through the record, not through a formula.
        "file_uuid": f"corpusuuid-{key}",
        "meeting_key": key,
        "team_id": key[:4],
        "series_id": key[:7],
        "series_kind": "planning sync",
        "register": "formal",
        "date": "2025-01-02",
        "near_duplicate_cluster": None,
        "title": f"{key[:4]} — planning sync ({key})",
        "speakers": [
            {"speaker_id": name.replace(" ", ""), "name": name, "role": "lead"} for name in SPEAKERS
        ],
        "turn_count": len(turns),
        "word_count": sum(len(t["content"].split()) for t in turns),
        "duration_seconds": turns[-1]["end"],
        "turns": turns,
    }


def write_fixture_corpus(root: Path, meetings: list[dict[str, Any]] | None = None) -> Path:
    """Write the corpus under ``root`` and return the rung directory.

    ``root`` stands in for ``$RAG_EVAL_DATA_DIR/synthetic``; the corpus itself
    lands one level down, exactly as the real one does.
    """
    corpus = Path(root) / "otsynth-fixture-v1"
    (corpus / "meetings").mkdir(parents=True)
    records = meetings if meetings is not None else [meeting(key) for key in MEETING_KEYS]
    # Two shards, as the generator writes them, so the byte-offset index is
    # exercised across files rather than within one.
    half = max(1, len(records) // 2)
    for shard_index, start in enumerate(range(0, len(records), half)):
        (corpus / "meetings" / f"part-{shard_index:04d}.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in records[start : start + half]),
            encoding="utf-8",
        )
    (corpus / "queries.jsonl").write_text(
        "".join(json.dumps(q, sort_keys=True) + "\n" for q in QUERIES), encoding="utf-8"
    )
    (corpus / "config.json").write_text(
        json.dumps(
            {"corpus_id": "otsynth-fixture-v1", "generator_version": "1.0.0", "seed": 20260812}
        ),
        encoding="utf-8",
    )
    return corpus


def gold_meeting_keys(query_id: str) -> list[str]:
    """The ``meeting_key``s in one fixture query's gold set."""
    query = next(q for q in QUERIES if q["query_id"] == query_id)
    return [uuid_.removeprefix("corpusuuid-") for uuid_ in query["gold_files"]]
