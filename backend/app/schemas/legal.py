"""Wire contract for ``/api/legal`` (versioned legal documents and acknowledgments)."""

from datetime import datetime

from pydantic import BaseModel
from pydantic import Field


class LegalDocumentSummary(BaseModel):
    """A served document plus the caller's acknowledgment state."""

    key: str
    title: str
    version: str
    status: str
    audience: str
    sha256: str
    requires_acknowledgment: bool
    acknowledged: bool
    acknowledged_at: datetime | None = None


class LegalDocumentDetail(BaseModel):
    """A served document with its full text."""

    key: str
    title: str
    version: str
    status: str
    sha256: str
    markdown: str


class LegalAcknowledgmentRequest(BaseModel):
    """What the caller saw: the server refuses it unless it matches what is served now."""

    key: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    sha256: str = Field(min_length=64, max_length=64)


class LegalAcknowledgmentRecord(BaseModel):
    """One stored acceptance."""

    key: str
    version: str
    sha256: str
    accepted_at: datetime


class LegalAcknowledgmentAdminRecord(LegalAcknowledgmentRecord):
    """An acceptance as listed to administrators."""

    user_email: str
