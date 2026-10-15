"""Wire contracts for the local Whisper model library (#1246).

``/admin/local-models`` lists the catalog with what is on disk, starts and cancels the one
download. Error codes are stable strings the SPA translates; the server's ``message`` is a fixed
English sentence and never carries exception text or a token.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

DownloadStatus = Literal["queued", "downloading", "verifying", "completed", "failed", "cancelled"]
LanguageSupport = Literal["multilingual", "english_only", "english_optimized"]


class LocalModelDownloadState(BaseModel):
    """Where the (single) model download stands."""

    model_id: str
    status: DownloadStatus
    bytes_done: int = 0
    bytes_total: int | None = None
    percent: float | None = None
    error_code: str | None = None
    updated_at: datetime


class LocalModelEntry(BaseModel):
    """One catalog model and its state in this deployment's cache."""

    id: str
    display_name: str
    repo_id: str
    approx_bytes: int
    vram_hint_gb: float
    language_support: LanguageSupport
    supports_translation: bool
    is_default: bool
    downloaded: bool
    partial: bool = Field(description="A download was interrupted and left partial files.")
    present_bytes: int
    is_active: bool = Field(description="The model the transcription workers are set to use.")


class LocalModelCatalogResponse(BaseModel):
    """The catalog, the download in flight (if any) and the cache's free space."""

    models: list[LocalModelEntry]
    download: LocalModelDownloadState | None = None
    free_bytes: int
    cache_writable: bool
    active_model: str


class LocalModelDownloadRequest(BaseModel):
    """Start downloading one catalog model."""

    model_id: str = Field(max_length=100)
