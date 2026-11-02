"""Pieces every tag route module shares: the router, the write gate, response helpers.

All route modules register onto the one :data:`router`; the package
``__init__`` imports them in the order their paths must be matched.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.models.sharing import TagShare
from app.models.tag import Tag
from app.schemas.tag import Tag as TagSchema
from app.schemas.tag import TagMutationResult
from app.schemas.tag import TagShareTarget
from app.services.request_context import RequestContext
from app.services.tag_operations import TAG_NOT_FOUND
from app.services.tag_operations import TagMutationOutcome
from app.services.tag_operations import TagNotFoundError
from app.services.tag_operations import TagTenantMismatchError
from app.services.tag_scope import TagWriteRights
from app.services.tag_scope import tag_ownership
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import tags_by_uuid

router = APIRouter()

TAG_NAME_REQUIRED = "Tag name is required"
TENANT_MISMATCH = "Tags from different tenants cannot be merged"


def _tag_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=TAG_NOT_FOUND)


def _writable_tag_ids(
    db: Session,
    tag_uuids: list[UUID],
    *,
    user_id: int,
    can_write_system: bool,
    organization_id: int | None,
    is_org_admin: bool = False,
) -> list[int]:
    """Resolve tag uuids to ids the caller may rename, merge, delete or share.

    The rule is :class:`TagWriteRights`. A tag it refuses answers 404, never
    403, so probing cannot tell another tenant's tag from an unknown uuid.

    Returns:
        Ids in input order, duplicates kept.

    Raises:
        HTTPException: 404 "Tag not found" for any unknown or non-writable uuid.
    """
    rights = TagWriteRights(
        user_id=user_id,
        organization_id=organization_id,
        can_write_system=can_write_system,
        is_org_admin=is_org_admin,
    )
    rows = tags_by_uuid(db, tag_uuids)
    ids: list[int] = []
    for tag_uuid in tag_uuids:
        tag = rows.get(tag_uuid)
        if tag is None or not rights.permits(tag):
            raise _tag_not_found()
        ids.append(tag.id)
    return ids


def _write_rights(ctx: RequestContext, user_id: int) -> TagWriteRights:
    """The request's tag write rights, built from the same inputs every write route passes.

    A support grant refuses every tag write at the route, so under one nothing
    is reported writable.
    """
    return TagWriteRights(
        user_id=user_id,
        organization_id=ctx.org_id,
        can_write_system=ctx.bypass.deployment_scope_writable,
        is_org_admin=ctx.is_org_admin,
        refused=ctx.bypass.under_grant,
    )


def _writable_uuids(db: Session, tag_uuids: Iterable[UUID], rights: TagWriteRights) -> set[UUID]:
    """Which of ``tag_uuids`` the caller may write, loaded in one query."""
    return {
        tag_uuid for tag_uuid, tag in tags_by_uuid(db, tag_uuids).items() if rights.permits(tag)
    }


def _share_target(share: TagShare) -> TagShareTarget:
    """Wire form of one tag grant, named for display."""
    sharer = share.shared_by_user
    shared_by = (sharer.full_name or sharer.email or None) if sharer is not None else None
    if share.target_type == "user":
        user = share.target_user
        name = (user.full_name or user.email) if user is not None else None
        return TagShareTarget(
            uuid=share.uuid, target_type="user", display_name=name or "user", shared_by=shared_by
        )
    group = share.target_group
    return TagShareTarget(
        uuid=share.uuid,
        target_type="group",
        display_name=(group.name if group is not None else None) or "group",
        shared_by=shared_by,
    )


def _tag_response(tag: Tag, rights: TagWriteRights) -> TagSchema:
    """A tag as the caller sees it, labelled with its ownership and write right."""
    return TagSchema.model_validate(
        {
            "uuid": tag.uuid,
            "name": tag.name,
            "source": tag.source,
            "ownership": tag_ownership(tag, rights.user_id),
            "can_write": rights.permits(tag),
        }
    )


def _mutation_response(outcome: TagMutationOutcome, rights: TagWriteRights) -> TagMutationResult:
    """Serialize an operation's outcome, labelling its tag for the acting user."""
    result = TagMutationResult.model_validate(outcome)
    if outcome.tag is not None:
        result.tag = _tag_response(outcome.tag, rights)
    return result


@contextmanager
def _translated_tag_errors() -> Iterator[None]:
    """Map the tag services' refusals onto fixed HTTP answers (never their text)."""
    try:
        yield
    except TagNotFoundError as exc:
        raise _tag_not_found() from exc
    except TagTenantMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=TENANT_MISMATCH
        ) from exc
    except InvalidTagNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=TAG_NAME_REQUIRED
        ) from exc


def _invalid_name() -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=TAG_NAME_REQUIRED)
