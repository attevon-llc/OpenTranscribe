"""Durable storage for end-to-end pipeline wall-clock timing.

Every transcription task writes a ``benchmark:{task_id}`` Redis hash when
``ENABLE_BENCHMARK_TIMING=true`` is set. That hash has a 24h TTL — fine for
live benchmarking, lost for historical analysis. At ``postprocess_end`` we
flush the hash into this table so week-over-week trends, p95 distributions,
and regression detection can be queried with plain SQL.

The table is purely additive — nothing in the pipeline depends on reading
from it. Columns use BIGINT epoch-milliseconds so timestamps can be
subtracted cheaply in SQL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class FilePipelineTiming(Base):
    """One row per completed transcription task_id.

    See ``docs/PIPELINE_TIMING.md`` for the canonical marker reference and
    interpretation notes. All ``*_ms`` columns are milliseconds since the
    Unix epoch so consumers can compute durations with simple subtraction.

    Pre-dispatch markers (client hash + HTTP ingress) are only populated
    when the upload went through the instrumented API endpoint; they are
    nullable for reprocess and background-dispatched tasks.
    """

    __tablename__ = "file_pipeline_timing"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media_file.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # --- Stage 0: client-side (optional, requires frontend instrumentation) ---
    client_hash_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    client_hash_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    client_put_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    client_put_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # --- Stage 1-5: API ingress (legacy flow only; null for reprocess) ---
    http_request_received_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    http_read_complete_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    http_validation_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    imohash_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    imohash_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    prepare_upload_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    minio_put_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    minio_put_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    thumbnail_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    thumbnail_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    db_commit_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    db_commit_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    http_response_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # URL-ingest path only (yt-dlp download wall-clock).
    url_download_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    url_download_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # --- Stage 6-10: CPU preprocess ---
    dispatch_timestamp_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    preprocess_task_prerun_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    media_download_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    media_download_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ffmpeg_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ffmpeg_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    temp_upload_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    temp_upload_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    preprocess_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # --- Stage 11-12: GPU transcribe + diarize ---
    gpu_received_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gpu_task_prerun_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gpu_audio_load_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gpu_audio_load_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gpu_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # --- Stage 13-15: CPU postprocess + completion ---
    postprocess_received_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    postprocess_task_prerun_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    postprocess_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    completion_notified_ms: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    # Terminal error marker — lets SQL filter error-mode rows cleanly.
    pipeline_error_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # --- Stage 17-22: async enrichment (fire-and-forget, end > completion) ---
    search_index_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    search_index_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    search_index_chunks_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    search_index_chunks_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    speaker_upsert_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    speaker_upsert_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    waveform_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    waveform_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    clustering_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    clustering_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    summary_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    summary_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    redaction_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    redaction_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # --- Derived (stored for query speed; recomputable from the above) ---
    user_perceived_duration_ms: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    fully_indexed_duration_ms: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )

    # --- Per-task context ---
    audio_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    whisper_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asr_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asr_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    redaction_detectors: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pii_entities_found: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gpu_device: Mapped[str | None] = mapped_column(String(128), nullable=True)
    http_flow: Mapped[str | None] = mapped_column(String(32), nullable=True)
    queue_depth_at_dispatch: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    concurrent_files_at_dispatch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_worker_cold: Mapped[str | None] = mapped_column(String(8), nullable=True)
    gpu_worker_cold: Mapped[str | None] = mapped_column(String(8), nullable=True)
    cpu_transcribe_worker_cold: Mapped[str | None] = mapped_column(String(8), nullable=True)
    retry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    per_retry_timings: Mapped[Any | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
