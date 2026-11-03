"""Wire contract of the erasure receipt API (issue #1353): counts keyed by store name, never
personal data. No subject id is exposed; the ledger uuid is the reference."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class ErasureReceiptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    uuid: UUID
    ledger_uuid: UUID
    subject_type: str
    attempt: int
    phase: str
    generated_at: datetime
    outcome: str
    deleted: dict[str, int]
    de_attributed: dict[str, int]
    retained: dict[str, int]
    ttl: dict[str, int]
    residual: dict[str, int]
    unverified: dict[str, int]
    inventory_version: int
    digest: str
    #: Why each retained or de-attributed store keeps data: a fixed sentence per key.
    retained_reasons: dict[str, str] = Field(default_factory=dict)


class ErasureLedgerItem(BaseModel):
    uuid: UUID
    subject_type: str
    status: str
    actor_kind: str
    requested_at: datetime
    sla_due_at: datetime
    completed_at: datetime | None
    attempts: int
    latest_receipt_outcome: str | None


class ErasureLedgerPage(BaseModel):
    items: list[ErasureLedgerItem]
    total: int
    page: int
    per_page: int


class IgnoredIndices(BaseModel):
    """Index names an operator classified as holding no personal data (names only)."""

    names: list[str] = Field(default_factory=list, max_length=200)
