"""SQLite FTS5 + sqlite-vec store foundation."""
from __future__ import annotations
import sqlite3
import contextlib
import struct
from pathlib import Path
from typing import Iterable
import sqlite_vec
VECTOR_DIM = 512
TRANSCRIPT_VECTOR_DIM = 384
VECTOR_WARN_THRESHOLD = 100_000
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS search_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS transcript_chunk ("
    "id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, file_uuid TEXT NOT NULL, "
    "user_id INTEGER, source TEXT NOT NULL, title TEXT, speaker TEXT, "
    "start_time REAL, end_time REAL, upload_time TEXT, content_type TEXT, "
    "duration REAL, file_size INTEGER, language TEXT, content TEXT NOT NULL)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS transcript_chunk_fts USING fts5(content, title, speaker)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS speaker_vec USING vec0(embedding float[512])",
    "CREATE VIRTUAL TABLE IF NOT EXISTS transcript_vec USING vec0(embedding float[384])",
    "CREATE TABLE IF NOT EXISTS transcript_vec_meta ("
    "rowid INTEGER PRIMARY KEY, chunk_id INTEGER NOT NULL, file_id INTEGER NOT NULL, file_uuid TEXT NOT NULL, model TEXT)",
    "CREATE TABLE IF NOT EXISTS speaker_vec_meta ("
    "rowid INTEGER PRIMARY KEY, speaker_id INTEGER, profile_id INTEGER, name TEXT, model TEXT NOT NULL)",
)
class SQLiteSearchStore:
    """Owns the embedded SQLite search database."""
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
    def connect(self) -> sqlite3.Connection:
        """Open SQLite, enable WAL, load sqlite-vec, and ensure schema."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        self.ensure_schema(conn)
        return conn

    @staticmethod
    def ensure_schema(conn: sqlite3.Connection) -> None:
        """Create the foundation FTS/vector schema."""
        for statement in SCHEMA:
            conn.execute(statement)
        for name, kind in (
            ("user_id", "INTEGER"), ("upload_time", "TEXT"), ("content_type", "TEXT"),
            ("duration", "REAL"), ("file_size", "INTEGER"), ("language", "TEXT"),
        ):
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(f"ALTER TABLE transcript_chunk ADD COLUMN {name} {kind}")
        conn.commit()

    @staticmethod
    def vector_blob(values: Iterable[float]) -> bytes:
        """Pack exactly one 512-d float vector for sqlite-vec."""
        vector = tuple(float(item) for item in values)
        if len(vector) != VECTOR_DIM:
            raise ValueError(f"expected {VECTOR_DIM}-d vector, got {len(vector)}")
        return struct.pack(f"{VECTOR_DIM}f", *vector)

    @staticmethod
    def count_summary(conn: sqlite3.Connection) -> dict[str, int | bool]:
        """Return index counts and whether brute-force warning threshold is crossed."""
        chunks = int(conn.execute("SELECT count(*) FROM transcript_chunk").fetchone()[0])
        vectors = int(conn.execute("SELECT count(*) FROM speaker_vec_meta").fetchone()[0])
        transcript_vectors = int(conn.execute("SELECT count(*) FROM transcript_vec_meta").fetchone()[0])
        return {
            "text_chunks": chunks,
            "speaker_vectors": vectors,
            "transcript_vectors": transcript_vectors,
            "vector_warning": vectors > VECTOR_WARN_THRESHOLD,
        }

    @staticmethod
    def upsert_transcript_chunk(conn: sqlite3.Connection, row: dict) -> None:
        """Upsert one transcript/screen text row into content + FTS tables."""
        values = (
            row["id"], row["file_id"], row["file_uuid"], row.get("user_id"),
            row["source"], row.get("title"), row.get("speaker"), row.get("start_time"),
            row.get("end_time"), row.get("upload_time"), row.get("content_type"),
            row.get("duration"), row.get("file_size"), row.get("language"), row["content"],
        )
        conn.execute(
            "INSERT INTO transcript_chunk(id, file_id, file_uuid, user_id, source, title, speaker, "
            "start_time, end_time, upload_time, content_type, duration, file_size, language, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET file_id=excluded.file_id, "
            "file_uuid=excluded.file_uuid, user_id=excluded.user_id, source=excluded.source, title=excluded.title, "
            "speaker=excluded.speaker, start_time=excluded.start_time, end_time=excluded.end_time, "
            "upload_time=excluded.upload_time, content_type=excluded.content_type, "
            "duration=excluded.duration, file_size=excluded.file_size, "
            "language=excluded.language, content=excluded.content",
            values,
        )
        conn.execute("DELETE FROM transcript_chunk_fts WHERE rowid = ?", (row["id"],))
        conn.execute(
            "INSERT INTO transcript_chunk_fts(rowid, content, title, speaker) VALUES (?, ?, ?, ?)",
            (row["id"], row["content"], row.get("title"), row.get("speaker")),
        )

    def upsert_speaker_vector(self, conn: sqlite3.Connection, row: dict) -> None:
        """Upsert one 512-d speaker/profile vector."""
        rowid = int(row["rowid"])
        conn.execute("DELETE FROM speaker_vec WHERE rowid = ?", (rowid,))
        conn.execute(
            "INSERT INTO speaker_vec(rowid, embedding) VALUES (?, ?)",
            (rowid, self.vector_blob(row["embedding"])),
        )
        conn.execute(
            "INSERT INTO speaker_vec_meta(rowid, speaker_id, profile_id, name, model) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(rowid) DO UPDATE SET "
            "speaker_id=excluded.speaker_id, profile_id=excluded.profile_id, "
            "name=excluded.name, model=excluded.model",
            (rowid, row.get("speaker_id"), row.get("profile_id"), row.get("name"), row["model"]),
        )

    def search_speaker_vectors(self, conn: sqlite3.Connection, vector, limit: int = 5) -> list[dict]:
        """Return nearest speaker vectors from sqlite-vec."""
        rows = conn.execute(
            "SELECT m.rowid, m.speaker_id, m.profile_id, m.name, m.model, v.distance "
            "FROM speaker_vec v JOIN speaker_vec_meta m ON m.rowid = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (self.vector_blob(vector), limit),
        ).fetchall()
        keys = ("rowid", "speaker_id", "profile_id", "name", "model", "distance")
        return [dict(zip(keys, row)) for row in rows]

    @staticmethod
    def transcript_vector_blob(values: Iterable[float]) -> bytes:
        """Pack exactly one 384-d transcript vector."""
        vector = tuple(float(item) for item in values)
        if len(vector) != TRANSCRIPT_VECTOR_DIM:
            raise ValueError(f"expected {TRANSCRIPT_VECTOR_DIM}-d vector, got {len(vector)}")
        return struct.pack(f"{TRANSCRIPT_VECTOR_DIM}f", *vector)

    def upsert_transcript_vector(self, conn: sqlite3.Connection, row: dict) -> None:
        """Upsert one transcript semantic vector."""
        rowid = int(row["rowid"])
        conn.execute("DELETE FROM transcript_vec WHERE rowid = ?", (rowid,))
        conn.execute("INSERT INTO transcript_vec(rowid, embedding) VALUES (?, ?)",
                     (rowid, self.transcript_vector_blob(row["embedding"])))
        conn.execute(
            "INSERT INTO transcript_vec_meta(rowid, chunk_id, file_id, file_uuid, model) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(rowid) DO UPDATE SET "
            "chunk_id=excluded.chunk_id, file_id=excluded.file_id, "
            "file_uuid=excluded.file_uuid, model=excluded.model",
            (rowid, row["chunk_id"], row["file_id"], row["file_uuid"], row.get("model")),
        )

    def search_transcript_vectors(self, conn: sqlite3.Connection, vector, limit: int = 5, user_id: int | None = None) -> list[dict]:
        """Return nearest transcript vectors from sqlite-vec, optionally user-scoped."""
        rows = conn.execute(
            "SELECT m.rowid, m.chunk_id, m.file_id, m.file_uuid, m.model, v.distance, c.user_id "
            "FROM transcript_vec v JOIN transcript_vec_meta m ON m.rowid = v.rowid "
            "JOIN transcript_chunk c ON c.id = m.chunk_id "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (self.transcript_vector_blob(vector), limit * 3 if user_id is not None else limit),
        ).fetchall()
        keys = ("rowid", "chunk_id", "file_id", "file_uuid", "model", "distance", "user_id")
        result = [dict(zip(keys, row)) for row in rows]
        if user_id is not None:
            result = [row for row in result if row.get("user_id") == user_id]
        return result[:limit]

    @staticmethod
    def search_text(conn: sqlite3.Connection, query: str, limit: int = 10, user_id: int | None = None) -> list[dict]:
        """Return FTS5 ranked rows using bm25()."""
        where = "transcript_chunk_fts MATCH ?"
        params: list = [query]
        if user_id is not None:
            where += " AND c.user_id = ?"
            params.append(user_id)
        params.append(limit)
        rows = conn.execute(
            "SELECT c.file_id, c.file_uuid, c.user_id, c.source, c.title, c.speaker, c.start_time, "
            "c.end_time, c.upload_time, c.content_type, c.duration, c.file_size, c.language, "
            "c.content, bm25(transcript_chunk_fts) AS score "
            "FROM transcript_chunk_fts f JOIN transcript_chunk c ON c.id = f.rowid "
            f"WHERE {where} ORDER BY score LIMIT ?",
            tuple(params),
        ).fetchall()
        keys = ("file_id", "file_uuid", "user_id", "source", "title", "speaker", "start_time", "end_time", "upload_time", "content_type", "duration", "file_size", "language", "content", "score")
        return [dict(zip(keys, row)) for row in rows]

    def log_summary(self, conn: sqlite3.Connection) -> dict[str, int | bool]:
        """Persist and return current count summary for orchestrator status."""
        summary = self.count_summary(conn)
        for key, value in summary.items():
            conn.execute(
                "INSERT INTO search_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
        conn.commit()
        return summary
