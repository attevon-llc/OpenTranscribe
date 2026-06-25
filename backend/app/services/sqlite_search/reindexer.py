"""Bounded transcript reindexing into embedded SQLite search."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.sqlite_search.store import SQLiteSearchStore


def _date_filter(query, media_model, date_from: datetime | None, date_to: datetime | None):
    """Apply optional upload_time bounds."""
    if date_from is not None:
        query = query.filter(media_model.upload_time >= date_from)
    if date_to is not None:
        query = query.filter(media_model.upload_time < date_to)
    return query


class SQLiteTranscriptReindexer:
    """Indexes bounded completed transcript segments into SQLite FTS."""

    def __init__(self, store: SQLiteSearchStore):
        self.store = store

    def rows_for_slice(
        self,
        db,
        *,
        user_id: int,
        date_from=None,
        date_to=None,
        limit: int | None = None,
        file_uuids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch completed transcript rows for one bounded user slice."""
        from app.models.media import FileStatus, MediaFile, Speaker, TranscriptSegment

        query = (
            db.query(TranscriptSegment, MediaFile, Speaker)
            .join(MediaFile, MediaFile.id == TranscriptSegment.media_file_id)
            .outerjoin(Speaker, Speaker.id == TranscriptSegment.speaker_id)
            .filter(MediaFile.user_id == user_id, MediaFile.status == FileStatus.COMPLETED)
            .order_by(MediaFile.upload_time, TranscriptSegment.start_time)
        )
        query = _date_filter(query, MediaFile, date_from, date_to)
        if file_uuids:
            query = query.filter(MediaFile.uuid.in_(file_uuids))
        if limit is not None:
            query = query.limit(limit)
        rows = []
        for segment, media, speaker in query.all():
            rows.append({
                "id": int(segment.id),
                "file_id": int(media.id),
                "file_uuid": str(media.uuid),
                "user_id": int(media.user_id),
                "source": "transcript",
                "title": str(media.title or media.filename),
                "speaker": str(speaker.display_name or speaker.name) if speaker else None,
                "start_time": float(segment.start_time),
                "end_time": float(segment.end_time),
                "upload_time": media.upload_time.isoformat() if media.upload_time else "",
                "content_type": str(media.content_type or ""),
                "duration": float(media.duration or 0.0),
                "file_size": int(media.file_size or 0),
                "language": str(media.language or ""),
                "content": str(segment.text),
            })
        return rows

    def reindex_rows(self, rows: list[dict[str, Any]]) -> dict[str, int | bool]:
        """Write rows into SQLite and return orchestrator count summary."""
        with self.store.connect() as conn:
            for row in rows:
                self.store.upsert_transcript_chunk(conn, row)
            conn.commit()
            summary = self.store.log_summary(conn)
        return {**summary, "rows_seen": len(rows)}
