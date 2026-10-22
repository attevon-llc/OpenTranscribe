"""Wire contract for takedown notice records (issue #1254).

Two audiences, two shapes. The administrator sees the whole record. The file owner sees
only :class:`OwnerTakedownOut`: what they were told, when, and where to dispute it. The
internal ``basis``, the requester reference and the handler are not on that model at all,
so no serializer can leak them by accident.
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Annotated
from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import StringConstraints
from pydantic import field_validator

from app.core.enums import TakedownLinkStatus
from app.core.enums import TakedownNoticeType
from app.core.enums import TakedownNotifyEvent

#: A notice may be entered late but not from the future; a little skew is tolerated.
_RECEIVED_AT_SKEW = timedelta(minutes=5)

_Text2000 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


def _check_received_at(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("received_at must include a timezone offset")
    if value > datetime.now(UTC) + _RECEIVED_AT_SKEW:
        raise ValueError("received_at cannot be in the future")
    return value


class TakedownNoticeCreate(BaseModel):
    """Record a legal notice. This changes no file's state."""

    notice_type: TakedownNoticeType
    received_at: datetime
    requester_reference: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ] = Field(
        ...,
        description="An opaque case number, ticket id or complainant organization. Never contact details.",
    )
    basis: _Text2000 = Field(..., description="The internal legal basis. Never shown to the owner.")

    @field_validator("notice_type")
    @classmethod
    def _real_notices_only(cls, value: TakedownNoticeType) -> TakedownNoticeType:
        if value is TakedownNoticeType.OPERATOR_ACTION:
            raise ValueError("an operator action is recorded by quarantining with a reason")
        return value

    @field_validator("received_at")
    @classmethod
    def _not_in_the_future(cls, value: datetime) -> datetime:
        return _check_received_at(value)


class TakedownLinkPatch(BaseModel):
    """Record a counter-notice or an upheld decision for one file under a notice."""

    status: Literal[TakedownLinkStatus.COUNTER_NOTICE, TakedownLinkStatus.UPHELD]
    received_at: datetime | None = Field(
        None, description="When the counter-notice arrived (default: now)."
    )
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None

    @field_validator("received_at")
    @classmethod
    def _not_in_the_future(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _check_received_at(value)


class TakedownLinkOut(BaseModel):
    """One file under a notice, as the administrator sees it."""

    uuid: str
    file_uuid: str
    #: Live title while the file exists; ``None`` once it has been purged.
    file_title: str | None = None
    file_exists: bool
    owner_user_uuid: str
    organization_uuid: str | None = None
    owner_reason: str
    status: str
    quarantined_at: datetime
    counter_notice_received_at: datetime | None = None
    outcome_at: datetime | None = None
    outcome_note: str | None = None
    legal_hold_placed: bool


class TakedownNoticeOut(BaseModel):
    """A notice with its files; ``status`` is derived from the links."""

    uuid: str
    notice_type: str
    received_at: datetime
    requester_reference: str | None = None
    basis: str
    recorded_by_authority: str
    handled_by_user_uuid: str | None = None
    created_at: datetime
    status: Literal["open", "resolved"]
    links: list[TakedownLinkOut] = Field(default_factory=list)


class TakedownNoticeList(BaseModel):
    """A page of notices, newest received first."""

    notices: list[TakedownNoticeOut]
    total: int


class NotifyRequest(BaseModel):
    """Tell the affected owners about a notice's takedowns (or releases)."""

    event: TakedownNotifyEvent = TakedownNotifyEvent.QUARANTINED


class NotifyResponse(BaseModel):
    """What one notify call did. Counts only: no address is ever echoed."""

    sent: int
    failed: int
    skipped_no_transport: int
    already_notified: int


class OwnerTakedownOut(BaseModel):
    """What the owner of a taken-down file is told."""

    link_uuid: str
    file_uuid: str
    file_title: str | None = None
    notice_type: str
    received_at: datetime
    owner_reason: str
    status: str
    quarantined_at: datetime
    outcome_at: datetime | None = None
    #: ``operator`` or ``organization``: who a counter-notice goes to.
    dispute_to: Literal["operator", "organization"]
    abuse_contact_email: str | None = None


class OwnerTakedownList(BaseModel):
    """The caller's own takedown history."""

    takedowns: list[OwnerTakedownOut]
    total: int
