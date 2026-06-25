"""Import Auris clean speaker centroids into sqlite-vec."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.sqlite_search.store import SQLiteSearchStore, VECTOR_DIM

MODEL = "sherpa-3dspeaker"


def load_centroids(path: str | Path) -> list[dict[str, Any]]:
    """Load name -> 512-d centroid JSON as deterministic rows."""
    data = json.loads(Path(path).read_text())
    rows: list[dict[str, Any]] = []
    for rowid, name in enumerate(sorted(data), start=1):
        vector = data[name]
        if len(vector) != VECTOR_DIM:
            raise ValueError(f"{name}: expected {VECTOR_DIM}-d vector, got {len(vector)}")
        rows.append({
            "rowid": rowid,
            "speaker_id": None,
            "profile_id": None,
            "name": name,
            "model": MODEL,
            "embedding": vector,
        })
    return rows


def import_gallery(sqlite_path: str | Path, centroids_path: str | Path) -> dict[str, Any]:
    """Import all Auris clean centroids into SQLite speaker vectors."""
    rows = load_centroids(centroids_path)
    store = SQLiteSearchStore(sqlite_path)
    with store.connect() as conn:
        conn.execute("DELETE FROM speaker_vec")
        conn.execute("DELETE FROM speaker_vec_meta")
        for row in rows:
            store.upsert_speaker_vector(conn, row)
        conn.commit()
        summary = store.log_summary(conn)
    return {**summary, "rows_seen": len(rows), "source": str(centroids_path)}
