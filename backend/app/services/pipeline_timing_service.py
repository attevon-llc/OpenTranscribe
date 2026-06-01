"""Flush a ``benchmark:{task_id}`` Redis hash into ``file_pipeline_timing``.

Called from ``finalize_transcription`` (postprocess.py) when benchmark timing
is enabled. Best-effort — any failure here is logged at DEBUG and never
propagates; the pipeline does not depend on this row being written.

The Redis hash stores values as strings; we convert to the appropriate types
here and ignore unknown keys (forward compatibility with new markers).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.session_utils import session_scope
from app.models.pipeline_timing import FilePipelineTiming
from app.utils import benchmark_timing

logger = logging.getLogger(__name__)

# Markers that should be stored as float seconds in Redis and persisted as
# epoch-ms in the database. Anything not in this set is treated as a context
# field (string / int / JSON).
_TIMESTAMP_MARKERS: tuple[str, ...] = (
    "client_hash_start",
    "client_hash_end",
    "client_put_start",
    "client_put_end",
    "http_request_received",
    "http_read_complete",
    "http_validation_end",
    "imohash_start",
    "imohash_end",
    "prepare_upload_end",
    "minio_put_start",
    "minio_put_end",
    "thumbnail_start",
    "thumbnail_end",
    "db_commit_start",
    "db_commit_end",
    "http_response_end",
    "url_download_start",
    "url_download_end",
    "dispatch_timestamp",
    "preprocess_task_prerun",
    "media_download_start",
    "media_download_end",
    "ffmpeg_start",
    "ffmpeg_end",
    "metadata_start",
    "metadata_end",
    "temp_upload_start",
    "temp_upload_end",
    "preprocess_end",
    "gpu_received",
    "gpu_task_prerun",
    "gpu_audio_load_start",
    "gpu_audio_load_end",
    "gpu_end",
    "postprocess_received",
    "postprocess_task_prerun",
    "postprocess_end",
    "completion_notified",
    "pipeline_error_end",
    "search_index_start",
    "search_index_end",
    "search_index_chunks_start",
    "search_index_chunks_end",
    "speaker_upsert_start",
    "speaker_upsert_end",
    "waveform_start",
    "waveform_end",
    "clustering_start",
    "clustering_end",
    "summary_start",
    "summary_end",
    "redaction_start",
    "redaction_end",
)

_CONTEXT_STRING_KEYS: tuple[str, ...] = (
    "content_type",
    "whisper_model",
    "asr_provider",
    "asr_model",
    "redaction_detectors",
    "gpu_device",
    "http_flow",
    "cpu_worker_cold",
    "gpu_worker_cold",
    "cpu_transcribe_worker_cold",
)
_CONTEXT_INT_KEYS: tuple[str, ...] = (
    "file_size_bytes",
    "concurrent_files_at_dispatch",
    "retry_count",
    "pii_entities_found",
)
_CONTEXT_FLOAT_KEYS: tuple[str, ...] = ("audio_duration_s",)
_CONTEXT_JSON_KEYS: tuple[str, ...] = ("queue_depth_at_dispatch", "per_retry_timings")


def _to_ms(value: str) -> int | None:
    """Convert a Redis hash value (string of float seconds) to epoch-ms."""
    try:
        return int(float(value) * 1000)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_json(value: str) -> Any | None:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _extract_timestamps(raw: dict[str, str]) -> dict[str, Any]:
    """Parse every timestamp marker (float seconds → epoch-ms column)."""
    out: dict[str, Any] = {}
    for marker in _TIMESTAMP_MARKERS:
        if marker in raw:
            ms = _to_ms(raw[marker])
            if ms is not None:
                out[f"{marker}_ms"] = ms
    return out


def _extract_context(raw: dict[str, str]) -> dict[str, Any]:
    """Parse context columns (strings, ints, floats, JSON blobs)."""
    out: dict[str, Any] = {}
    for str_key in _CONTEXT_STRING_KEYS:
        if str_key in raw and raw[str_key] != "":
            out[str_key] = raw[str_key]
    for int_key in _CONTEXT_INT_KEYS:
        if int_key in raw:
            int_val = _coerce_int(raw[int_key])
            if int_val is not None:
                out[int_key] = int_val
    for float_key in _CONTEXT_FLOAT_KEYS:
        if float_key in raw:
            float_val = _coerce_float(raw[float_key])
            if float_val is not None:
                out[float_key] = float_val
    for json_key in _CONTEXT_JSON_KEYS:
        if json_key in raw:
            json_val = _coerce_json(raw[json_key])
            if json_val is not None:
                out[json_key] = json_val
    return out


def _compute_derived_durations(row: dict[str, Any]) -> dict[str, int]:
    """Derive user-perceived and fully-indexed durations from parsed markers."""
    out: dict[str, int] = {}
    http_start_ms = row.get("http_request_received_ms") or row.get("dispatch_timestamp_ms")
    completion_ms = row.get("completion_notified_ms") or row.get("postprocess_end_ms")

    if http_start_ms and completion_ms and completion_ms >= http_start_ms:
        out["user_perceived_duration_ms"] = completion_ms - http_start_ms

    async_end_candidates: list[int] = []
    for k in (
        "search_index_chunks_end_ms",
        "search_index_end_ms",
        "speaker_upsert_end_ms",
        "waveform_end_ms",
        "clustering_end_ms",
        "summary_end_ms",
        "postprocess_end_ms",
    ):
        val = row.get(k)
        if isinstance(val, int):
            async_end_candidates.append(val)
    if http_start_ms and async_end_candidates:
        fully = max(async_end_candidates)
        if fully >= http_start_ms:
            out["fully_indexed_duration_ms"] = fully - http_start_ms
    return out


def build_row_payload(raw: dict[str, str]) -> dict[str, Any]:
    """Transform a Redis hash dict into a ``FilePipelineTiming`` row payload.

    Public so the admin endpoint can reuse it without touching the DB.
    """
    row: dict[str, Any] = {}
    row.update(_extract_timestamps(raw))
    row.update(_extract_context(raw))
    row.update(_compute_derived_durations(row))
    return row


def record_pipeline_timing(
    task_id: str,
    file_id: int,
    user_id: int | None = None,
) -> bool:
    """Flush the Redis timing hash for ``task_id`` into ``file_pipeline_timing``.

    Returns True when a row is written, False on no-op/failure. Safe to call
    multiple times; uses an ON CONFLICT DO UPDATE upsert keyed on task_id so
    retries and error-path flushes merge cleanly.
    """
    raw = benchmark_timing.fetch_all(task_id)
    if not raw:
        return False

    payload = build_row_payload(raw)
    payload["task_id"] = task_id
    payload["file_id"] = file_id
    if user_id is not None:
        payload["user_id"] = user_id

    try:
        with session_scope() as db:
            stmt = pg_insert(FilePipelineTiming).values(**payload)
            update_cols = {
                c: stmt.excluded[c] for c in payload if c not in ("task_id", "file_id", "user_id")
            }
            if update_cols:
                stmt = stmt.on_conflict_do_update(
                    index_elements=["task_id"],
                    set_=update_cols,
                )
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=["task_id"])
            db.execute(stmt)
            db.commit()
        return True
    except Exception as e:
        logger.debug(f"record_pipeline_timing({task_id}) failed: {e}")
        return False


def derived_durations(raw: dict[str, str]) -> dict[str, int]:
    """Extract user-perceived and fully-indexed durations from a Redis hash.

    Returns a dict with whichever of ``user_perceived_duration_ms`` and
    ``fully_indexed_duration_ms`` can be computed. Used by the admin endpoint.
    """
    row = build_row_payload(raw)
    return {
        k: v
        for k, v in row.items()
        if k in ("user_perceived_duration_ms", "fully_indexed_duration_ms")
    }


def now_ms() -> int:
    """Current wall-clock as epoch-ms — handy for dashboard consumers."""
    return int(time.time() * 1000)
