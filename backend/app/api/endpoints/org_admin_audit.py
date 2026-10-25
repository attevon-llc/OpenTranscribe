"""``GET /org-admin/audit-logs/export``: an organization admin's audit download (issue #1277).

The same tenant-scoped read as ``GET /org-admin/audit-logs`` (the scope and the member guards
live once, in ``org_admin.org_audit_query``), rendered by the writer the super-admin export also
uses (``auth/audit_export``). It sits on its own router because it is gated by its own
capability, ``org.audit_logs``, mounted in ``api/router.py`` with no platform-admin bypass: a
platform account gets the same 404 as anyone while the key is off, and is not an organization
admin, so it never reaches the body.

Every export is itself an audit event (``audit.export``): the format, the row count and whether
it was truncated, never the rows.
"""

import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import require_org_admin
from app.api.endpoints.org_admin import org_audit_query
from app.api.endpoints.org_admin import org_id_of
from app.auth.audit import TENANT_SCOPE_ORG
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.audit import query_audit_logs
from app.auth.audit_export import DISPLAY_COLUMNS
from app.auth.audit_export import EXPORT_COLUMNS
from app.auth.audit_export import audit_export_response
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.core.constants import AUDIT_EXPORT_MAX_ROWS
from app.core.constants import ORG_AUDIT_EXPORT_RATE_LIMIT
from app.db.base import get_db
from app.middleware.audit import get_request_context
from app.services.org_audit_view import with_display_fields

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/export")
@limiter.limit(ORG_AUDIT_EXPORT_RATE_LIMIT, key_func=user_or_ip_key)
def export_org_audit_logs(
    request: Request,
    response: Response,
    export_format: Literal["csv", "json"] = Query("csv", alias="format"),
    start_date: datetime | None = Query(None, description="Start date for the export"),
    end_date: datetime | None = Query(None, description="End date for the export"),
    event_type: str | None = Query(None, description="Filter by event type"),
    user_id: int | None = Query(None, description="Filter by actor user ID (an org member)"),
    user_uuid: UUID | None = Query(None, description="Filter by actor uuid (an org member)"),
    target_user_id: int | None = Query(None, description="Filter by subject user ID (a member)"),
    target_user_uuid: UUID | None = Query(None, description="Filter by subject uuid (a member)"),
    outcome: str | None = Query(None, description="Filter by outcome"),
    resource_type: str | None = Query(None, max_length=64, description="Filter by resource type"),
    resource_uuid: UUID | None = Query(None, description="Filter by resource uuid"),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
):
    """Download the caller's organization's audit events, newest first, up to 10,000 rows.

    A larger result is cut at the cap and says so in ``X-Export-Truncated``; narrow the dates or
    the filters to get the rest. CSV cells are spreadsheet-formula neutralised. Names are
    resolved at read time inside this organization only (``actor_display`` and friends).
    """
    org_id = org_id_of(ctx)
    result = query_audit_logs(
        **org_audit_query(
            db,
            ctx,
            start_date=start_date,
            end_date=end_date,
            event_type=event_type,
            user_id=user_id,
            user_uuid=user_uuid,
            target_user_id=target_user_id,
            target_user_uuid=target_user_uuid,
            outcome=outcome,
            resource_type=resource_type,
            resource_uuid=resource_uuid,
        ),
        limit=AUDIT_EXPORT_MAX_ROWS,
        offset=0,
    )
    if "error" in result:
        # query_audit_logs words the failure generically; nothing of it is echoed.
        raise HTTPException(status_code=503, detail="Audit log export is unavailable.")

    logs = with_display_fields(db, org_id, int(ctx.user.id), result["logs"])
    truncated = int(result["total"]) > len(logs)

    ctx_info = get_request_context(request)
    audit_logger.log(
        event_type=AuditEventType.AUDIT_EXPORT,
        outcome=AuditOutcome.SUCCESS,
        user_id=int(ctx.user.id),
        username=str(ctx.user.email),
        source_ip=ctx_info["source_ip"],
        user_agent=ctx_info["user_agent"],
        organization_id=org_id,
        tenant_scope=TENANT_SCOPE_ORG,
        details={
            "scope": "organization",
            "format": export_format,
            "rows": len(logs),
            "truncated": truncated,
        },
    )
    return audit_export_response(
        logs,
        export_format,
        filename_prefix="audit-logs",
        columns=(*EXPORT_COLUMNS, *DISPLAY_COLUMNS),
        truncated=truncated,
    )
