"""``POST /files/management/bulk-action``: one verb applied to many files.

``_process_single_file_action`` is the per-file funnel: it resolves the file under the
caller's permission (``min_permission="editor"``, except delete, which has its own rule),
applies the legal processing gate, and dispatches to the handlers. The route resolves a
tag once for the batch, then reports each file's outcome without echoing exception text.
"""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.files.bulk_action_handlers import BulkActionResult
from app.api.endpoints.files.bulk_action_handlers import RedactionNotEnabledError
from app.api.endpoints.files.bulk_action_handlers import _apply_batch_tag
from app.api.endpoints.files.bulk_action_handlers import _handle_cancel_action
from app.api.endpoints.files.bulk_action_handlers import _handle_delete_action
from app.api.endpoints.files.bulk_action_handlers import _handle_identify_speakers_action
from app.api.endpoints.files.bulk_action_handlers import _handle_recover_action
from app.api.endpoints.files.bulk_action_handlers import _handle_summarize_action
from app.api.endpoints.files.bulk_action_handlers import _redact_if_enabled
from app.api.endpoints.files.bulk_rework_actions import _handle_reprocess_action
from app.api.endpoints.files.bulk_rework_actions import _handle_retry_action
from app.api.endpoints.files.crud import get_media_file_by_uuid
from app.core.exceptions import DomainError
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.db.base import get_db
from app.legal.processing_gate import check_processing_allowed_for_file
from app.models.tag import Tag
from app.models.user import User
from app.services.platform_bypass import PlatformBypass
from app.services.request_context import RequestContext
from app.services.tag_bulk import CHANGED_OUTCOMES
from app.services.tag_bulk import TAG_ACTIONS
from app.services.tag_bulk import BulkTagOutcome
from app.services.tag_bulk import BulkTagResolution
from app.services.tag_bulk import refresh_tagged_files
from app.services.tag_bulk import resolve_bulk_tag
from app.services.tag_service import InvalidTagNameError

router = APIRouter()
logger = logging.getLogger(__name__)


class BulkActionRequest(BaseModel):
    """Request for bulk file operations."""

    file_uuids: list[str]
    # delete | retry | cancel | recover | reprocess | summarize | redact
    # | identify_speakers | add_tag | remove_tag
    action: str
    force: bool = False
    reset_retry_count: bool = False
    # Must be set for add_tag / remove_tag; ignored by every other action.
    tag_name: str | None = None
    # Selective reprocessing (optional, used when action='reprocess')
    stages: list[str] = Field(default_factory=list)
    min_speakers: int | None = None
    max_speakers: int | None = None
    num_speakers: int | None = None
    disable_diarization: bool | None = None

    @model_validator(mode="after")
    def validate_disable_diarization_scope(self) -> "BulkActionRequest":
        """``disable_diarization`` only changes a reprocess that transcribes."""
        if not self.disable_diarization:
            return self
        if self.action != "reprocess":
            raise ValueError("disable_diarization only applies to action='reprocess'")
        if self.stages and "transcription" not in self.stages:
            raise ValueError(
                "disable_diarization only applies when the 'transcription' stage is "
                "reprocessed; re-diarizing or re-running downstream stages cannot skip "
                "diarization"
            )
        return self


#: Bulk actions that start processing of the file, and so pass the legal processing gate.
PROCESSING_BULK_ACTIONS = frozenset(
    {"retry", "recover", "reprocess", "summarize", "redact", "identify_speakers"}
)


def _process_single_file_action(
    db: Session,
    file_uuid: str,
    action: str,
    current_user: User,
    is_admin: bool,
    bypass: PlatformBypass,
    force: bool,
    reset_retry_count: bool,
    stages: list[str] | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    disable_diarization: bool = False,
    organization_id: OrgScope = UNSCOPED,
    tag: Tag | None = None,
    is_org_admin: bool = False,
) -> BulkActionResult:
    """Process a single file action, returning the result."""
    if action == "delete":
        # Delete resolves the file under its own rule (services/delete_permissions),
        # not the "editor" pre-check below: an editor share must NOT pass, and an
        # org admin with no share on a member's file must (issue #1103).
        return _handle_delete_action(
            db, file_uuid, current_user, force, is_admin, bypass, organization_id, is_org_admin
        )

    db_file = get_media_file_by_uuid(
        db,
        file_uuid,
        current_user.id,
        bypass=bypass,
        organization_id=organization_id,
        min_permission="editor",
    )
    file_id = db_file.id

    if action in PROCESSING_BULK_ACTIONS:
        # The legal processing gate, per file, for the file OWNER. A refusal is this file's
        # result (its code in ``error``); the rest of the batch carries on.
        refusal = check_processing_allowed_for_file(db, db_file)
        if refusal is not None:
            return BulkActionResult(
                file_uuid=file_uuid,
                success=False,
                message=refusal.message,
                error=refusal.code,
            )

    action_handlers = {
        "retry": lambda: _handle_retry_action(db, file_uuid, file_id, reset_retry_count, is_admin),
        "cancel": lambda: _handle_cancel_action(db, file_uuid, file_id),
        "recover": lambda: _handle_recover_action(db, file_uuid, file_id),
        "reprocess": lambda: _handle_reprocess_action(
            db,
            file_uuid,
            file_id,
            is_admin,
            stages,
            min_speakers,
            max_speakers,
            num_speakers,
            disable_diarization,
        ),
        "summarize": lambda: _handle_summarize_action(db, file_uuid, file_id, current_user.id),
        "redact": lambda: _redact_if_enabled(
            db, db_file, file_uuid, file_id, current_user, bypass, organization_id
        ),
        "identify_speakers": lambda: _handle_identify_speakers_action(
            db, file_uuid, file_id, current_user.id
        ),
        "add_tag": lambda: _apply_batch_tag(
            db, file_uuid, file_id, tag, add=True, user_id=current_user.id, bypass=bypass
        ),
        "remove_tag": lambda: _apply_batch_tag(
            db, file_uuid, file_id, tag, add=False, user_id=current_user.id, bypass=bypass
        ),
    }

    handler = action_handlers.get(action)
    if handler:
        return handler()

    return BulkActionResult(
        file_uuid=file_uuid,
        success=False,
        message=f"Unknown action: {action}",
        error="UNKNOWN_ACTION",
    )


@router.post("/management/bulk-action", response_model=list[BulkActionResult])
def bulk_file_action(
    request: BulkActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Perform bulk actions on multiple files."""
    is_tag_action = request.action in TAG_ACTIONS
    resolution = BulkTagResolution(None)
    if is_tag_action:
        # Refused before resolving: adding resolves by inserting, and a support grant
        # must never leave a new tag in the tenant's vocabulary.
        refuse_under_support_grant(ctx)
        try:
            resolution = resolve_bulk_tag(
                db,
                request.action,
                request.tag_name,
                user_id=current_user.id,
                organization_id=ctx.org_id,
            )
        except InvalidTagNameError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Tag name is required",
            ) from exc

    try:
        results = []
        is_admin = current_user.is_admin

        for file_uuid in request.file_uuids:
            try:
                result = _process_single_file_action(
                    db=db,
                    file_uuid=file_uuid,
                    action=request.action,
                    current_user=current_user,
                    is_admin=is_admin,
                    bypass=ctx.bypass,
                    force=request.force,
                    reset_retry_count=request.reset_retry_count,
                    stages=request.stages if request.stages else None,
                    min_speakers=request.min_speakers,
                    max_speakers=request.max_speakers,
                    num_speakers=request.num_speakers,
                    disable_diarization=bool(request.disable_diarization),
                    organization_id=ctx.org_id,
                    tag=resolution.tag,
                    is_org_admin=ctx.is_org_admin,
                )
                results.append(result)
            except RedactionNotEnabledError:
                # A policy-wide refusal, not a per-file failure: surface it as the 409
                # it is so the caller sees why instead of a 200 with an inner error.
                raise
            except (HTTPException, DomainError) as e:
                results.append(
                    BulkActionResult(
                        file_uuid=file_uuid,
                        success=False,
                        message=str(e.detail),
                        error="HTTP_ERROR",
                        outcome=BulkTagOutcome.FAILED if is_tag_action else None,
                    )
                )
            except Exception as e:
                # `results` IS this endpoint's response body (`return results`, below), so
                # the per-file report may carry only facts we authored. The raw exception
                # text here reaches whatever the action touched — MinIO/boto3 errors quote
                # the bucket and endpoint URL, Celery dispatch errors quote the broker URL
                # with its embedded password, OSErrors quote host filesystem paths (#914).
                # What the caller actually needs from a bulk action is preserved: WHICH
                # file failed, that it failed, and the machine-readable `error` code.
                logger.exception(f"Error processing bulk action for file {file_uuid}")
                results.append(
                    BulkActionResult(
                        file_uuid=file_uuid,
                        success=False,
                        message=f"Unexpected error ({type(e).__name__})",
                        error="UNEXPECTED_ERROR",
                        outcome=BulkTagOutcome.FAILED if is_tag_action else None,
                    )
                )

        if is_tag_action:
            changed_uuids = [r.file_uuid for r in results if r.outcome in CHANGED_OUTCOMES]
            # Every file's change is already committed and the reindex is idempotent, so a
            # failed refresh must not turn those committed results into an error response.
            try:
                refresh_tagged_files(
                    db,
                    changed_uuids,
                    user_id=current_user.id,
                    created_tag_id=resolution.created_tag_id,
                )
            except Exception:
                logger.exception(f"Post-batch tag refresh failed after bulk {request.action}")

        return results

    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Error in bulk file action: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error performing bulk file action",
        ) from e
