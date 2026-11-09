"""Who may see, restore and destroy a trashed file (issue #1101, plan section 3.6).

The trash routes cannot use the normal lookup (``uuid_helpers.get_file_by_uuid_with_permission``):
a trashed file is unavailable there to everyone. This module is their own lookup and list.

| Action | Personal scope | Organization scope | Platform role |
|---|---|---|---|
| Trash, restore | owner | owner, or ``org:admin`` of the file's org | ``bypass.allows(need="write")`` |
| List | own trashed files | own; an ``org:admin`` sees the whole org trash | ``bypass.sees_all_in_scope`` |
| Delete forever, empty | owner | ``org:admin`` only | ``bypass.allows(need="write")`` |

Trash and restore are exactly the delete right (``delete_permissions.can_delete_file``). Delete
forever is narrower in an organization: a member's mistake stays recoverable by an admin for
the window. A quarantined trashed file stays invisible to every caller the quarantine hides it
from, exactly as in the gallery. Sharing never reaches the trash: an editor or viewer share
grants nothing here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Query
from sqlalchemy.orm import Session

from app.core.exceptions import AccessDeniedError
from app.core.exceptions import ResourceNotFoundError
from app.models.media import MediaFile
from app.models.user import User
from app.services.delete_permissions import can_delete_file
from app.services.file_availability import unavailable_list_exempt
from app.services.platform_bypass import Need
from app.services.request_context import RequestContext
from app.services.uuid_helpers import _resource_in_tenant_scope
from app.utils.db_helpers import org_stamp_is
from app.utils.uuid_parse import parse_uuid_or_none

#: 403 detail for an organization member asking to delete forever.
PURGE_FORBIDDEN_DETAIL = (
    "Only an organization admin can permanently delete a file in the organization's trash."
)
#: 403 detail for a caller who can see a trashed file but may not restore it.
RESTORE_FORBIDDEN_DETAIL = "Only the file's owner or an organization admin can restore this file."

_NOT_FOUND = "File not found in the trash"


def _is_org_admin_of(ctx: RequestContext, file: MediaFile) -> bool:
    return (
        ctx.is_org_admin
        and ctx.org_id is not None
        and file.organization_id is not None
        and int(file.organization_id) == int(ctx.org_id)
    )


def _platform_reaches(ctx: RequestContext, file: MediaFile, need: Need) -> bool:
    return ctx.bypass.allows(
        org_id=file.organization_id,
        owner_id=file.user_id,
        need=need,
        resource_type="media_file",
        resource_uuid=str(file.uuid),
    )


def get_trashed_file(db: Session, file_uuid: Any, ctx: RequestContext) -> MediaFile:
    """Load a trashed file the caller may see, or raise 404.

    404 (never 403) for a file that is not trashed, outside the active tenant, quarantined and
    hidden from the caller, or neither the caller's nor in an organization they administer:
    the trash is not a share surface, so a non-owner learns nothing about another's trash.
    """
    parsed = parse_uuid_or_none(file_uuid)
    file = db.query(MediaFile).filter(MediaFile.uuid == parsed).first() if parsed else None
    if file is None or file.deleted_at is None:
        raise ResourceNotFoundError(_NOT_FOUND)
    if _platform_reaches(ctx, file, "read"):
        return file
    if not _resource_in_tenant_scope(file, ctx.org_id):
        raise ResourceNotFoundError(_NOT_FOUND)
    if file.is_quarantined:
        raise ResourceNotFoundError(_NOT_FOUND)
    if file.user_id == ctx.user.id or _is_org_admin_of(ctx, file):
        return file
    raise ResourceNotFoundError(_NOT_FOUND)


def require_restorable_by(ctx: RequestContext, file: MediaFile) -> None:
    """403 unless the caller holds the delete right on ``file`` (owner, org admin, platform)."""
    if not can_delete_file(
        ctx.user,
        file,
        organization_id=ctx.org_id,
        is_org_admin=ctx.is_org_admin,
        bypass=ctx.bypass,
    ):
        raise AccessDeniedError(RESTORE_FORBIDDEN_DETAIL)


def can_purge(ctx: RequestContext, file: MediaFile) -> bool:
    """Whether the caller may delete ``file`` forever (personal owner, org admin, platform)."""
    if _platform_reaches(ctx, file, "write"):
        return True
    if file.organization_id is None:
        return file.user_id == ctx.user.id
    return _is_org_admin_of(ctx, file)


def require_purgeable_by(ctx: RequestContext, file: MediaFile) -> None:
    """403 unless :func:`can_purge`."""
    if not can_purge(ctx, file):
        raise AccessDeniedError(PURGE_FORBIDDEN_DETAIL)


def _sees_whole_trash(ctx: RequestContext) -> bool:
    return ctx.bypass.sees_all_in_scope or (ctx.org_id is not None and ctx.is_org_admin)


def trash_query(db: Session, ctx: RequestContext) -> Query[MediaFile]:
    """Every trashed file the caller may list in the active tenant (never the gallery query)."""
    query = db.query(MediaFile).filter(
        MediaFile.deleted_at.is_not(None), org_stamp_is(MediaFile.organization_id, ctx.org_id)
    )
    if not _sees_whole_trash(ctx):
        query = query.filter(MediaFile.user_id == ctx.user.id)
    if not unavailable_list_exempt(ctx.bypass):
        query = query.filter(MediaFile.is_quarantined.is_(False))
    return query


def trash_page(
    db: Session, ctx: RequestContext, *, page: int, page_size: int
) -> tuple[list[MediaFile], int]:
    """One page of :func:`trash_query`, newest deletion first, and the total."""
    query = trash_query(db, ctx)
    files = (
        query.order_by(MediaFile.deleted_at.desc(), MediaFile.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return files, query.count()


def people_of(db: Session, files: list[MediaFile]) -> dict[int, User]:
    """The owners and deleters of ``files``, by id, in one query (for the list's names)."""
    ids = {int(f.user_id) for f in files} | {
        int(f.deleted_by_user_id) for f in files if f.deleted_by_user_id
    }
    if not ids:
        return {}
    return {int(u.id): u for u in db.query(User).filter(User.id.in_(ids))}


def require_may_empty(ctx: RequestContext) -> None:
    """403 for an organization member: only an org admin (or the platform) empties an org trash."""
    if ctx.org_id is not None and not _sees_whole_trash(ctx):
        raise AccessDeniedError(PURGE_FORBIDDEN_DETAIL)


def purgeable_query(db: Session, ctx: RequestContext) -> Query[MediaFile]:
    """The part of the caller's trash they may delete forever ("empty trash").

    Personal scope: their own trash. Organization scope: the whole org trash for an org admin
    and for a platform role that lists it, nothing for a member. Held and quarantined rows are
    left to ``mark_for_immediate_purge``, which refuses them.
    """
    require_may_empty(ctx)
    return trash_query(db, ctx)


def row_flags(ctx: RequestContext, file: MediaFile) -> tuple[bool, bool]:
    """``(can_restore, can_purge)`` display hints for one listed row.

    Computed without ``bypass.allows`` so that listing records no write-use under a support
    grant; the action routes decide again through :func:`require_restorable_by` /
    :func:`require_purgeable_by`.
    """
    grant = ctx.bypass.grant
    platform_write = ctx.bypass.sees_all_in_scope and (
        grant is None or grant.access_level == "write"
    )
    owner = file.user_id == ctx.user.id
    org_admin = _is_org_admin_of(ctx, file)
    restore = platform_write or owner or org_admin
    purge = platform_write or org_admin or (owner and file.organization_id is None)
    return restore, purge


__all__ = [
    "PURGE_FORBIDDEN_DETAIL",
    "RESTORE_FORBIDDEN_DETAIL",
    "can_purge",
    "people_of",
    "get_trashed_file",
    "purgeable_query",
    "require_may_empty",
    "require_purgeable_by",
    "require_restorable_by",
    "row_flags",
    "trash_page",
    "trash_query",
]
