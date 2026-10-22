"""Organization-scoped quarantine and release (issue #1253).

A second, tenant-scoped door onto the ONE takedown implementation
(``takedown_service``). An organization administrator can list, quarantine and release
files belonging to their own organization and nothing else.

Authority is decided here by the route, never by the caller's roles: every action is
performed as ``TakedownActor.organization(...)``, so someone who is also a platform
administrator still cannot use these routes to touch a takedown the operator placed
("platform wins", see ``takedown_authority``).

Isolation: the file is looked up with ``organization_id == ctx.org_id`` IN THE QUERY.
Another organization's file, a personal file and a missing file are indistinguishable
(the same 404), so the routes are not an existence oracle. The organization comes from
the resolved request context, never from the path, query or body.

Gating: ``require_org_admin`` plus the server-enforced ``org.takedown`` capability
(mounted in ``api/router.py``), which is off in the community edition. The platform
routes under ``/admin/files`` are deliberately NOT behind any capability.
"""

import logging
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy import ColumnElement
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm import aliased

from app.api.deps_context import RequestContext
from app.api.deps_context import require_org_admin
from app.auth.audit import AuditEventType
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.core.constants import ORG_TAKEDOWN_RATE_LIMIT
from app.core.enums import TakedownOrigin
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.organization import OrganizationMembership
from app.models.user import User
from app.schemas.admin import QuarantineActionResponse
from app.schemas.org_takedown import OrgQuarantinedFile
from app.schemas.org_takedown import OrgQuarantinedFilesList
from app.schemas.org_takedown import OrgQuarantineRequest
from app.schemas.org_takedown import OrgReleaseRequest
from app.services.takedown_authority import CODE_NOT_FOUND
from app.services.takedown_authority import current_origin
from app.services.takedown_notices import audit_unresolved_lookup
from app.services.takedown_service import TakedownActor
from app.services.takedown_service import TakedownRefusedError
from app.services.takedown_service import quarantine_file
from app.services.takedown_service import release_file

logger = logging.getLogger(__name__)

router = APIRouter()

_FORMER_MEMBER = "Former member"


def _org_id(ctx: RequestContext) -> int:
    """The caller's organization; ``require_org_admin`` guarantees it exists."""
    if ctx.org_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization administrator access required",
        )
    return int(ctx.org_id)


def get_org_takedown_file(db: Session, file_uuid: str, org_id: int) -> MediaFile:
    """Resolve ``file_uuid`` inside ``org_id`` only; 404 for everything else.

    Deliberately not ``uuid_helpers.get_file_by_uuid_with_permission``: that chokepoint
    hides quarantined files from everyone but platform administrators, and a file taken
    down by this organization must still resolve for this organization's administrators.
    A malformed uuid keeps the repo's 400 (a format error, not an existence signal).

    Raises:
        HTTPException: 400 for a malformed uuid, 404 when no file with that uuid
            belongs to ``org_id`` (including another organization's file, a personal
            file, and a file that does not exist).
    """
    try:
        parsed = UUID(file_uuid)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid UUID format: {file_uuid}"
        ) from None
    file = (
        db.query(MediaFile)
        .filter(MediaFile.uuid == parsed, MediaFile.organization_id == org_id)
        .first()
    )
    if file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return file


def _request_meta(request: Request) -> tuple[str, str]:
    ip = request.client.host if request.client else ""
    return ip, request.headers.get("user-agent", "")


def _resolve_or_audit(
    db: Session,
    file_uuid: str,
    ctx: RequestContext,
    request: Request,
    event_type: AuditEventType,
) -> MediaFile:
    """:func:`get_org_takedown_file`, recording a miss as one FAILURE event in the actor's org."""
    try:
        return get_org_takedown_file(db, file_uuid, _org_id(ctx))
    except HTTPException as miss:
        if miss.status_code == status.HTTP_404_NOT_FOUND:
            source_ip, user_agent = _request_meta(request)
            audit_unresolved_lookup(
                event_type,
                actor=TakedownActor.organization(ctx.user, _org_id(ctx)),
                error_code=CODE_NOT_FOUND,
                source_ip=source_ip,
                user_agent=user_agent,
            )
        raise


def _refusal_to_http(refusal: TakedownRefusedError) -> HTTPException:
    return HTTPException(
        status_code=refusal.status_code,
        detail={"code": refusal.code, "message": refusal.message},
    )


def _display(user: User | None, is_member: bool) -> str:
    """A person's name for the queue; an address only while they are still a member."""
    if user is None:
        return _FORMER_MEMBER
    if user.full_name:
        return str(user.full_name)
    return str(user.email) if is_member else _FORMER_MEMBER


@router.get(
    "/quarantined",
    response_model=OrgQuarantinedFilesList,
    response_model_exclude_unset=True,
)
def list_org_quarantined_files(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    include_legal_holds: bool = Query(
        False, description="Also include released files that remain under legal hold."
    ),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
):
    """List this organization's taken-down files, newest first.

    A file the operator holds is listed (so an administrator can answer "why did this
    file vanish") with ``can_release`` false and without its reason or actor.
    """
    org_id = _org_id(ctx)
    condition: ColumnElement[bool] = MediaFile.is_quarantined.is_(True)
    if include_legal_holds:
        condition = or_(condition, MediaFile.legal_hold.is_(True))
    scoped = (MediaFile.organization_id == org_id, condition)

    total = db.query(func.count(MediaFile.id)).filter(*scoped).scalar() or 0

    owner = aliased(User)
    actor = aliased(User)
    owner_member = aliased(OrganizationMembership)
    actor_member = aliased(OrganizationMembership)
    rows = (
        db.query(MediaFile, owner, actor, owner_member.id, actor_member.id)
        .filter(*scoped)
        .outerjoin(owner, owner.id == MediaFile.user_id)
        .outerjoin(actor, actor.id == MediaFile.quarantined_by)
        .outerjoin(
            owner_member,
            (owner_member.user_id == MediaFile.user_id) & (owner_member.organization_id == org_id),
        )
        .outerjoin(
            actor_member,
            (actor_member.user_id == MediaFile.quarantined_by)
            & (actor_member.organization_id == org_id),
        )
        .order_by(MediaFile.quarantined_at.desc().nullslast(), MediaFile.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    files = []
    for f, file_owner, file_actor, owner_member_id, actor_member_id in rows:
        origin = current_origin(f) or TakedownOrigin.PLATFORM
        fields: dict = {
            "uuid": str(f.uuid),
            "filename": f.filename,
            "title": f.title,
            "owner_uuid": str(file_owner.uuid) if file_owner is not None else "",
            "owner_display": _display(file_owner, owner_member_id is not None),
            "quarantined_at": f.quarantined_at.isoformat() if f.quarantined_at else None,
            "legal_hold": bool(f.legal_hold),
            "is_quarantined": bool(f.is_quarantined),
            "takedown_origin": origin.value,
            "can_release": origin is TakedownOrigin.ORGANIZATION,
        }
        if origin is TakedownOrigin.ORGANIZATION:
            # Only a file this organization placed discloses why and by whom.
            fields["quarantine_reason"] = f.quarantine_reason
            fields["quarantined_by_display"] = (
                _display(file_actor, actor_member_id is not None)
                if file_actor is not None
                else None
            )
        files.append(OrgQuarantinedFile(**fields))
    return OrgQuarantinedFilesList(files=files, total=int(total))


# One budget shared by both POSTs, keyed by scope rather than by URL: the limiter's default
# bucket includes the request path, and these paths carry a file uuid, so a per-route limit
# would hand every distinct uuid its own fresh allowance and never limit anything.
@router.post("/{file_uuid}/quarantine", response_model=QuarantineActionResponse)
@limiter.shared_limit(ORG_TAKEDOWN_RATE_LIMIT, scope="org_takedown", key_func=user_or_ip_key)
def quarantine_org_file(
    file_uuid: str,
    request_body: OrgQuarantineRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
):
    """Take one of this organization's files down (hidden from everyone but the operator).

    Never places a legal hold: that decision is not delegated to organizations yet.
    Refused with 409 ``takedown_platform_origin`` when the operator already holds it.
    """
    org_id = _org_id(ctx)
    file = _resolve_or_audit(db, file_uuid, ctx, request, AuditEventType.ADMIN_FILE_QUARANTINE)
    source_ip, user_agent = _request_meta(request)
    try:
        file = quarantine_file(
            db,
            file,
            actor=TakedownActor.organization(ctx.user, org_id),
            reason=request_body.reason,
            # An organization administrator has no notice to cite: the stated reason is the
            # record, kept as an organization-authority operator action (issue #1254).
            operator_action_reason=request_body.reason,
            legal_hold=False,
            source_ip=source_ip,
            user_agent=user_agent,
        )
    except TakedownRefusedError as refusal:
        raise _refusal_to_http(refusal) from None
    return QuarantineActionResponse(
        uuid=str(file.uuid),
        is_quarantined=bool(file.is_quarantined),
        legal_hold=bool(file.legal_hold),
        status=str(file.status.value if hasattr(file.status, "value") else file.status),
        presign_revoked=bool(getattr(file, "presign_revoked", False)),
        takedown_origin=file.takedown_origin,
    )


@router.post("/{file_uuid}/release", response_model=QuarantineActionResponse)
@limiter.shared_limit(ORG_TAKEDOWN_RATE_LIMIT, scope="org_takedown", key_func=user_or_ip_key)
def release_org_file(
    file_uuid: str,
    request: Request,
    response: Response,
    request_body: OrgReleaseRequest | None = None,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
):
    """Release a file this organization took down.

    409 when the file is not held; 403 ``takedown_platform_origin`` when the operator
    placed the takedown (only the operator can lift it).
    """
    org_id = _org_id(ctx)
    file = _resolve_or_audit(db, file_uuid, ctx, request, AuditEventType.ADMIN_FILE_RELEASE)
    if not file.is_quarantined and not file.legal_hold:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="File is not quarantined and carries no legal hold",
        )
    source_ip, user_agent = _request_meta(request)
    try:
        file = release_file(
            db,
            file,
            actor=TakedownActor.organization(ctx.user, org_id),
            clear_legal_hold=True,
            reason=request_body.reason if request_body is not None else None,
            source_ip=source_ip,
            user_agent=user_agent,
        )
    except TakedownRefusedError as refusal:
        raise _refusal_to_http(refusal) from None
    return QuarantineActionResponse(
        uuid=str(file.uuid),
        is_quarantined=bool(file.is_quarantined),
        legal_hold=bool(file.legal_hold),
        status=str(file.status.value if hasattr(file.status, "value") else file.status),
        presign_tag_cleared=bool(getattr(file, "presign_tag_cleared", True)),
        takedown_origin=file.takedown_origin,
    )
