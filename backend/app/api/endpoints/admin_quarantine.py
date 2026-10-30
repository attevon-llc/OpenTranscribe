"""Admin abuse / DMCA / safe-harbor takedown (quarantine) routes."""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_admin_user
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.user import User
from app.schemas.admin import QuarantineActionResponse
from app.schemas.admin import QuarantinedFile
from app.schemas.admin import QuarantinedFilesList
from app.schemas.admin import QuarantineNoticeRef
from app.schemas.admin import QuarantineRequest
from app.schemas.admin import ReleaseRequest
from app.services.platform_bypass import audit_metadata_access

# No basicConfig here — this module is imported via the API router before
# configure_logging() runs; a default root handler would double every log line.
logger = logging.getLogger(__name__)

router = APIRouter()


# ===========================================================================
# Abuse / DMCA / safe-harbor takedown (admin quarantine)
# ===========================================================================


def _request_meta(request: Request) -> tuple[str, str]:
    """Extract (source_ip, user_agent) for the takedown audit trail."""
    ip = request.client.host if request.client else ""
    return ip, request.headers.get("user-agent", "")


@router.get("/files/quarantined", response_model=QuarantinedFilesList)
def list_quarantined_files(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    include_legal_holds: bool = Query(
        False,
        description=(
            "Also include files that are no longer quarantined but remain under "
            "legal hold -- the release(clear_legal_hold=False) intermediate state "
            "(issue #825). Additive: still-quarantined files are always included "
            "regardless of this flag."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """List taken-down files for admin review (abuse/DMCA queue).

    Quarantined files are hidden from every normal read surface, so this is the
    only place an admin can see and act on them. Newest takedown first.

    By default this is the takedown QUEUE (``is_quarantined IS TRUE``) only.
    ``release(clear_legal_hold=False)`` unquarantines a file (restoring normal
    visibility) while keeping its legal-hold, which produced a state no endpoint
    could list at all until ``include_legal_holds=true`` was added (issue #825) --
    an admin who deliberately kept a hold on a released file had no way to find
    it again through the API a review-queue client would use.
    """
    from sqlalchemy.orm import aliased

    from app.models.organization import Organization
    from app.services.takedown_authority import origin_value
    from app.services.takedown_notice_queries import live_notice_refs

    # Two DIFFERENT FKs to `user` on the same row (owner, and the admin who acted),
    # so this needs two aliased OUTER joins, not a per-row lookup (issue #576).
    # Both are OUTER: `quarantined_by` is nullable (pre-v371 rows), and
    # an INNER join there would silently drop legacy rows from the queue entirely.
    owner_alias = aliased(User)
    quarantined_by_alias = aliased(User)

    if include_legal_holds:
        condition = or_(MediaFile.is_quarantined.is_(True), MediaFile.legal_hold.is_(True))
    else:
        condition = MediaFile.is_quarantined.is_(True)

    base_query = db.query(MediaFile).filter(condition)
    total = base_query.with_entities(func.count(MediaFile.id)).scalar() or 0

    rows = (
        db.query(MediaFile, owner_alias, quarantined_by_alias, Organization)
        .filter(condition)
        .outerjoin(owner_alias, owner_alias.id == MediaFile.user_id)
        .outerjoin(quarantined_by_alias, quarantined_by_alias.id == MediaFile.quarantined_by)
        .outerjoin(Organization, Organization.id == MediaFile.organization_id)
        .order_by(MediaFile.quarantined_at.desc().nullslast())
        .offset(offset)
        .limit(limit)
        .all()
    )
    audit_metadata_access(
        db,
        current_user,
        route="/api/admin/files/quarantined",
        count=len(rows),
        organization_ids=(f.organization_id for f, _owner, _by, _org in rows),
    )
    refs = live_notice_refs(db, [int(f.id) for f, _owner, _by, _org in rows])
    files = [
        QuarantinedFile(
            uuid=str(f.uuid),
            filename=f.filename,
            title=f.title,
            # `owner_alias` is deliberately non-nullable in the response: `user_id` is a
            # required FK on MediaFile, so a missing owner row would mean the
            # database's own referential integrity was already broken -- not a
            # case this endpoint needs to model as optional.
            owner_uuid=str(owner.uuid) if owner is not None else "",
            owner_email=str(owner.email) if owner is not None else "",
            organization_uuid=str(org.uuid) if org is not None else None,
            quarantine_reason=f.quarantine_reason,
            quarantined_at=f.quarantined_at.isoformat() if f.quarantined_at else None,
            quarantined_by_email=str(quarantined_by.email) if quarantined_by is not None else None,
            legal_hold=bool(f.legal_hold),
            is_quarantined=bool(f.is_quarantined),
            takedown_origin=origin_value(f),
            notices=[
                QuarantineNoticeRef(notice_uuid=n, link_uuid=lk, notice_type=nt, status=st)
                for n, lk, nt, st in refs.get(int(f.id), [])
            ],
        )
        for f, owner, quarantined_by, org in rows
    ]
    return QuarantinedFilesList(files=files, total=int(total))


@router.post("/files/{file_uuid}/quarantine", response_model=QuarantineActionResponse)
def quarantine_media_file(
    file_uuid: str,
    request_body: QuarantineRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Take a file down (abuse/DMCA). Hides it from all read surfaces + audits.

    The original media/transcript are NOT deleted — the takedown is reversible
    via ``/release`` and a legal-hold (default on) protects the object from
    deletion while a dispute/notice is open.
    """
    from app.api.endpoints.admin_takedown_notices import takedown_notice_http_error
    from app.services.takedown_notice_service import TakedownNoticeError
    from app.services.takedown_notice_service import find_notice
    from app.services.takedown_service import TakedownActor
    from app.services.takedown_service import quarantine_file
    from app.services.uuid_helpers import get_file_by_uuid

    file = get_file_by_uuid(db, file_uuid)
    source_ip, user_agent = _request_meta(request)
    try:
        notice = (
            find_notice(db, request_body.notice_uuid)
            if request_body.notice_uuid is not None
            else None
        )
        file = quarantine_file(
            db,
            file,
            actor=TakedownActor.platform(current_user),
            reason=request_body.reason,
            notice=notice,
            operator_action_reason=request_body.operator_action_reason,
            legal_hold=request_body.legal_hold,
            source_ip=source_ip,
            user_agent=user_agent,
        )
    except TakedownNoticeError as error:
        raise takedown_notice_http_error(error) from None
    return QuarantineActionResponse(
        uuid=str(file.uuid),
        is_quarantined=bool(file.is_quarantined),
        legal_hold=bool(file.legal_hold),
        status=str(file.status.value if hasattr(file.status, "value") else file.status),
        presign_revoked=bool(getattr(file, "presign_revoked", False)),
        takedown_origin=file.takedown_origin,
    )


@router.post("/files/{file_uuid}/release", response_model=QuarantineActionResponse)
def release_media_file(
    file_uuid: str,
    request: Request,
    request_body: ReleaseRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Release a quarantined file: restore access, lift the legal-hold, audit.

    J-B7 (issue #576/#689): ``release(clear_legal_hold=False)`` produces a file
    that is *released but still held* -- ``is_quarantined=False, legal_hold=True``.
    Before this, that state's only documented remedy (this very endpoint) refused
    it with 409 "File is not quarantined", which made the account-deletion guard's
    (``_assert_no_held_files``) own documented remedy unreachable. The
    409 stays for the *release-from-quarantine* leg -- releasing a file that was
    never quarantined and carries no hold is still refused -- but a
    released-but-held file may now have its hold lifted here.
    """
    from app.services.takedown_service import TakedownActor
    from app.services.takedown_service import release_file
    from app.services.uuid_helpers import get_file_by_uuid

    file = get_file_by_uuid(db, file_uuid)
    if not file.is_quarantined and not file.legal_hold:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="File is not quarantined and carries no legal hold",
        )
    clear_hold = request_body.clear_legal_hold if request_body is not None else True
    source_ip, user_agent = _request_meta(request)
    file = release_file(
        db,
        file,
        actor=TakedownActor.platform(current_user),
        clear_legal_hold=clear_hold,
        reason=request_body.reason if request_body is not None else None,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return QuarantineActionResponse(
        uuid=str(file.uuid),
        is_quarantined=bool(file.is_quarantined),
        legal_hold=bool(file.legal_hold),
        status=str(file.status.value if hasattr(file.status, "value") else file.status),
        presign_tag_cleared=bool(getattr(file, "presign_tag_cleared", True)),
        takedown_origin=file.takedown_origin,
    )
