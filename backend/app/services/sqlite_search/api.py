"""API helpers for the embedded SQLite search index."""
from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.services.sqlite_search.store import SQLiteSearchStore


def _indexable_file_uuids(db, user_id: int) -> set[str]:
    """Return completed transcript-bearing file UUIDs for one user."""
    from sqlalchemy import exists
    from sqlalchemy import select

    from app.models.media import FileStatus, MediaFile, TranscriptSegment

    has_segments = exists(
        select(TranscriptSegment.id).where(TranscriptSegment.media_file_id == MediaFile.id)
    )
    rows = (
        db.query(MediaFile.uuid)
        .filter(MediaFile.user_id == user_id, MediaFile.status == FileStatus.COMPLETED, has_segments)
        .all()
    )
    return {str(row[0]) for row in rows}


def _indexed_file_uuids(user_id: int) -> set[str]:
    """Return file UUIDs currently present in the SQLite text index."""
    store = SQLiteSearchStore(settings.SQLITE_SEARCH_PATH)
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT file_uuid FROM transcript_chunk WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return {str(row[0]) for row in rows}


def sqlite_reindex_status(db, user_id: int, in_progress: bool = False) -> dict[str, Any]:
    """Return API-compatible status for the embedded SQLite search index."""
    from app.services.sqlite_search.orchestrator import SQLiteSearchOrchestrator

    all_uuids = _indexable_file_uuids(db, user_id)
    indexed_uuids = _indexed_file_uuids(user_id)
    state = SQLiteSearchOrchestrator(SQLiteSearchStore(settings.SQLITE_SEARCH_PATH)).status()
    return {
        "total_files": len(all_uuids),
        "indexed_files": len(indexed_uuids & all_uuids),
        "pending_files": len(all_uuids - indexed_uuids),
        "in_progress": in_progress or state.get("status") == "running",
        "stop_requested": False,
        "current_model": "sqlite-fts5-sqlite-vec",
        "current_dimension": 512,
        "last_indexed_at": state.get("finished_at"),
        "backend": "sqlite",
        "counts": state.get("counts", {}),
        "job": {k: v for k, v in state.items() if k != "counts"},
    }


def sqlite_pending_file_uuids(db, user_id: int) -> list[str]:
    """Return completed file UUIDs missing from the SQLite text index."""
    return sorted(_indexable_file_uuids(db, user_id) - _indexed_file_uuids(user_id))
