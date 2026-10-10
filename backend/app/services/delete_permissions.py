"""Who may permanently delete a media file (issue #1103).

Delete is a separate right from edit. Before this module every delete path
resolved the file with ``min_permission="editor"``, so an ``editor`` share grant
(a collection shared for collaboration) let a non-owner destroy the file, while
an organization admin with no share could not delete a member's file at all.

The rule, in one place:

* the file's owner (uploader);
* an ``org:admin`` of the file's organization, acting in that organization;
* a platform admin, in single-tenant mode only (``PlatformBypass``; in multi-tenant mode
  only through a support-access grant with write access).

A personal (org-less) file therefore has exactly one non-admin deleter: its
owner. An editor share keeps every edit right and loses only delete.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import MediaFile
from app.models.user import User
from app.services.platform_bypass import PlatformBypass

logger = logging.getLogger(__name__)

#: 403 detail for a caller who can see the file but may not delete it.
DELETE_FORBIDDEN_DETAIL = "Only the file's owner or an organization admin can delete this file."


def can_delete_file(
    user: User,
    file: MediaFile,
    *,
    organization_id: OrgScope = UNSCOPED,
    is_org_admin: bool = False,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> bool:
    """Whether ``user`` may permanently delete ``file``.

    Args:
        user: The caller.
        file: The resolved media file.
        organization_id: The caller's active organization (``ctx.org_id``), None
            for personal scope, or UNSCOPED when no request context exists.
        is_org_admin: Whether the caller is ``org:admin`` of ``organization_id``
            (``ctx.is_org_admin``). Only honoured when ``organization_id`` is an
            org id equal to the file's organization.
        bypass: The request's platform bypass, decided against the file's tenant.
    """
    if bypass.allows(
        org_id=file.organization_id,
        owner_id=file.user_id,
        need="write",
        resource_type="media_file",
        resource_uuid=str(file.uuid),
    ):
        return True
    if file.user_id == user.id:
        return True
    return (
        is_org_admin
        and isinstance(organization_id, int)
        and file.organization_id is not None
        and file.organization_id == organization_id
    )


def _audit_denied(user: User, file: MediaFile, reason: str) -> None:
    try:
        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditOutcome
        from app.auth.audit import audit_logger

        audit_logger.log(
            AuditEventType.FILE_DELETE_DENIED,
            AuditOutcome.FAILURE,
            user_id=int(user.id),
            username=str(user.email),
            error_code=reason,
            details={"file_uuid": str(file.uuid)},
            organization_id=(
                int(file.organization_id) if file.organization_id is not None else None
            ),
            target_user_id=int(file.user_id),
        )
    except Exception:  # noqa: BLE001 - auditing must never turn a 403 into a 500
        logger.exception("Could not audit denied delete of file %s", file.uuid)


def get_deletable_file(
    db: Session,
    file_uuid: str,
    user: User,
    *,
    organization_id: OrgScope = UNSCOPED,
    is_org_admin: bool = False,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> MediaFile:
    """Resolve ``file_uuid`` for a permanent delete, or raise.

    404: the file does not exist, or is quarantined and the caller is not a
    platform admin (same as every read path). 403: outside the caller's active
    tenant, or visible to the caller but not deletable by them (the latter is
    audited as ``file.delete.denied``). The platform ``bypass`` is decided on the
    loaded file, so in multi-tenant mode an admin outside the file's tenant is
    refused like anyone else.
    """
    from app.services.takedown_service import is_hidden_for
    from app.utils.uuid_helpers import _resource_in_tenant_scope
    from app.utils.uuid_helpers import get_file_by_uuid

    file = get_file_by_uuid(db, file_uuid)

    if bypass.allows(
        org_id=file.organization_id,
        owner_id=file.user_id,
        need="write",
        resource_type="media_file",
        resource_uuid=str(file.uuid),
    ):
        return file

    if is_hidden_for(file, is_admin=False):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    if not _resource_in_tenant_scope(file, organization_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this file",
        )

    if can_delete_file(
        user,
        file,
        organization_id=organization_id,
        is_org_admin=is_org_admin,
        bypass=bypass,
    ):
        return file

    from app.services.permission_service import PermissionService

    if PermissionService.get_file_permission(db, file.id, user.id) is None:
        # Not even visible to the caller: same answer as any other lookup.
        _audit_denied(user, file, "no_access")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this file",
        )

    _audit_denied(user, file, "not_owner_or_org_admin")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DELETE_FORBIDDEN_DETAIL)


__all__ = [
    "DELETE_FORBIDDEN_DETAIL",
    "can_delete_file",
    "get_deletable_file",
]
