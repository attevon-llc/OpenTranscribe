"""Wire contract of the DOCX / PDF transcript export (issue #1278)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class DocumentExportLabels(BaseModel):
    """Already-translated strings the document prints (the backend stays translation-free).

    ``page_label`` and ``exported_label`` are templates: the server substitutes ``{page}`` /
    ``{pages}`` / ``{date}`` with ``str.replace``, never ``str.format``, so any other brace is
    printed as typed.
    """

    model_config = ConfigDict(extra="forbid")

    speaker_default_label: str = Field("Speaker", max_length=200)
    user_comment_label: str = Field("USER COMMENT", max_length=200)
    page_label: str = Field("Page {page} of {pages}", max_length=200)
    exported_label: str = Field("Exported {date}", max_length=200)
    redacted_label: str = Field("Redacted", max_length=200)


class TranscriptDocumentExportRequest(BaseModel):
    """What to put in the document and how to number it."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["docx", "pdf"]
    include_comments: bool = False
    include_timestamps: bool = True
    include_speakers: bool = True
    line_numbering: Literal["per_page", "continuous", "none"] = "per_page"
    page_size: Literal["a4", "letter"] = "a4"
    #: A REQUEST for the original text. Granted to the owner only, and never under the admin
    #: ``export_locked`` floor.
    reveal_original: bool = False
    labels: DocumentExportLabels = Field(default_factory=lambda: DocumentExportLabels())


class DocumentExportAccepted(BaseModel):
    """The job was queued (or an identical one is already running)."""

    status: Literal["processing"]
    #: 64 hex characters, HMAC-bound to the requesting user; open it on
    #: ``GET /files/bulk-export-stream?job=``.
    job_id: str
