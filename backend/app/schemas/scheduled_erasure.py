"""Wire contract of a scheduled organization or member erasure (issue #1105).

``POST /org-admin/gdpr/erase-user/{uuid}`` and ``POST /org-admin/gdpr/erase-organization`` answer
**202** with :class:`ScheduledErasureReceipt` (before v0.7.0 they erased at once and answered 200
with the erasure summary). ``GET /org-admin/gdpr/erasures`` lists the open ones;
``POST /org-admin/gdpr/erasures/{ledger_uuid}/cancel`` withdraws one.

Error bodies carry ``detail = {"code": ..., "message": ...}``: ``reauth_required`` (403),
``reauth_unavailable``, ``request_exists``, ``already_executing``, ``not_cancellable`` (409),
``ledger_unavailable`` (503).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from pydantic import SecretStr


class OrgErasureConfirm(BaseModel):
    """Re-authentication for the request: the caller's password or a current TOTP code."""

    password: SecretStr | None = Field(default=None, max_length=1024)
    mfa_code: str | None = Field(default=None, max_length=32)


class ScheduledErasureReceipt(BaseModel):
    """The request receipt: ids and times only, no personal data."""

    ledger_uuid: UUID
    subject_type: Literal["org_member", "organization"]
    #: The member whose data will be erased (``org_member`` only).
    subject_user_uuid: UUID | None = None
    requested_at: datetime
    scheduled_for: datetime | None = None
    sla_due_at: datetime
    request_digest: str | None = None
    status: Literal["scheduled", "cancelled"]
    cancelled_at: datetime | None = None


class ScheduledErasureItem(ScheduledErasureReceipt):
    """One open scheduled erasure in the organization's list."""

    #: The requesting admin's display name, resolved at read time and only while they are a
    #: member of this organization; the ledger stores no names.
    requested_by: str | None = None
