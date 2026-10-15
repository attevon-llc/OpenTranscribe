"""Wire contract for support-access grants (issue #1122, plan section 6).

Every timestamp is ISO-8601 UTC with a ``Z`` suffix. ``status`` is computed server-side
from the grant's timestamps (the client never derives it), and ``server_time`` on every
page lets the client render countdowns without trusting its own clock.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

GrantStatus = Literal["pending", "active", "denied", "expired", "revoked", "lapsed"]
AccessLevel = Literal["read", "write"]
GrantMode = Literal["approved", "break_glass"]
TargetKind = Literal["organization", "personal"]


class UserRef(BaseModel):
    """A user as shown in a grant. The whole object is ``null`` once the account is gone."""

    uuid: str
    full_name: str | None = None
    email: str


class OrgRef(BaseModel):
    """An organization as shown in a grant. ``null`` once the organization is gone."""

    uuid: str
    name: str
    slug: str | None = None


class SupportGrantOut(BaseModel):
    uuid: str
    status: GrantStatus
    target_kind: TargetKind
    grant_mode: GrantMode
    access_level: AccessLevel
    organization: OrgRef | None = None
    subject_user: UserRef | None = None
    grantee: UserRef | None = None
    reason: str
    ticket_ref: str | None = None
    requested_duration_minutes: int
    requested_at: datetime
    pending_expires_at: datetime | None = None
    decided_by: UserRef | None = None
    decided_at: datetime | None = None
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_by: UserRef | None = None
    revoked_at: datetime | None = None


class GrantPage(BaseModel):
    items: list[SupportGrantOut]
    total: int
    server_time: datetime


class SupportGrantUseOut(BaseModel):
    occurred_at: datetime
    method: str
    route: str
    resource_type: str | None = None
    resource_uuid: str | None = None
    need: Literal["read", "write"] | None = None


class UsePage(BaseModel):
    items: list[SupportGrantUseOut]
    total: int
    server_time: datetime


class CreateGrantBody(BaseModel):
    """``POST /support-access/grants``. Exactly one of the two targets must be set."""

    organization_uuid: str | None = None
    subject_user_uuid: str | None = None
    access_level: AccessLevel
    reason: str = Field(min_length=10, max_length=2000)
    duration_minutes: int = Field(ge=15, le=480)


class BreakGlassBody(BaseModel):
    """``POST /support-access/grants/break-glass``. super_admin only; ticket is mandatory."""

    organization_uuid: str | None = None
    subject_user_uuid: str | None = None
    access_level: AccessLevel
    reason: str = Field(min_length=10, max_length=2000)
    ticket_ref: str = Field(min_length=1, max_length=255)
    duration_minutes: int = Field(default=60, ge=15, le=240)


class ApproveBody(BaseModel):
    """An approver may shorten the requested duration, never extend it."""

    duration_minutes: int | None = Field(default=None, ge=15, le=480)


class DecisionNoteBody(BaseModel):
    """Deny / revoke. The note goes to the audit event only, never to the grant row."""

    note: str | None = Field(default=None, max_length=500)


class OrgTargetOut(BaseModel):
    uuid: str
    name: str
    slug: str | None = None
