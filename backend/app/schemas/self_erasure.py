"""Wire contract of the self-service erasure request (issue #1354).

``GET|POST /users/me/erasure-request``, ``DELETE /users/me/erasure-request/{ledger_uuid}`` for the
account holder, and ``GET|PUT /admin/self-erasure`` for the instance settings (super_admin): the self-service switch and
grace period, and the delay of an organization admin's scheduled erasure (issue #1105).

``GET`` also reports ``last_outcome`` when the account holder's latest request was closed by the
system without erasing (``blocked`` / ``last_super_admin``).

Error bodies carry ``detail = {"code": ..., "message": ...}``: ``last_super_admin``,
``request_exists``, ``reauth_required``, ``reauth_unavailable``, ``already_executing``,
``not_cancellable``, ``ledger_unavailable``. A disabled feature answers 404 on POST and DELETE so
it is not advertised; ``GET`` reports ``enabled: false``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr


class SelfErasureRequestReceipt(BaseModel):
    """The request receipt: ids and times only, no personal data."""

    model_config = ConfigDict(from_attributes=True)

    ledger_uuid: UUID
    requested_at: datetime
    scheduled_for: datetime | None = None
    sla_due_at: datetime
    request_digest: str
    status: Literal["scheduled", "cancelled"]
    cancelled_at: datetime | None = None


class SelfErasureOutcome(BaseModel):
    """A request the system closed without erasing anything. Codes only, no personal data."""

    status: Literal["blocked"]
    reason: Literal["last_super_admin"]
    ledger_uuid: UUID
    #: When the blocked request fell due (its ``scheduled_for``).
    at: datetime | None = None


class SelfErasureStatus(BaseModel):
    enabled: bool
    grace_days: int
    #: Whether this account can re-authenticate (password or MFA), so the UI can say so up front.
    can_reauthenticate: bool
    request: SelfErasureRequestReceipt | None = None
    #: Set only when there is no open request and the latest one was blocked by the system.
    last_outcome: SelfErasureOutcome | None = None


class SelfErasureCreate(BaseModel):
    confirm: Literal["ERASE"]
    password: SecretStr | None = Field(default=None, max_length=1024)
    mfa_code: str | None = Field(default=None, max_length=32)


class SelfErasureSettings(BaseModel):
    enabled: bool
    grace_days: int = Field(ge=0, le=21)
    max_grace_days: int = 21
    #: How long an organization admin's scheduled erasure waits (issue #1105), in hours.
    org_erasure_delay_hours: int = Field(ge=24, le=168)
    org_member_erasure_delay_hours: int = Field(ge=24, le=168)
    #: Grace days before an administrator-removed member's personal files are erased (#1104).
    removed_member_grace_days: int = Field(default=14, ge=0, le=21)
    min_org_erasure_delay_hours: int = 24
    max_org_erasure_delay_hours: int = 168


class SelfErasureSettingsUpdate(BaseModel):
    enabled: bool | None = None
    grace_days: int | None = Field(default=None, ge=0, le=21)
    org_erasure_delay_hours: int | None = Field(default=None, ge=24, le=168)
    org_member_erasure_delay_hours: int | None = Field(default=None, ge=24, le=168)
    removed_member_grace_days: int | None = Field(default=None, ge=0, le=21)
