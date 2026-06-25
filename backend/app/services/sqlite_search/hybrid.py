"""Hybrid ranking helpers for embedded SQLite search."""
from __future__ import annotations

from typing import Any


def _ranked_file_ids(rows: list[dict[str, Any]]) -> list[int]:
    """Return unique file ids preserving row order."""
    seen: set[int] = set()
    out: list[int] = []
    for row in rows:
        file_id = row.get("file_id")
        if file_id is None or int(file_id) in seen:
            continue
        seen.add(int(file_id))
        out.append(int(file_id))
    return out


def rrf_fuse(
    text_rows: list[dict[str, Any]],
    vector_rows: list[dict[str, Any]],
    *,
    rank_constant: int = 30,
) -> list[dict[str, Any]]:
    """Fuse ranked text/vector rows with Reciprocal Rank Fusion."""
    scores: dict[int, float] = {}
    ranks: dict[int, dict[str, int]] = {}
    for source, ids in (("text", _ranked_file_ids(text_rows)), ("vector", _ranked_file_ids(vector_rows))):
        for rank, file_id in enumerate(ids, start=1):
            scores[file_id] = scores.get(file_id, 0.0) + 1.0 / (rank_constant + rank)
            ranks.setdefault(file_id, {})[source] = rank
    return [
        {"file_id": file_id, "score": score, "ranks": ranks.get(file_id, {})}
        for file_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]


def hybrid_text_only(text_rows: list[dict[str, Any]], *, rank_constant: int = 30) -> list[dict[str, Any]]:
    """Return hybrid-shaped ranking when only FTS rows are available."""
    return rrf_fuse(text_rows, [], rank_constant=rank_constant)
