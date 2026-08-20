"""Wire contracts for the document plane (#362 Stage 6d)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

#: Maps ``Document.status`` (reuses ``FileStatus``) to a human label. Only the four
#: values a document ever actually takes are listed — ``QUEUED``/``DOWNLOADING``/
#: ``CANCELLING``/``CANCELLED``/``ORPHANED``/``QUARANTINED`` are transcript-pipeline
#: states a document's lifecycle (pending -> processing -> completed/error) never enters.
_DISPLAY_STATUS: dict[str, str] = {
    "pending": "Pending",
    "processing": "Processing",
    "completed": "Ready",
    "error": "Failed",
}


def display_status(status: str) -> str:
    return _DISPLAY_STATUS.get(status, status.capitalize())


class DocumentResponse(BaseModel):
    """One document, list or detail shape — the fields are the same either way."""

    model_config = ConfigDict(from_attributes=True)

    uuid: UUID
    filename: str
    file_size: int
    content_type: str
    status: str
    display_status: str
    error_category: str | None = None
    last_error_message: str | None = None
    parser: str | None = None
    page_count: int | None = None
    word_count: int
    chunk_count: int
    language: str | None = None
    has_embedded_text: bool | None = None
    ocr_applied: bool
    parse_warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    parsed_at: datetime | None = None
    #: v399 (#362 lane C4) — the same takedown/legal-hold pair ``MediaFile`` exposes,
    #: so the gallery card and admin review queue render one shape for both.
    is_quarantined: bool = False
    legal_hold: bool = False


class DocumentListResponse(BaseModel):
    documents: list[DocumentResponse]
    total: int
    skip: int
    limit: int


class DocumentChunkResponse(BaseModel):
    """One ``DocumentChunk`` row — the detail view's evidence unit (provenance for a
    citation, source for a page-anchored jump in an ``<iframe>`` viewer).
    """

    model_config = ConfigDict(from_attributes=True)

    chunk_index: int
    text: str
    char_start: int
    char_end: int
    page: int | None = None
    section_path: list[str] | None = None
    block_types: list[str] | None = None


class DocumentChunkListResponse(BaseModel):
    chunks: list[DocumentChunkResponse]
    total: int


class DocumentQuarantineRequest(BaseModel):
    """Admin takedown request — mirrors ``admin.py``'s ``QuarantineRequest`` for media."""

    reason: str = Field(..., min_length=1, max_length=2000)
    legal_hold: bool = True


class DocumentReleaseRequest(BaseModel):
    clear_legal_hold: bool = True


class DocumentQuarantineActionResponse(BaseModel):
    uuid: UUID
    is_quarantined: bool
    legal_hold: bool
    status: str


class QuarantinedDocument(BaseModel):
    """One row in the admin takedown review queue — the document counterpart of
    ``admin.py``'s ``QuarantinedFile``.
    """

    uuid: UUID
    filename: str
    quarantine_reason: str | None = None
    quarantined_at: str | None = None
    legal_hold: bool = False


class QuarantinedDocumentsList(BaseModel):
    documents: list[QuarantinedDocument]
    total: int


class DocumentDownloadResponse(BaseModel):
    """A presigned, time-limited URL for the original uploaded file.

    ``inline`` (the default) omits the Content-Disposition override so a PDF renders
    in an ``<iframe>`` rather than triggering a save dialog; ``download=true`` on the
    request asks for ``attachment`` instead, for formats a browser cannot render.
    """

    url: str
    filename: str
    content_type: str
