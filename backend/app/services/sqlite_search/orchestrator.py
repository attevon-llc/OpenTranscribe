"""Local orchestration for embedded SQLite search indexing."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.services.sqlite_search.reindexer import SQLiteTranscriptReindexer
from app.services.sqlite_search.store import SQLiteSearchStore

STATUS_KEY = "sqlite_search_reindex_status"


def _now() -> str:
    """Return an ISO timestamp for persisted job status."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ReindexSlice:
    """Bounded search indexing request."""
    user_id: int
    date_from: datetime | None = None
    date_to: datetime | None = None
    limit: int | None = None
    file_uuids: list[str] | None = None


@dataclass(frozen=True)
class JobStatus:
    """Persisted status for the latest local search indexing job."""
    status: str
    attempts: int
    started_at: str
    finished_at: str | None = None
    error: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)


class SQLiteSearchOrchestrator:
    """Coordinates bounded SQLite search indexing with status and retries."""

    def __init__(
        self,
        store: SQLiteSearchStore,
        reindexer: SQLiteTranscriptReindexer | None = None,
        max_attempts: int = 2,
    ):
        self.store = store
        self.reindexer = reindexer or SQLiteTranscriptReindexer(store)
        self.max_attempts = max(1, max_attempts)

    def status(self) -> dict[str, Any]:
        """Return the latest persisted status plus current index counts."""
        with self.store.connect() as conn:
            row = conn.execute("SELECT value FROM search_meta WHERE key = ?", (STATUS_KEY,)).fetchone()
            counts = self.store.count_summary(conn)
        state = json.loads(row[0]) if row else {"status": "idle", "attempts": 0}
        return {**state, "counts": counts}

    def run_reindex(self, db, index_slice: ReindexSlice) -> dict[str, Any]:
        """Run a bounded reindex with retry-visible status."""
        started = _now()
        last_error = None
        for attempt in range(1, self.max_attempts + 1):
            self._write(JobStatus("running", attempt, started, error=last_error))
            try:
                rows = self.reindexer.rows_for_slice(db, **asdict(index_slice))
                summary = self.reindexer.reindex_rows(rows)
                status = JobStatus("completed", attempt, started, _now(), summary=summary)
                self._write(status)
                return {**asdict(status), "elapsed_ms": self._elapsed_ms(started)}
            except Exception as exc:  # pragma: no cover - message preserved in tests
                last_error = str(exc)
                if attempt >= self.max_attempts:
                    failed = JobStatus("failed", attempt, started, _now(), error=last_error)
                    self._write(failed)
                    return {**asdict(failed), "elapsed_ms": self._elapsed_ms(started)}
                time.sleep(0.05 * attempt)
        return self.status()

    def _write(self, status: JobStatus) -> None:
        """Persist a job status snapshot."""
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO search_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (STATUS_KEY, json.dumps(asdict(status), sort_keys=True)),
            )
            conn.commit()

    @staticmethod
    def _elapsed_ms(started_at: str) -> int:
        """Return elapsed milliseconds from a persisted timestamp."""
        started = datetime.fromisoformat(started_at)
        return int((datetime.now(UTC) - started).total_seconds() * 1000)
