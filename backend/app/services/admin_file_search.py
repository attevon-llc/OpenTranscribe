"""Find files for an administrator's legal-hold and takedown pickers.

The gallery list (``GET /files``) is scoped to what the CALLER may open: in multi-tenant mode an
organization administrator sees only their own and shared files, and an instance administrator
only theirs. A hold, though, may name any of the organization's files (``/org-admin/holds``) or
any tenant's (``/admin/holds``), including a file in the trash, a hidden one or a quarantined
one, so a picker built on the gallery list could not reach every file the hold routes accept.
This is that reach, and nothing wider: the organization scope is exactly the predicate
``/org-admin/holds`` places with (``media_file.organization_id = <org>``), the platform scope
every row.

The text match is the gallery's own (:func:`title_or_filename_matches`, which
``services/file_list_filters.apply_search_filter`` also uses), the tenant gate is
``permission_service.org_scope_pred``, the hold badge is ``content_hold_queries`` and the trash
and takedown state is ``file_availability``: no predicate here is a second copy of one elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import ColumnElement
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tenancy import OrgScope
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.user import User
from app.schemas.content_hold import AdminFileHit
from app.schemas.content_hold import AdminFileSearchResult
from app.schemas.content_hold import FileAvailability
from app.schemas.content_hold import HoldSummary
from app.services.content_hold_queries import list_hold_summaries
from app.services.file_availability import index_availability
from app.services.permission_service import org_scope_pred
from app.services.search.availability_field import AVAILABLE
from app.services.search.availability_field import QUARANTINED
from app.services.search.availability_field import TRASHED
from app.utils.sql_like import escape_like

#: The most rows one request returns: a picker shows a short list to choose from.
ADMIN_FILE_SEARCH_MAX = 50

#: ``file_availability``'s three states, typed for the wire. A fourth state would be a KeyError
#: here, not a silently mislabelled row.
_AVAILABILITY: dict[str, FileAvailability] = {
    AVAILABLE: "available",
    TRASHED: "trashed",
    QUARANTINED: "quarantined",
}


def title_or_filename_matches(search: str) -> ColumnElement[bool]:
    """Case-insensitive substring match on the title or the file name, LIKE wildcards literal."""
    pattern = f"%{escape_like(search)}%"
    return or_(
        MediaFile.filename.ilike(pattern, escape="\\"),
        MediaFile.title.ilike(pattern, escape="\\"),
    )


@dataclass(frozen=True)
class AdminFileMatch:
    """One file a picker may offer, with what the administrator needs to tell it apart."""

    file: MediaFile
    owner: str
    organization_name: str | None
    availability: FileAvailability
    hold: HoldSummary


def search_files_for_admin(
    db: Session,
    *,
    search: str,
    organization_id: OrgScope,
    limit: int,
    offset: int = 0,
) -> tuple[list[AdminFileMatch], int]:
    """Files whose title or name contains ``search``, newest upload first, and the total.

    Args:
        db: Session.
        search: The text typed into the picker (already stripped, never empty).
        organization_id: An organization's id for its administrator, ``UNSCOPED`` for an
            instance administrator (every tenant).
        limit: Page size, capped at :data:`ADMIN_FILE_SEARCH_MAX`.
        offset: Rows to skip.

    Returns:
        The page of matches and the number of matching files.
    """
    if not search:
        return [], 0  # whitespace only: a picker asks for something, never for everything
    where = [title_or_filename_matches(search)]
    tenant = org_scope_pred(MediaFile.organization_id, organization_id)
    if tenant is not None:
        where.append(tenant)

    total = int(db.execute(select(func.count(MediaFile.id)).where(*where)).scalar_one())
    rows = db.execute(
        select(MediaFile, User, Organization)
        .outerjoin(User, User.id == MediaFile.user_id)
        .outerjoin(Organization, Organization.id == MediaFile.organization_id)
        .where(*where)
        .order_by(MediaFile.upload_time.desc(), MediaFile.id.desc())
        .offset(offset)
        .limit(min(limit, ADMIN_FILE_SEARCH_MAX))
    ).all()
    holds = list_hold_summaries(db, (int(f.id) for f, _owner, _org in rows))
    return [
        AdminFileMatch(
            file=f,
            owner=str(owner.full_name or owner.email) if owner is not None else "",
            organization_name=str(org.name) if org is not None else None,
            availability=_AVAILABILITY[index_availability(f)],
            hold=holds[int(f.id)],
        )
        for f, owner, org in rows
    ], total


def admin_file_search_result(
    matches: list[AdminFileMatch], total: int, *, with_organization: bool
) -> AdminFileSearchResult:
    """The wire shape; an organization's own search leaves its (single) organization out."""
    return AdminFileSearchResult(
        files=[
            AdminFileHit(
                uuid=m.file.uuid,
                title=m.file.title,
                filename=str(m.file.filename),
                upload_time=m.file.upload_time,
                owner=m.owner,
                organization_name=m.organization_name if with_organization else None,
                availability=m.availability,
                hold=m.hold,
            )
            for m in matches
        ],
        total=total,
    )
