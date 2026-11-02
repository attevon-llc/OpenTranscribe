"""``/admin/legal-processing-gate``: legal documents required before processing (super_admin).

``GET`` returns the configuration and the documents the active provider serves; ``PUT`` is a
partial update, audited as ``admin.settings.change`` with one event per changed setting. A key
the provider does not serve is a 422 ``legal_document_unavailable``: nothing is written, so a
typo can never turn the gate on with a requirement nobody can satisfy (or silently off).
Enforcement lives in ``app.legal.processing_gate``.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.db.base import get_db
from app.legal import processing_gate_admin
from app.legal.processing_gate import REASON_DOCUMENT_UNAVAILABLE
from app.schemas.legal_processing_gate import ProcessingGateSettings
from app.schemas.legal_processing_gate import ProcessingGateSettingsUpdate

router = APIRouter()


@router.get("", response_model=ProcessingGateSettings)
def get_processing_gate_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> ProcessingGateSettings:
    """The gate's configuration (super_admin)."""
    return processing_gate_admin.read_settings(db)


@router.put("", response_model=ProcessingGateSettings)
def put_processing_gate_settings(
    body: ProcessingGateSettingsUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> ProcessingGateSettings:
    """Partial update (super_admin). Audited as admin.settings.change."""
    unknown = processing_gate_admin.unserved_keys(body.required_document_keys or [])
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": REASON_DOCUMENT_UNAVAILABLE, "documents": unknown},
        )
    changes = processing_gate_admin.update_settings(
        db, required_keys=body.required_document_keys, scope=body.acceptance_scope
    )
    source_ip, user_agent = _get_client_info(request)
    for change in changes:
        audit_logger.log(
            event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
            outcome=AuditOutcome.SUCCESS,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=source_ip,
            user_agent=user_agent,
            details=change,
        )
    return processing_gate_admin.read_settings(db)
