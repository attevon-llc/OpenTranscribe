"""Per-file handlers behind ``POST /files/management/bulk-action``.

Each ``_handle_*`` turns one file and one verb into a ``BulkActionResult`` rather than
raising, so a batch reports per-file outcomes. Retry and reprocess, which share the
retry-ceiling gate, live in ``bulk_rework_actions``; the dispatching loop and the
route are in ``bulk_actions``.
"""

import logging

from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.endpoints.files.crud import delete_media_file
from app.core.constants import HIDDEN_MEDIA_PER_USER_CAP
from app.core.env import env_bool
from app.core.exceptions import DomainError
from app.core.queues import NLPPriority
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.tag import Tag
from app.models.user import User
from app.services.delete_permissions import DELETE_FORBIDDEN_DETAIL
from app.services.dispatch_priority import nlp_priority
from app.services.llm_settings_outage import LLM_SETTINGS_UNREADABLE_MESSAGE
from app.services.platform_bypass import PlatformBypass
from app.services.tag_bulk import BulkTagOutcome
from app.services.tag_bulk import apply_tag_to_file
from app.tasks.summarization import summarize_transcript_task
from app.utils.db_helpers import file_has_transcript
from app.utils.stuck_file_recovery import recover_stuck_file
from app.utils.task_cancel import cancel_active_task

logger = logging.getLogger(__name__)


class BulkActionResult(BaseModel):
    """Result of bulk file operations."""

    file_uuid: str
    success: bool
    message: str
    error: str | None = None
    outcome: BulkTagOutcome | None = None


#: Bulk verbs that change a file's content, and the ``content.hold.refused`` action each is
#: recorded as when the file is held (legal-hold freeze). Delete and the trash verbs refuse
#: through ``trash_service``; cancel only acts during the initial run; hide is a view preference.
BULK_FREEZE_ACTIONS: dict[str, str] = {
    "retry": "retry",
    "recover": "recover",
    "reprocess": "reprocess",
    "summarize": "summary_generate",
    "identify_speakers": "identify_speakers",
    "redact": "redaction_rescan",
    "add_tag": "tag_add",
    "remove_tag": "tag_remove",
}

#: The verbs that regenerate the transcript keep the reprocess wording.
_REWORK_VERBS = frozenset({"retry", "recover", "reprocess"})


def bulk_hold_refusal(
    db: Session,
    file_uuid: str,
    file_id: int,
    verb: str,
    *,
    actor: User | None,
    request: Request | None = None,
) -> BulkActionResult | None:
    """This file's ``FILE_UNDER_LEGAL_HOLD`` row when ``verb`` would change a held file.

    ``None`` when the verb is not a content change or the file is free. Audited like a
    single-file refusal; the message is fixed text, never an exception's.
    """
    from app.api.hold_refusal import audit_hold_refusals
    from app.services.hold_guard import FREEZE_REFUSAL
    from app.services.hold_guard import LEGAL_HOLD_ERROR_CODE
    from app.services.hold_guard import REPROCESS_REFUSAL
    from app.services.hold_guard import hold_states

    action = BULK_FREEZE_ACTIONS.get(verb)
    if action is None:
        return None
    held = hold_states(db, [file_id])
    if not held:
        return None
    audit_hold_refusals(db, held, actor=actor, action=action, request=request)
    return BulkActionResult(
        file_uuid=file_uuid,
        success=False,
        message=REPROCESS_REFUSAL if verb in _REWORK_VERBS else FREEZE_REFUSAL,
        error=LEGAL_HOLD_ERROR_CODE,
    )


def _handle_delete_action(
    db: Session,
    file_uuid: str,
    current_user: User,
    bypass: PlatformBypass,
    organization_id: OrgScope = UNSCOPED,
    is_org_admin: bool = False,
    request: Request | None = None,
) -> BulkActionResult:
    """Move one file of a bulk delete to the trash (issue #1101).

    Same rule as the single route (``crud.delete_media_file``): a delete the caller has no
    right to (issue #1103: not the owner, not an org admin of the file's organization, not a
    platform admin) is reported as a per-file ``FORBIDDEN`` result and a held file as
    ``FILE_UNDER_LEGAL_HOLD``, so the rest of the batch still runs. Since every delete is
    reversible there is no ``force`` any more: a file with a task in flight is trashed and
    its task cancelled.
    """
    try:
        delete_media_file(
            db,
            file_uuid,
            current_user,
            organization_id=organization_id,
            is_org_admin=is_org_admin,
            bypass=bypass,
            request=request,
        )
    except (HTTPException, DomainError) as e:
        from app.services.hold_guard import LEGAL_HOLD_ERROR_CODE
        from app.services.hold_guard import LEGAL_HOLD_REFUSAL

        detail = e.detail
        if (
            e.status_code == status.HTTP_409_CONFLICT
            and isinstance(detail, dict)
            and detail.get("error") == LEGAL_HOLD_ERROR_CODE
        ):
            # A hold is a deliberate refusal: reported per file so the rest of the batch runs.
            return BulkActionResult(
                file_uuid=file_uuid,
                success=False,
                message=LEGAL_HOLD_REFUSAL,
                error=LEGAL_HOLD_ERROR_CODE,
            )
        if e.status_code != status.HTTP_403_FORBIDDEN:
            raise
        # A fixed message, never the exception text (issue #914's no-echo rule).
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message=DELETE_FORBIDDEN_DETAIL,
            error="FORBIDDEN",
        )
    return BulkActionResult(
        file_uuid=file_uuid,
        success=True,
        message="File moved to the trash",
    )


def _handle_cancel_action(db: Session, file_uuid: str, file_id: int) -> BulkActionResult:
    """Handle cancel action for bulk operations."""
    success = cancel_active_task(db, file_id)
    return BulkActionResult(
        file_uuid=file_uuid,
        success=success,
        message="Task cancelled successfully" if success else "Failed to cancel task",
        error=None if success else "CANCEL_FAILED",
    )


def _handle_recover_action(db: Session, file_uuid: str, file_id: int) -> BulkActionResult:
    """Handle recover action for bulk operations."""
    success = recover_stuck_file(db, file_id)
    return BulkActionResult(
        file_uuid=file_uuid,
        success=success,
        message="File recovered successfully" if success else "Failed to recover file",
        error=None if success else "RECOVERY_FAILED",
    )


def _llm_unavailable(file_uuid: str, user_id: int | None) -> BulkActionResult | None:
    """None when the caller's LLM client builds, else this file's failure row.

    Bulk actions report per-file outcomes instead of aborting the batch. Each message is a
    fixed sentence, never the exception text.
    """
    from app.services.llm_service import LLMConfigurationError
    from app.services.llm_service import LLMService

    def refused(message: str, error: str) -> BulkActionResult:
        return BulkActionResult(file_uuid=file_uuid, success=False, message=message, error=error)

    try:
        llm_service = LLMService.create_from_settings(user_id=user_id, raise_on_unusable=True)
    except LLMConfigurationError as unusable:
        return refused(unusable.user_message, "LLM_CONFIG_UNUSABLE")
    except SQLAlchemyError:
        logger.exception(f"LLM settings could not be read for file {file_uuid}")
        return refused(LLM_SETTINGS_UNREADABLE_MESSAGE, "LLM_SETTINGS_UNAVAILABLE")
    except Exception:
        logger.exception(f"Could not construct an LLM client for file {file_uuid}")
        return refused("LLM provider is not configured", "LLM_NOT_AVAILABLE")
    if llm_service is None:
        return refused("LLM provider is not configured", "LLM_NOT_AVAILABLE")
    llm_service.close()
    return None


def _handle_summarize_action(
    db: Session, file_uuid: str, file_id: int, user_id: int | None = None, *, bulk: bool = False
) -> BulkActionResult:
    """Handle summarize action for bulk operations.

    ``bulk`` (more than one file in the request) queues it behind single requests
    (``services/dispatch_priority.py``).
    """

    unavailable = _llm_unavailable(file_uuid, user_id)
    if unavailable is not None:
        return unavailable

    # Check file status - must be completed
    db_file = db.query(MediaFile).filter_by(id=file_id).first()
    if not db_file or db_file.status != FileStatus.COMPLETED:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="File must be completed before summarizing",
            error="INVALID_STATUS",
        )

    if not env_bool("SKIP_CELERY"):
        task = summarize_transcript_task.apply_async(
            kwargs={"file_uuid": file_uuid, "force_regenerate": False},
            priority=nlp_priority(db, user_id, base=NLPPriority.USER_TRIGGERED, bulk=bulk),
        )
        message = f"Summarization started (task: {task.id})"
    else:
        message = "Summarization prepared (test mode)"

    return BulkActionResult(file_uuid=file_uuid, success=True, message=message)


def _handle_identify_speakers_action(
    db: Session, file_uuid: str, file_id: int, user_id: int | None = None, *, bulk: bool = False
) -> BulkActionResult:
    """Handle LLM speaker identification for bulk operations.

    Mirrors ``POST /api/files/{uuid}/identify-speakers``. Suggestions are stored as
    confidence-scored predictions for manual review and are never auto-applied.

    Availability is probed by constructing the client, as ``_handle_summarize_action``
    does — the async ``is_llm_available`` helper the single-file endpoint uses cannot be
    awaited from this synchronous dispatch.
    """

    # Via the `speaker_tasks` re-export shim, as every other caller does — it keeps the
    # legacy task-name routing alive.
    from app.tasks.speaker_tasks import identify_speakers_llm_task

    unavailable = _llm_unavailable(file_uuid, user_id)
    if unavailable is not None:
        return unavailable

    db_file = db.query(MediaFile).filter_by(id=file_id).first()
    if not db_file or db_file.status != FileStatus.COMPLETED:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="File must be completed before identifying speakers",
            error="INVALID_STATUS",
        )

    if not db_file.speakers:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="No speakers found in this file to identify",
            error="NO_SPEAKERS",
        )

    if not env_bool("SKIP_CELERY"):
        task = identify_speakers_llm_task.apply_async(
            kwargs={"file_uuid": file_uuid},
            priority=nlp_priority(db, user_id, base=NLPPriority.USER_TRIGGERED, bulk=bulk),
        )
        message = f"Speaker identification started (task: {task.id})"
    else:
        message = "Speaker identification prepared (test mode)"

    return BulkActionResult(file_uuid=file_uuid, success=True, message=message)


def _handle_redact_action(db: Session, file_uuid: str, file_id: int) -> BulkActionResult:
    """Handle content-redaction (re)scan for bulk/selective operations.

    Re-runs redaction detection for a completed file (e.g., old files that predate the
    feature, or to apply a settings change). Idempotent — replaces cached spans.

    The status claim and its withdrawal are ``redaction_gate.claim_and_dispatch_redaction_scan``,
    shared with the lazy dispatch on a transcript read. This site used to set
    ``pending``, commit, and then publish unguarded, which made pressing "Redact" on a
    healthy file a way to withhold its transcript permanently whenever the broker was
    momentarily unreachable — see that helper for why nothing else recovers the row.
    """

    from app.api.endpoints.files.redaction_gate import claim_and_dispatch_redaction_scan

    db_file = db.query(MediaFile).filter_by(id=file_id).first()
    if not db_file or db_file.status != FileStatus.COMPLETED:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="File must be completed before redaction",
            error="INVALID_STATUS",
        )
    if not file_has_transcript(db, file_id):
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="No transcript to redact",
            error="NO_TRANSCRIPT",
        )

    if env_bool("SKIP_CELERY"):
        # Nothing is published in test mode, so nothing claims `pending` either: a
        # `pending` with no scan behind it is the stranding this function was fixed
        # for, and it is no more recoverable because a test wrote it.
        return BulkActionResult(
            file_uuid=file_uuid, success=True, message="Redaction prepared (test mode)"
        )

    # Claims `pending` first — that is what shows the UI's "Redacting…" state and what
    # stops a second overlapping batch queueing a duplicate scan — and withdraws the
    # claim if the publish never reached the broker.
    dispatch = claim_and_dispatch_redaction_scan(db, db_file, surface="bulk redact action")
    if not dispatch.queued:
        # Reported as a per-file failure rather than swallowed. The row is back at the
        # status it had, so the file is exactly as readable as before the click, and
        # the caller can retry the batch.
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="Could not queue redaction — the task broker is unreachable. "
            "Nothing was changed; try again.",
            error="DISPATCH_FAILED",
        )

    return BulkActionResult(
        file_uuid=file_uuid,
        success=True,
        message=f"Redaction started (task: {dispatch.task_id})",
    )


def _apply_batch_tag(
    db: Session,
    file_uuid: str,
    file_id: int,
    tag: Tag | None,
    *,
    add: bool,
    user_id: int,
    bypass: PlatformBypass,
) -> BulkActionResult:
    """Attach (``add=True``) or detach the batch's tag on one file.

    A thin adapter: ``tag_bulk.apply_tag_to_file`` owns the permission and tenant
    checks, the per-file savepoint and the mapping of every failure onto a
    ``failed`` outcome, so nothing is caught here. This only copies its result onto
    the response row.

    Args:
        db: Database session.
        file_uuid: Public id of the file, echoed on the row.
        file_id: Internal id of the same file.
        tag: The tag resolved once for the batch; None when a removal named a tag
            that does not exist.
        add: Attach when True, detach when False.
        user_id: The caller.
        bypass: The request's platform bypass.

    Returns:
        The file's row, carrying its ``BulkTagOutcome``.
    """
    applied = apply_tag_to_file(
        db, file_id=file_id, tag=tag, add=add, user_id=user_id, bypass=bypass
    )
    return BulkActionResult(
        file_uuid=file_uuid,
        success=applied.success,
        message=applied.message,
        error=applied.error,
        outcome=applied.outcome,
    )


class RedactionNotEnabledError(HTTPException):
    """The viewer's redaction policy masks nothing, so a scan could change nothing visible."""

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail="Redaction is not enabled. Turn it on and choose at least one "
            "category in Settings > Content Redaction before running a scan.",
        )


def _require_redaction_enabled(
    db: Session,
    db_file: MediaFile,
    current_user: User,
    bypass: PlatformBypass,
    organization_id: OrgScope,
) -> None:
    """Refuse a redaction scan when the governing policy would mask nothing.

    Uses the same policy resolution as the transcript read, so the button the UI
    shows and the action the server accepts can never disagree.
    """
    from app.api.endpoints.files.redaction_gate import _resolve_redaction_for_request

    cfg, _ = _resolve_redaction_for_request(
        db,
        db_file,
        current_user,
        bypass=bypass,
        redact=True,
        organization_id=organization_id,
    )
    if not cfg.masks_anything:
        raise RedactionNotEnabledError()


def _redact_if_enabled(
    db: Session,
    db_file: MediaFile,
    file_uuid: str,
    file_id: int,
    current_user: User,
    bypass: PlatformBypass,
    organization_id: OrgScope,
) -> BulkActionResult:
    _require_redaction_enabled(db, db_file, current_user, bypass, organization_id)
    return _handle_redact_action(db, file_uuid, file_id)


#: The per-user hide verbs (issue #1102). Read access is enough: hiding changes only the
#: caller's own view, so it does not take the "editor" pre-check the other verbs do.
HIDE_ACTIONS = frozenset({"hide", "unhide"})


def _handle_hide_action(
    db: Session,
    file_uuid: str,
    current_user: User,
    bypass: PlatformBypass,
    organization_id: OrgScope,
    *,
    hide: bool,
) -> BulkActionResult:
    """Hide or unhide one file for the caller. A file they cannot read raises the 404.

    A refusal at the per-user cap is this file's result (``HIDDEN_LIMIT``), not the batch's.
    """
    from app.services import hidden_media_service
    from app.services.uuid_helpers import get_file_by_uuid_with_permission

    media_file = get_file_by_uuid_with_permission(
        db,
        file_uuid,
        current_user.id,
        bypass=bypass,
        organization_id=organization_id,
        conceal_inaccessible=True,
    )
    file_ids = [int(media_file.id)]
    if not hide:
        hidden_media_service.unhide(db, current_user.id, file_ids)
        db.commit()
        return BulkActionResult(file_uuid=file_uuid, success=True, message="File unhidden")
    try:
        hidden_media_service.hide(db, current_user.id, file_ids)
    except hidden_media_service.HiddenLimitError:
        # Nothing was written, so there is nothing to roll back; the advisory lock ends with
        # the batch's next commit or the session's close.
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message=f"You can hide at most {HIDDEN_MEDIA_PER_USER_CAP} files",
            error=hidden_media_service.HIDDEN_LIMIT_ERROR_CODE,
        )
    db.commit()
    return BulkActionResult(file_uuid=file_uuid, success=True, message="File hidden")
