"""Versioned legal documents and administrator acknowledgments.

The text is vendored from the central legal repository (``app/legal``) and is served
unmodified; the SHA-256 of the exact bytes identifies what an acceptance covers.
"""

from __future__ import annotations

import re
import uuid as uuid_pkg

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_SUPER_ADMIN
from app.db.base import get_db
from app.legal import acceptance
from app.legal import registry
from app.models.legal_acceptance import LegalAcceptance
from app.models.organization import Organization
from app.models.user import User
from app.schemas.legal import LOCALE_PATTERN
from app.schemas.legal import LegalAcknowledgmentAdminRecord
from app.schemas.legal import LegalAcknowledgmentRecord
from app.schemas.legal import LegalAcknowledgmentRequest
from app.schemas.legal import LegalDocumentDetail
from app.schemas.legal import LegalDocumentSummary

router = APIRouter()

_LOCALE_RE = re.compile(LOCALE_PATTERN)
_ORG_ADMIN = "org:admin"


def _coded(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _org_uuid(db: Session, org_id: int | None) -> uuid_pkg.UUID | None:
    if org_id is None:
        return None
    return db.execute(select(Organization.uuid).where(Organization.id == org_id)).scalar_one()


def _audit_details(row: LegalAcceptance) -> dict[str, str]:
    """Strings only: other emitters index ``details.scope`` as a string (see privacy purge).

    ``acceptance_uuid`` links the event to the row without copying its IP or email again.
    An unset locale is omitted, not written as null (``details`` is dynamically mapped).
    """
    details = {
        "document_key": row.document_key,
        "version": row.version,
        "sha256": row.sha256,
        "scope": row.scope,
        "acceptance_uuid": str(row.uuid),
    }
    if row.locale:
        details["locale"] = row.locale
    return details


def _header_locale(request: Request) -> str | None:
    """The first ``Accept-Language`` tag, when it is a valid BCP 47 shape; else ``None``."""
    raw = request.headers.get("accept-language", "")
    first = raw.split(",", 1)[0].split(";", 1)[0].strip()
    if first and len(first) <= 35 and _LOCALE_RE.match(first):
        return first
    return None


@router.get("/documents", response_model=list[LegalDocumentSummary])
def list_legal_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> list[LegalDocumentSummary]:
    """Every served document with the caller's (and the caller's org's) acknowledgment state."""
    out: list[LegalDocumentSummary] = []
    org_uuid = _org_uuid(db, ctx.org_id)
    for doc in registry.list_documents():
        row = acceptance.find_acceptance(db, current_user.id, doc)
        org_row = (
            acceptance.find_org_acceptance(db, org_uuid, doc) if org_uuid is not None else None
        )
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
                organization_acknowledged=org_row is not None,
                organization_acknowledged_at=org_row.accepted_at if org_row is not None else None,
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
    ctx: RequestContext = Depends(get_current_context),
) -> LegalAcknowledgmentRecord:
    """Record that the caller accepted the exact text now served for ``key``.

    Only a super_admin may acknowledge the operator acknowledgment. The request must carry
    the version and hash the server serves now (409 otherwise), so a stale page cannot
    accept text it never showed. Repeating the same acceptance returns the stored record.

    ``scope='organization'``: an org admin accepts on behalf of the organization the request
    is acting in. Not available for the operator acknowledgment, nor under a support grant.
    """
    doc = registry.get_document(body.key)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if body.scope == "organization":
        if body.key == registry.OPERATOR_ACKNOWLEDGMENT_KEY:
            raise _coded(
                422,
                "legal_scope_not_supported",
                "This document cannot be accepted on behalf of an organization",
            )
        if ctx.org_id is None or ctx.org_role != _ORG_ADMIN or ctx.bypass.under_grant:
            raise _coded(
                403,
                "legal_org_acceptance_forbidden",
                "Only an organization administrator may accept for the organization",
            )
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
    locale = body.locale or _header_locale(request)
    row, created = acceptance.record_acceptance(
        db,
        current_user.id,
        doc,
        client_ip,
        user_agent=None if user_agent == "unknown" else user_agent,
        locale=locale,
        organization_id=ctx.org_id,
        organization_uuid=_org_uuid(db, ctx.org_id),
        scope=body.scope,
    )
    if created:
        audit_logger.log(
            event_type=AuditEventType.LEGAL_ACKNOWLEDGED,
            outcome=AuditOutcome.SUCCESS,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=client_ip,
            user_agent=user_agent,
            organization_id=ctx.org_id,
            details=_audit_details(row),
        )
    return LegalAcknowledgmentRecord(
        key=row.document_key,
        version=row.version,
        sha256=row.sha256,
        accepted_at=row.accepted_at,
        scope=row.scope,
        organization_uuid=row.organization_uuid,
    )


@router.get("/acknowledgments", response_model=list[LegalAcknowledgmentAdminRecord])
def list_legal_acknowledgments(
    organization: str | None = Query(default=None, pattern="^current$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> list[LegalAcknowledgmentAdminRecord]:
    """Stored acceptances, newest first, with the accepting user's email.

    Without ``organization`` this is the instance-wide list (platform admins). With
    ``organization=current`` an org admin sees the rows recorded in their organization.
    The join is OUTER: a record can outlive the account that made it.
    """
    query = select(LegalAcceptance, User.email).outerjoin(User, User.id == LegalAcceptance.user_id)
    if organization == "current":
        if ctx.org_id is None or ctx.org_role != _ORG_ADMIN:
            raise _coded(
                403,
                "legal_org_acceptance_forbidden",
                "Only an organization administrator may list the organization's acceptances",
            )
        query = query.where(LegalAcceptance.organization_id == ctx.org_id)
    elif current_user.role not in (ROLE_ADMIN, ROLE_SUPER_ADMIN):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    rows = db.execute(
        query.order_by(LegalAcceptance.accepted_at.desc(), LegalAcceptance.id.desc())
    ).all()
    return [
        LegalAcknowledgmentAdminRecord(
            key=r.document_key,
            version=r.version,
            sha256=r.sha256,
            accepted_at=r.accepted_at,
            scope=r.scope,
            organization_uuid=r.organization_uuid,
            user_email=str(email) if email is not None else None,
            erased=r.user_id is None,
            retain_until=r.retain_until,
        )
        for r, email in rows
    ]
