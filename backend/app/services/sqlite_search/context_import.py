"""Import external context records into SQLite FTS search."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.services.sqlite_search.store import SQLiteSearchStore

SEARCHABLE_KINDS = {"ocr_text", "screen_context_report", "screen_activity_insights"}


def _stable_negative_id(*parts: object) -> int:
    """Return a deterministic negative SQLite row id."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return -(int(digest[:15], 16) % 9_000_000_000_000_000 + 1)


def _record_text(record: dict[str, Any]) -> str:
    """Build searchable text for one supported context record."""
    kind = record.get("kind")
    if kind == "ocr_text":
        return "\n".join(
            str(record.get(k) or "").strip()
            for k in ("window_title", "text")
            if str(record.get(k) or "").strip()
        )
    if kind == "screen_context_report":
        return "\n".join(
            str(record.get(k) or "").strip()
            for k in ("activity_label", "app_or_window", "summary")
            if str(record.get(k) or "").strip()
        )
    if kind == "screen_activity_insights":
        return json.dumps({
            "activity_mix": record.get("activity_mix", {}),
            "active_hours": record.get("active_hours", {}),
            "active_days": record.get("active_days", {}),
            "top_apps": record.get("top_apps", []),
        }, sort_keys=True)
    return ""


def _record_time(record: dict[str, Any]) -> str:
    """Return the best timestamp for sorting/filtering."""
    return str(record.get("captured_at") or record.get("window_start") or "")


def context_row(record: dict[str, Any], user_id: int | None = None) -> dict[str, Any] | None:
    """Convert a Rewind/HiNotes context record into a search row."""
    kind = str(record.get("kind") or "")
    text = _record_text(record)
    if kind not in SEARCHABLE_KINDS or not text.strip():
        return None
    source_id = str(record.get("source_id") or record.get("content_hash") or kind)
    recording = str(record.get("recording_id") or source_id)
    title = str(record.get("app_or_window") or record.get("window_title") or f"{kind} {recording}")
    return {
        "id": _stable_negative_id(record.get("source", "context"), kind, source_id),
        "file_id": _stable_negative_id(record.get("source", "context"), recording),
        "file_uuid": f"{record.get('source', 'context')}:{recording}",
        "user_id": user_id,
        "source": kind,
        "title": title,
        "speaker": None,
        "start_time": None,
        "end_time": None,
        "upload_time": _record_time(record),
        "content_type": "text/plain",
        "duration": None,
        "file_size": len(text.encode()),
        "language": "",
        "content": text,
    }


def records_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract records from a payload or a single insights object."""
    if isinstance(payload.get("records"), list):
        return list(payload["records"])
    return [payload] if payload.get("kind") in SEARCHABLE_KINDS else []


def import_context_payloads(
    sqlite_path: str,
    payload_paths: list[Path],
    user_id: int | None = None,
) -> dict[str, Any]:
    """Import context JSON payloads into the SQLite FTS table."""
    store = SQLiteSearchStore(sqlite_path)
    rows: list[dict[str, Any]] = []
    for path in payload_paths:
        payload = json.loads(path.expanduser().read_text())
        rows.extend(row for record in records_from_payload(payload) if (row := context_row(record, user_id)))
    with store.connect() as conn:
        for row in rows:
            store.upsert_transcript_chunk(conn, row)
        conn.commit()
        summary = store.log_summary(conn)
    return {**summary, "context_rows_seen": len(rows)}
