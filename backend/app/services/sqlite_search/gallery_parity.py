"""Compare canonical Auris gallery decisions with sqlite-vec decisions."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from app.services.sqlite_search.store import SQLiteSearchStore


def _l2(a: list[float], b: list[float]) -> float:
    """Return Euclidean distance between two vectors."""
    return math.sqrt(sum((x - y) * (x - y) for x, y in zip(a, b)))


def _top_from_gallery(centroids: dict[str, list[float]], vector: list[float]) -> dict[str, Any]:
    """Return nearest gallery row by L2 distance."""
    name, distance = min(((name, _l2(vec, vector)) for name, vec in centroids.items()), key=lambda x: x[1])
    return {"name": name, "distance": distance}


def compare_gallery_decision(sqlite_path: str, centroids_path: str | Path, query_name: str) -> dict[str, Any]:
    """Compare canonical JSON-gallery and sqlite-vec top match for one speaker."""
    centroids = json.loads(Path(centroids_path).read_text())
    if query_name not in centroids:
        raise KeyError(f"speaker not in gallery: {query_name}")
    vector = [float(item) for item in centroids[query_name]]
    gallery_top = _top_from_gallery(centroids, vector)
    store = SQLiteSearchStore(sqlite_path)
    with store.connect() as conn:
        sqlite_rows = store.search_speaker_vectors(conn, vector, limit=1)
    sqlite_top = sqlite_rows[0] if sqlite_rows else None
    sqlite_name = sqlite_top.get("name") if sqlite_top else None
    return {
        "query_name": query_name,
        "gallery_top": gallery_top["name"],
        "sqlite_top": sqlite_name,
        "match": gallery_top["name"] == sqlite_name,
        "gallery_distance": gallery_top["distance"],
        "sqlite_distance": sqlite_top.get("distance") if sqlite_top else None,
    }


def compare_gallery_sample(sqlite_path: str, centroids_path: str | Path, limit: int = 10) -> dict[str, Any]:
    """Compare deterministic first-N gallery names against sqlite-vec."""
    centroids = json.loads(Path(centroids_path).read_text())
    results = [compare_gallery_decision(sqlite_path, centroids_path, name) for name in sorted(centroids)[:limit]]
    return {
        "sample_size": len(results),
        "matches": sum(1 for item in results if item["match"]),
        "mismatches": [item for item in results if not item["match"]],
        "results": results,
    }
