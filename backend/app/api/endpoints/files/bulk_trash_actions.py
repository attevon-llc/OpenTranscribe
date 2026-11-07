"""Bulk ``restore`` and ``purge`` (issue #1101): the trash verbs of ``/files/management/bulk-action``.

Each file is resolved through ``trash_access`` (the normal lookup 404s a trashed file) and
changed through ``trash_service``, with the same permission rules as the single trash routes.
A refusal is that file's result, never the batch's: ``NOT_FOUND``, ``FORBIDDEN``,
``NOT_RESTORABLE`` (its purge is due), ``FILE_UNDER_LEGAL_HOLD`` or ``FILE_UNDER_TAKEDOWN``.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from app.api.endpoints.files.bulk_action_handlers import BulkActionResult
from app.core.exceptions import AccessDeniedError
from app.core.exceptions import ResourceNotFoundError
from app.services import trash_access
from app.services import trash_service
from app.services.hold_guard import LEGAL_HOLD_ERROR_CODE
from app.services.hold_guard import LEGAL_HOLD_REFUSAL
from app.services.hold_guard import hold_state
from app.services.request_context import RequestContext

TRASH_BULK_ACTIONS = frozenset({"restore", "purge"})


def _refused(file_uuid: str, message: str, error: str) -> BulkActionResult:
    return BulkActionResult(file_uuid=file_uuid, success=False, message=message, error=error)


def handle_trash_action(
    db: Session, file_uuid: str, action: str, ctx: RequestContext, request: Request | None
) -> BulkActionResult:
    """Restore or purge one trashed file; every refusal is reported, never raised."""
    try:
        file = trash_access.get_trashed_file(db, file_uuid, ctx)
        if action == "restore":
            trash_access.require_restorable_by(ctx, file)
        else:
            trash_access.require_purgeable_by(ctx, file)
    except ResourceNotFoundError:
        return _refused(file_uuid, "File not found in the trash", "NOT_FOUND")
    except AccessDeniedError:
        detail = (
            trash_access.RESTORE_FORBIDDEN_DETAIL
            if action == "restore"
            else trash_access.PURGE_FORBIDDEN_DETAIL
        )
        return _refused(file_uuid, detail, "FORBIDDEN")

    if action == "restore":
        restored, _ = trash_service.restore_files(
            db, [file], actor=ctx.user, request=request, bypass=ctx.bypass
        )
        if not restored:
            return _refused(
                file_uuid,
                "This file's time in the trash has ended and it is being deleted.",
                trash_service.NOT_RESTORABLE,
            )
        return BulkActionResult(file_uuid=file_uuid, success=True, message="File restored")

    marked, _ = trash_service.mark_for_immediate_purge(
        db, [file], actor=ctx.user, request=request, bypass=ctx.bypass
    )
    if not marked:
        if hold_state(db, int(file.id)).held:
            return _refused(file_uuid, LEGAL_HOLD_REFUSAL, LEGAL_HOLD_ERROR_CODE)
        return _refused(
            file_uuid,
            "A file under takedown review is kept until the takedown is released.",
            trash_service.FILE_UNDER_TAKEDOWN,
        )
    trash_service.dispatch_purge([int(file.id)])
    return BulkActionResult(file_uuid=file_uuid, success=True, message="File queued for deletion")
