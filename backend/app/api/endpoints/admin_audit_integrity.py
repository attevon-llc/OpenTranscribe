"""Whether the application database role can really not rewrite audit records (super_admin).

``GET /admin/security/audit-integrity`` reports the MEASURED state of the role this request's
database connection runs as (``has_table_privilege`` and friends), not the configuration. Role
names are never returned, only fixed booleans and the protected table names.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth.dependencies import get_current_active_superuser
from app.db.base import get_db
from app.db.role_check import measure_role

router = APIRouter()


class AuditTableIntegrity(BaseModel):
    """Measured privileges of the runtime role on one protected table."""

    name: str
    insert: bool
    update_columns: list[str]
    delete: bool
    truncate: bool
    guard_trigger_present: bool


class AuditIntegrityStatus(BaseModel):
    """Measured audit-record protection."""

    split_requested: bool
    enforced: bool
    runtime_role_is_superuser: bool
    runtime_role_owns_tables: bool
    tables: list[AuditTableIntegrity]


@router.get("", response_model=AuditIntegrityStatus)
def get_audit_integrity(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> AuditIntegrityStatus:
    """Measured state of audit-record protection for this connection's role."""
    state = measure_role(db.connection())
    return AuditIntegrityStatus(
        split_requested=state.split_requested,
        enforced=state.enforced,
        runtime_role_is_superuser=state.is_superuser,
        runtime_role_owns_tables=state.owns_tables,
        tables=[
            AuditTableIntegrity(
                name=t.name,
                insert=t.insert,
                update_columns=t.update_columns,
                delete=t.delete,
                truncate=t.truncate,
                guard_trigger_present=t.guard_trigger_present,
            )
            for t in state.tables
        ],
    )
