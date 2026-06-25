"""Import transcript semantic vectors from current OpenSearch chunks."""
from __future__ import annotations

from typing import Any

from app.services.sqlite_search.store import SQLiteSearchStore, TRANSCRIPT_VECTOR_DIM


def _chunk_vector_rows(limit: int = 1000) -> list[dict[str, Any]]:
    """Read current OpenSearch transcript chunks that have embeddings."""
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if client is None:
        return []
    body = {
        "size": limit,
        "query": {"exists": {"field": "embedding"}},
        "_source": ["file_id", "file_uuid", "chunk_index", "embedding", "embedding_model"],
    }
    hits = client.search(index=settings.OPENSEARCH_CHUNKS_INDEX, body=body)["hits"]["hits"]
    rows = []
    for idx, hit in enumerate(hits, start=1):
        src = hit.get("_source", {})
        if len(src.get("embedding") or []) != TRANSCRIPT_VECTOR_DIM:
            continue
        rows.append({
            "rowid": idx,
            "chunk_id": int(src.get("chunk_index") or idx),
            "file_id": int(src["file_id"]),
            "file_uuid": str(src["file_uuid"]),
            "model": src.get("embedding_model"),
            "embedding": src["embedding"],
        })
    return rows


def import_transcript_vectors(sqlite_path: str, limit: int = 1000) -> dict[str, Any]:
    """Copy current OpenSearch transcript vectors into SQLite."""
    rows = _chunk_vector_rows(limit=limit)
    store = SQLiteSearchStore(sqlite_path)
    with store.connect() as conn:
        conn.execute("DELETE FROM transcript_vec")
        conn.execute("DELETE FROM transcript_vec_meta")
        for row in rows:
            store.upsert_transcript_vector(conn, row)
        conn.commit()
        summary = store.log_summary(conn)
    return {**summary, "rows_seen": len(rows)}
