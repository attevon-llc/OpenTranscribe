"""Wire contract for ``/api/legal`` (versioned legal documents and acknowledgments)."""

import uuid as uuid_pkg
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

#: BCP 47 shape (language, then subtags). Shared with the Accept-Language fallback.
LOCALE_PATTERN = r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$"


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
    organization_acknowledged: bool = False
    organization_acknowledged_at: datetime | None = None


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
    scope: Literal["user", "organization"] = "user"
    locale: str | None = Field(default=None, max_length=35, pattern=LOCALE_PATTERN)


class LegalAcknowledgmentRecord(BaseModel):
    """One stored acceptance."""

    key: str
    version: str
    sha256: str
    accepted_at: datetime
    scope: str = "user"
    organization_uuid: uuid_pkg.UUID | None = None


class LegalAcknowledgmentAdminRecord(LegalAcknowledgmentRecord):
    """An acceptance as listed to administrators."""

    user_email: str | None = None
