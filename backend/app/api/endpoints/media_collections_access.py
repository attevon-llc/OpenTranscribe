"""Collection helpers shared by the ``media_collections*`` route modules.

Tenant predicate, hold badge and refusal, visibility authority, prompt resolution and the
NULL-timestamp sentinel. A leaf: the route modules import it, it imports none of them.
"""

from datetime import UTC
from datetime import datetime

from fastapi import HTTPException
from fastapi import status
from sqlalchemy import ColumnElement
from sqlalchemy.orm import Session

from app.auth.audit_events import TENANT_SCOPE_ORG
from app.auth.audit_events import TENANT_SCOPE_PERSONAL
from app.models.collection import Collection
from app.schemas.content_hold import HoldSummary
from app.services.collection_queries import find_usable_prompt
from app.services.content_audit import AuditedResource
from app.services.content_hold_queries import collection_hold_states
from app.services.content_hold_queries import collection_hold_summaries
from app.services.content_hold_service import record_hold_refusal
from app.services.hold_guard import HoldState
from app.services.hold_guard import refusal_detail
from app.services.request_context import RequestContext

#: Substituted for a NULL ``created_at`` on a share or a collection.
#:
#: ``collection_share.created_at`` and ``collection.created_at`` are both ``nullable`` in
#: Postgres — a ``server_default`` only fills a column an INSERT omits, so a raw-SQL insert
#: naming it, a backfill or an explicit ``UPDATE ... SET created_at = NULL`` leaves NULL.
#: ``Share.created_at`` and ``SharedCollectionInfo.shared_at`` are required ``datetime``
#: fields, so a NULL must become some timestamp or Pydantic fails the whole response.
#: The epoch, not ``datetime.now(UTC)``: "now" would claim the share was just created and
#: would sort a timestamp-less share to the top of the shared-with-me list.
UNKNOWN_TIMESTAMP = datetime(1970, 1, 1, tzinfo=UTC)


def _resolve_prompt_uuid(db: Session, prompt_uuid: str | None, user_id: int) -> int | None:
    """Resolve a prompt UUID to its internal ID, validating access."""
    if prompt_uuid is None:
        return None

    prompt = find_usable_prompt(db, prompt_uuid, user_id)

    if not prompt:
        raise HTTPException(
            status_code=404,
            detail="Summary prompt not found or not accessible",
        )

    return prompt.id


def _get_prompt_info(collection: Collection) -> tuple:
    """Get prompt UUID and name from a collection's default_summary_prompt relationship."""
    prompt = collection.default_summary_prompt
    if prompt:
        return (prompt.uuid, prompt.name)
    return (None, None)


def _tenant_scope(collection: Collection) -> str:
    """``tenant_scope`` of a collection event: whose tenant the collection belongs to."""
    return TENANT_SCOPE_ORG if collection.organization_id is not None else TENANT_SCOPE_PERSONAL


def _collection_held(db: Session, collection: Collection, ctx: RequestContext) -> HoldSummary:
    """The hold badge facts for one collection.

    Organization hold uuids only for its organization's admin, platform ones only for an
    instance administrator.
    """
    return collection_hold_summaries(
        db,
        [collection.id],
        reveal_uuids=ctx.is_org_admin,
        reveal_platform_uuids=ctx.user.is_admin,
    ).get(int(collection.id), HoldSummary())


def _hold_refusal(
    message: str, *, db: Session, collection: Collection, ctx: RequestContext, action: str
) -> HTTPException:
    """409 ``FILE_UNDER_LEGAL_HOLD`` for a collection change a hold forbids (and audit it).

    ``held_by`` is for everybody; the organization hold uuids only for the holding
    organization's admin, the platform ones only for an instance administrator.
    """
    state = collection_hold_states(db, [collection.id]).get(
        int(collection.id), HoldState(platform_flag=False)
    )
    record_hold_refusal(
        db,
        None,
        ctx.user,
        AuditedResource.collection(collection),
        action=action,
        held_by=state.held_by,
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=refusal_detail(
            state,
            reveal_uuids=ctx.is_org_admin,
            reveal_platform_uuids=ctx.user.is_admin,
            message=message,
        ),
    )


def _can_change_visibility(collection: Collection, permission: str | None) -> bool:
    """Whether the caller may switch ``collection``'s visibility (server-computed for the UI).

    Only an organization collection has a visibility to change, and only its owners may:
    the creator or an administrator of its organization (``permission == "owner"`` is
    exactly that for an organization collection; a share never grants it).
    """
    return collection.organization_id is not None and permission == "owner"


def _tenant_pred(ctx: RequestContext) -> ColumnElement[bool]:
    """Collections that live in the request's tenant (org context: that org; else personal).

    Community edition: ctx.org_id is None and rows are org-less, so this is a no-op.
    """
    if ctx.org_id is not None:
        return Collection.organization_id == ctx.org_id
    return Collection.organization_id.is_(None)
