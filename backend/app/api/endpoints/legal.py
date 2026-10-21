"""Versioned legal documents and administrator acknowledgments.

The text is vendored from the central legal repository (``app/legal``) and is served
unmodified; the SHA-256 of the exact bytes identifies what an acceptance covers.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_current_admin_user
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.roles import ROLE_SUPER_ADMIN
from app.db.base import get_db
from app.legal import acceptance
from app.legal import registry
from app.models.legal_acceptance import LegalAcceptance
from app.models.user import User
from app.schemas.legal import LegalAcknowledgmentAdminRecord
from app.schemas.legal import LegalAcknowledgmentRecord
from app.schemas.legal import LegalAcknowledgmentRequest
from app.schemas.legal import LegalDocumentDetail
from app.schemas.legal import LegalDocumentSummary

router = APIRouter()


@router.get("/documents", response_model=list[LegalDocumentSummary])
def list_legal_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> list[LegalDocumentSummary]:
    """Every served document with the caller's acknowledgment state for its current text."""
    out: list[LegalDocumentSummary] = []
    for doc in registry.list_documents():
        row = acceptance.find_acceptance(db, current_user.id, doc)
        out.append(
            LegalDocumentSummary(
                key=doc.key,
                title=doc.title,
                version=doc.version,
                status=doc.status,
                audience=doc.audience,
                sha256=doc.sha256,
                requires_acknowledgment=registry.requires_acknowledgment(doc.key),
                acknowledged=row is not None,
                acknowledged_at=row.accepted_at if row is not None else None,
            )
        )
    return out


@router.get("/documents/{key}", response_model=LegalDocumentDetail)
def get_legal_document(
    key: str,
    current_user: User = Depends(get_current_active_user),
) -> LegalDocumentDetail:
    """One document with its full markdown."""
    doc = registry.get_document(key)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return LegalDocumentDetail(
        key=doc.key,
        title=doc.title,
        version=doc.version,
        status=doc.status,
        sha256=doc.sha256,
        markdown=doc.markdown,
    )


@router.post("/acknowledgments", response_model=LegalAcknowledgmentRecord)
def acknowledge_legal_document(
    body: LegalAcknowledgmentRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> LegalAcknowledgmentRecord:
    """Record that the caller accepted the exact text now served for ``key``.

    Only a super_admin may acknowledge the operator acknowledgment. The request must carry
    the version and hash the server serves now (409 otherwise), so a stale page cannot
    accept text it never showed. Repeating the same acceptance returns the stored record.
    """
    doc = registry.get_document(body.key)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if body.key == registry.OPERATOR_ACKNOWLEDGMENT_KEY and current_user.role != ROLE_SUPER_ADMIN:
        raise HTTPException(
            status_code=403, detail="Only a super administrator may acknowledge this document"
        )
    if body.version != doc.version or body.sha256.lower() != doc.sha256:
        raise HTTPException(
            status_code=409,
            detail="The document has changed; reload it and review the current text",
        )
    client_ip, user_agent = _get_client_info(request)
    row, created = acceptance.record_acceptance(db, current_user.id, doc, client_ip)
    if created:
        audit_logger.log(
            event_type=AuditEventType.LEGAL_ACKNOWLEDGED,
            outcome=AuditOutcome.SUCCESS,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=client_ip,
            user_agent=user_agent,
            details={"document_key": doc.key, "version": doc.version, "sha256": doc.sha256},
        )
    return LegalAcknowledgmentRecord(
        key=row.document_key, version=row.version, sha256=row.sha256, accepted_at=row.accepted_at
    )


@router.get("/acknowledgments", response_model=list[LegalAcknowledgmentAdminRecord])
def list_legal_acknowledgments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> list[LegalAcknowledgmentAdminRecord]:
    """Every stored acceptance, newest first, with the accepting user's email."""
    rows = db.execute(
        select(LegalAcceptance, User.email)
        .join(User, User.id == LegalAcceptance.user_id)
        .order_by(LegalAcceptance.accepted_at.desc(), LegalAcceptance.id.desc())
    ).all()
    return [
        LegalAcknowledgmentAdminRecord(
            key=r.document_key,
            version=r.version,
            sha256=r.sha256,
            accepted_at=r.accepted_at,
            user_email=str(email),
        )
        for r, email in rows
    ]
