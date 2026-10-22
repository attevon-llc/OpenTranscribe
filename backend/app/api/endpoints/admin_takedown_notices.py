"""Platform-admin routes for takedown notice records (issue #1254).

A notice is the durable record of a legal notice (or of an operator acting with none),
the files it covers and what became of each. Recording a notice changes no file: taking a
file down is ``POST /admin/files/{uuid}/quarantine``, which must cite a notice here or
state that it had none.

Every route needs the admin tier. The queue and detail reads cover every tenant, so they
emit the same metadata-access audit event the quarantine queue does. Audit ``details`` and
logs carry ids, types and counts, never the basis, a reference or a reason.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_admin_user
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.core.constants import TAKEDOWN_EXPORT_MAX_ROWS
from app.core.constants import TAKEDOWN_EXPORT_RATE_LIMIT
from app.core.constants import TAKEDOWN_NOTICE_RATE_LIMIT
from app.core.enums import TakedownLinkStatus
from app.core.enums import TakedownNoticeType
from app.db.base import get_db
from app.models.takedown import TakedownNotice
from app.models.takedown import TakedownNoticeFile
from app.models.user import User
from app.schemas.takedown_notice import NotifyRequest
from app.schemas.takedown_notice import NotifyResponse
from app.schemas.takedown_notice import TakedownLinkPatch
from app.schemas.takedown_notice import TakedownNoticeCreate
from app.schemas.takedown_notice import TakedownNoticeList
from app.schemas.takedown_notice import TakedownNoticeOut
from app.services.platform_bypass import audit_metadata_access
from app.services.takedown_notice_queries import export_rows
from app.services.takedown_notice_queries import list_notices
from app.services.takedown_notice_queries import notices_out
from app.services.takedown_notice_service import CODE_LINK_NOT_FOUND
from app.services.takedown_notice_service import TakedownNoticeError
from app.services.takedown_notice_service import create_notice
from app.services.takedown_notice_service import find_notice
from app.services.takedown_notice_service import transition_link
from app.services.takedown_notification_service import notify_owners
from app.utils.csv_safe import render_csv

router = APIRouter()

_LIMIT_SCOPE = "takedown_notices"

_EXPORT_HEADER = (
    "notice_uuid",
    "notice_type",
    "received_at",
    "requester_reference",
    "basis",
    "recorded_by_authority",
    "handled_by_user_uuid",
    "link_uuid",
    "file_uuid",
    "owner_user_uuid",
    "organization_uuid",
    "owner_reason",
    "status",
    "quarantined_at",
    "counter_notice_received_at",
    "outcome_at",
    "outcome_note",
    "legal_hold_placed",
)


def takedown_notice_http_error(error: TakedownNoticeError) -> HTTPException:
    """The HTTP form of a refused notice operation: a coded detail with our own fixed text."""
    return HTTPException(
        status_code=error.status_code, detail={"code": error.code, "message": error.message}
    )


def _meta(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    return ip, request.headers.get("user-agent")


def _audit(event: AuditEventType, admin: User, request: Request, details: dict[str, Any]) -> None:
    ip, agent = _meta(request)
    audit_logger.log(
        event_type=event,
        outcome=AuditOutcome.SUCCESS,
        user_id=int(admin.id),
        username=str(admin.email),
        source_ip=ip,
        user_agent=agent,
        details=details,
    )


def _utc(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _export_record(notice: TakedownNotice, link: TakedownNoticeFile) -> list[Any]:
    return [
        str(notice.uuid),
        notice.notice_type,
        _utc(notice.received_at),
        notice.requester_reference,
        notice.basis,
        notice.recorded_by_authority,
        str(notice.handled_by_user_uuid) if notice.handled_by_user_uuid else None,
        str(link.uuid),
        str(link.file_uuid),
        str(link.owner_user_uuid),
        str(link.organization_uuid) if link.organization_uuid else None,
        link.owner_reason,
        link.status,
        _utc(link.quarantined_at),
        _utc(link.counter_notice_received_at),
        _utc(link.outcome_at),
        link.outcome_note,
        link.legal_hold_placed,
    ]


@router.post(
    "/takedown-notices", response_model=TakedownNoticeOut, status_code=status.HTTP_201_CREATED
)
@limiter.shared_limit(TAKEDOWN_NOTICE_RATE_LIMIT, scope=_LIMIT_SCOPE, key_func=user_or_ip_key)
def create_takedown_notice(
    body: TakedownNoticeCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Record a legal notice. No file is touched; cite it when quarantining."""
    notice = create_notice(
        db,
        handler=current_user,
        authority="platform",
        notice_type=body.notice_type,
        received_at=body.received_at,
        requester_reference=body.requester_reference,
        basis=body.basis,
    )
    db.commit()
    _audit(
        AuditEventType.ADMIN_TAKEDOWN_NOTICE_CREATE,
        current_user,
        request,
        {"notice_uuid": str(notice.uuid), "notice_type": notice.notice_type},
    )
    return notices_out(db, [notice])[0]


@router.get("/takedown-notices", response_model=TakedownNoticeList)
@limiter.shared_limit(TAKEDOWN_NOTICE_RATE_LIMIT, scope=_LIMIT_SCOPE, key_func=user_or_ip_key)
def list_takedown_notices(
    request: Request,
    response: Response,
    notice_status: Literal["open", "resolved"] | None = Query(None, alias="status"),
    notice_type: TakedownNoticeType | None = None,
    received_from: datetime | None = Query(None, alias="from"),
    received_to: datetime | None = Query(None, alias="to"),
    organization_uuid: UUID | None = None,
    file_uuid: UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """List notices, newest received first, with each notice's derived status."""
    rows, total = list_notices(
        db,
        status=notice_status,
        notice_type=notice_type.value if notice_type else None,
        received_from=received_from,
        received_to=received_to,
        organization_uuid=organization_uuid,
        file_uuid=file_uuid,
        limit=limit,
        offset=offset,
    )
    audit_metadata_access(
        db,
        current_user,
        route="/api/admin/takedown-notices",
        count=len(rows),
        organization_ids=(link.organization_id for n in rows for link in n.files),
    )
    return TakedownNoticeList(notices=notices_out(db, rows), total=total)


@router.get("/takedown-notices/export")
@limiter.limit(TAKEDOWN_EXPORT_RATE_LIMIT, key_func=user_or_ip_key)
def export_takedown_notices(
    request: Request,
    response: Response,
    export_format: Literal["csv", "json"] = Query("csv", alias="format"),
    received_from: datetime | None = Query(None, alias="from"),
    received_to: datetime | None = Query(None, alias="to"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Export one row per covered file for compliance reporting (does not need OpenSearch).

    Capped at ``TAKEDOWN_EXPORT_MAX_ROWS``; a partial export says so in
    ``X-Export-Truncated``. Every CSV cell is spreadsheet-formula neutralised.
    """
    rows, truncated = export_rows(
        db, received_from=received_from, received_to=received_to, cap=TAKEDOWN_EXPORT_MAX_ROWS
    )
    audit_metadata_access(
        db,
        current_user,
        route="/api/admin/takedown-notices/export",
        count=len(rows),
        organization_ids=(link.organization_id for _n, link in rows),
    )
    _audit(
        AuditEventType.ADMIN_TAKEDOWN_NOTICE_EXPORT,
        current_user,
        request,
        {
            "format": export_format,
            "rows": len(rows),
            "truncated": truncated,
            "from": _utc(received_from),
            "to": _utc(received_to),
        },
    )
    records = [_export_record(n, link) for n, link in rows]
    # The filename is built here from the date alone: nothing from a record reaches a header.
    filename = f"takedown-notices-{datetime.now(UTC):%Y%m%d}.{export_format}"
    if export_format == "json":
        body = json.dumps([dict(zip(_EXPORT_HEADER, r, strict=True)) for r in records], indent=2)
        media_type = "application/json"
    else:
        body = render_csv(_EXPORT_HEADER, records)
        media_type = "text/csv"
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Truncated": "true" if truncated else "false",
        },
    )


@router.get("/takedown-notices/{notice_uuid}", response_model=TakedownNoticeOut)
@limiter.shared_limit(TAKEDOWN_NOTICE_RATE_LIMIT, scope=_LIMIT_SCOPE, key_func=user_or_ip_key)
def get_takedown_notice(
    notice_uuid: UUID,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """One notice with every file it covers."""
    try:
        notice = find_notice(db, notice_uuid)
    except TakedownNoticeError as error:
        raise takedown_notice_http_error(error) from None
    audit_metadata_access(
        db,
        current_user,
        route="/api/admin/takedown-notices/{notice_uuid}",
        count=len(notice.files),
        organization_ids=(link.organization_id for link in notice.files),
    )
    return notices_out(db, [notice])[0]


@router.patch("/takedown-notices/{notice_uuid}/files/{file_uuid}", response_model=TakedownNoticeOut)
@limiter.shared_limit(TAKEDOWN_NOTICE_RATE_LIMIT, scope=_LIMIT_SCOPE, key_func=user_or_ip_key)
def update_takedown_notice_file(
    notice_uuid: UUID,
    file_uuid: UUID,
    body: TakedownLinkPatch,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Record a counter-notice or an upheld decision. The file's state does not change.

    ``released`` is not accepted: only the release action sets it, so the record cannot say
    a file was restored while it is still hidden.
    """
    try:
        notice = find_notice(db, notice_uuid)
        link = next((lk for lk in notice.files if lk.file_uuid == file_uuid), None)
        if link is None:
            raise TakedownNoticeError(CODE_LINK_NOT_FOUND, 404, "File not covered by this notice")
        transition_link(
            db,
            link,
            to=TakedownLinkStatus(body.status),
            received_at=body.received_at,
            note=body.note,
        )
    except TakedownNoticeError as error:
        raise takedown_notice_http_error(error) from None
    db.commit()
    _audit(
        AuditEventType.ADMIN_TAKEDOWN_NOTICE_UPDATE,
        current_user,
        request,
        {"notice_uuid": str(notice.uuid), "link_uuid": str(link.uuid), "status": link.status},
    )
    return notices_out(db, [notice])[0]


@router.post("/takedown-notices/{notice_uuid}/notify", response_model=NotifyResponse)
@limiter.shared_limit(TAKEDOWN_NOTICE_RATE_LIMIT, scope=_LIMIT_SCOPE, key_func=user_or_ip_key)
def notify_takedown_notice(
    notice_uuid: UUID,
    request: Request,
    response: Response,
    body: NotifyRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Email each affected owner once about this notice. Recipients come from the record.

    Idempotent: an owner already told about this event is skipped. The request carries no
    addresses and no user ids on purpose.
    """
    try:
        notice = find_notice(db, notice_uuid)
    except TakedownNoticeError as error:
        raise takedown_notice_http_error(error) from None
    event = (body or NotifyRequest()).event
    result = notify_owners(db, notice, event)
    _audit(
        AuditEventType.ADMIN_TAKEDOWN_NOTICE_NOTIFY,
        current_user,
        request,
        {"notice_uuid": str(notice.uuid), "event": event.value, **result.model_dump()},
    )
    return result
