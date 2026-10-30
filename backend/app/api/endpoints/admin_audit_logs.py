"""Admin reporting and audit-log routes (FedRAMP AU-2/AU-3)."""

import logging
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from sqlalchemy import false as sa_false
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth import get_current_admin_user
from app.auth.password_policy import password_expiry_cutoff
from app.core.config import settings
from app.db.base import get_db
from app.models.user import User
from app.models.user_mfa import UserMFA

# No basicConfig here — this module is imported via the API router before
# configure_logging() runs; a default root handler would double every log line.
logger = logging.getLogger(__name__)

router = APIRouter()


# ============== Reporting ==============


@router.get("/reports/account-status")
def get_account_status_report(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Account status summary for compliance reporting."""
    # Consolidate 3 User COUNT queries into 1 using FILTER clauses.
    # The expiry cutoff comes from the password policy rather than a local
    # timedelta(PASSWORD_MAX_AGE_DAYS): this report reimplemented the rule that
    # password_policy already owns, so with PASSWORD_POLICY_ENABLED=false it kept
    # reporting expired passwords that nothing would ever act on. A None cutoff
    # means expiry is not enforced, so nothing is expired.
    expiry_threshold = password_expiry_cutoff()
    expired_filter = User.password_changed_at < expiry_threshold if expiry_threshold else sa_false()
    user_row = db.query(
        func.count().label("total"),
        func.count().filter(User.is_active.is_(True)).label("active"),
        func.count().filter(expired_filter).label("pwd_expired"),
    ).one()

    # MFA is a separate table, so one more query
    mfa_enabled = db.query(func.count(UserMFA.id)).filter(UserMFA.totp_enabled.is_(True)).scalar()

    total = user_row.total
    active = user_row.active

    return {
        "total_users": total,
        "active_users": active,
        "inactive_users": total - active,
        "mfa_enabled_users": mfa_enabled,
        "password_expired_users": user_row.pwd_expired,
    }


# ============== Audit Logs (FedRAMP AU-2/AU-3) ==============


@router.get("/audit-logs")
def get_audit_logs(
    start_date: datetime | None = Query(None, description="Start date for log query"),
    end_date: datetime | None = Query(None, description="End date for log query"),
    event_type: str | None = Query(None, description="Filter by event type"),
    user_id: int | None = Query(None, description="Filter by user ID (the actor)"),
    target_user_id: int | None = Query(
        None, description="Filter by the affected/erased subject's user ID"
    ),
    outcome: str | None = Query(None, description="Filter by outcome"),
    limit: int = Query(default=100, le=1000, description="Maximum results"),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
):
    """
    Query audit logs with filtering. Super admin only (global, all tenants).

    Org-admins get a tenant-scoped view of the same data at
    ``GET /org-admin/audit-logs`` (see ``endpoints/org_admin.py``).

    ``user_id`` filters by the ACTOR who performed the action; ``target_user_id``
    filters by the SUBJECT the action was performed on (e.g. the erased user in a
    GDPR erasure) — see issue #443/#828 for the actor/subject convention.

    Note: This endpoint queries OpenSearch if audit logging to OpenSearch is
    enabled. If OpenSearch is not available, returns an error message.
    """
    from app.auth.audit import query_audit_logs

    return query_audit_logs(
        start_date=start_date,
        end_date=end_date,
        event_type=event_type,
        user_id=user_id,
        target_user_id=target_user_id,
        outcome=outcome,
        limit=limit,
        offset=offset,
    )


@router.get("/audit-logs/export")
def export_audit_logs(
    request: Request,
    export_format: str = Query("csv", description="Export format (csv or json)"),
    start_date: datetime | None = Query(None, description="Start date for export"),
    end_date: datetime | None = Query(None, description="End date for export"),
    current_user: User = Depends(get_current_active_superuser),
):
    """Export audit logs for compliance reporting. Super admin only (all tenants).

    Reads through ``query_audit_logs`` and renders with ``auth/audit_export``: the same two
    pieces the organization admin's export uses, so the columns and the CSV neutralisation
    are one implementation. Capped at ``AUDIT_EXPORT_MAX_ROWS`` newest events;
    ``X-Export-Truncated`` says when that cut something off. The export is itself audited.
    """
    from app.auth.audit import AuditEventType
    from app.auth.audit import AuditOutcome
    from app.auth.audit import audit_logger
    from app.auth.audit import query_audit_logs
    from app.auth.audit_export import audit_export_response
    from app.core.constants import AUDIT_EXPORT_MAX_ROWS
    from app.middleware.audit import get_request_context

    if export_format not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="Format must be csv or json")

    # Check if OpenSearch audit logging is enabled
    if not settings.AUDIT_LOG_TO_OPENSEARCH:
        raise HTTPException(
            status_code=400,
            detail="Audit log export requires AUDIT_LOG_TO_OPENSEARCH=true",
        )

    result = query_audit_logs(
        start_date=start_date, end_date=end_date, limit=AUDIT_EXPORT_MAX_ROWS, offset=0
    )
    if "error" in result:
        logger.error("Error exporting audit logs: %s", result["error"])
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        )

    logs = result["logs"]
    truncated = int(result["total"]) > len(logs)
    ctx_info = get_request_context(request)
    audit_logger.log(
        event_type=AuditEventType.AUDIT_EXPORT,
        outcome=AuditOutcome.SUCCESS,
        user_id=int(current_user.id),
        username=str(current_user.email),
        source_ip=ctx_info["source_ip"],
        user_agent=ctx_info["user_agent"],
        details={
            "scope": "platform",
            "format": export_format,
            "rows": len(logs),
            "truncated": truncated,
        },
    )
    return audit_export_response(
        logs, export_format, filename_prefix="audit-logs", truncated=truncated
    )
