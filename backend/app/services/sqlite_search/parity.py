"""Parity checks between SQLite FTS and OpenSearch chunk search."""
from __future__ import annotations

from typing import Any

from app.services.sqlite_search.store import SQLiteSearchStore


def _ids(rows: list[dict[str, Any]]) -> list[int]:
    """Return unique file IDs preserving rank order."""
    seen: set[int] = set()
    ordered: list[int] = []
    for row in rows:
        file_id = int(row["file_id"])
        if file_id not in seen:
            seen.add(file_id)
            ordered.append(file_id)
    return ordered


def _opensearch_rows(query: str, limit: int) -> list[dict[str, Any]]:
    """Fetch OpenSearch chunk hits for a keyword query."""
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if client is None:
        return []
    body = {
        "size": limit,
        "query": {"multi_match": {"query": query, "fields": ["content", "title"]}},
        "_source": ["file_id", "file_uuid", "title", "content", "start_time"],
    }
    response = client.search(index=settings.OPENSEARCH_CHUNKS_INDEX, body=body)
    return [dict(hit.get("_source", {}), score=hit.get("_score")) for hit in response["hits"]["hits"]]


def compare_fts(query: str, sqlite_path: str, limit: int = 10) -> dict[str, Any]:
    """Compare SQLite FTS and OpenSearch chunk results for one query."""
    store = SQLiteSearchStore(sqlite_path)
    with store.connect() as conn:
        sqlite_rows = store.search_text(conn, query, limit=limit)
    os_rows = _opensearch_rows(query, limit)
    sqlite_ids = _ids(sqlite_rows)
    os_ids = _ids(os_rows)
    overlap = sorted(set(sqlite_ids).intersection(os_ids))
    return {
        "query": query,
        "sqlite_count": len(sqlite_rows),
        "opensearch_count": len(os_rows),
        "sqlite_file_ids": sqlite_ids,
        "opensearch_file_ids": os_ids,
        "overlap_file_ids": overlap,
        "missing_from_sqlite": sorted(set(os_ids) - set(sqlite_ids)),
        "missing_from_opensearch": sorted(set(sqlite_ids) - set(os_ids)),
    }
