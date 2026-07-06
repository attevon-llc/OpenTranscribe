"""Speaker-vector import and parity helpers for SQLite search."""
from __future__ import annotations

from typing import Any

from app.services.sqlite_search.store import SQLiteSearchStore


def _speaker_doc_rows(limit: int = 500) -> list[dict[str, Any]]:
    """Read current OpenSearch speaker docs with embeddings."""
    from app.core.constants import get_speaker_index
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if client is None:
        return []
    body = {
        "size": limit,
        "query": {"exists": {"field": "embedding"}},
        "_source": ["speaker_id", "profile_id", "name", "embedding", "embedding_model"],
    }
    hits = client.search(index=get_speaker_index(), body=body)["hits"]["hits"]
    rows = []
    for idx, hit in enumerate(hits, start=1):
        src = hit.get("_source", {})
        if len(src.get("embedding") or []) != 512:
            continue
        rows.append({
            "rowid": idx,
            "speaker_id": src.get("speaker_id"),
            "profile_id": src.get("profile_id"),
            "name": src.get("name"),
            "model": src.get("embedding_model") or "sherpa-3dspeaker",
            "embedding": src["embedding"],
        })
    return rows


def import_speaker_vectors(sqlite_path: str, limit: int = 500) -> dict[str, Any]:
    """Copy current OpenSearch speaker vectors into SQLite for parity."""
    rows = _speaker_doc_rows(limit=limit)
    store = SQLiteSearchStore(sqlite_path)
    with store.connect() as conn:
        for row in rows:
            store.upsert_speaker_vector(conn, row)
        conn.commit()
        summary = store.log_summary(conn)
    return {**summary, "rows_seen": len(rows)}


def compare_speaker_vector(vector, sqlite_path: str, limit: int = 5) -> dict[str, Any]:
    """Compare top speaker IDs from SQLite and current OpenSearch."""
    from app.services.opensearch_service import find_matching_speaker

    store = SQLiteSearchStore(sqlite_path)
    with store.connect() as conn:
        sqlite_rows = store.search_speaker_vectors(conn, vector, limit=limit)
    os_match = find_matching_speaker(list(vector), user_id=0, threshold=-1.0)
    return {
        "sqlite_count": len(sqlite_rows),
        "sqlite_top_speaker_id": sqlite_rows[0].get("speaker_id") if sqlite_rows else None,
        "opensearch_top_speaker_id": os_match.get("speaker_id") if os_match else None,
        "has_sample": bool(sqlite_rows or os_match),
    }
