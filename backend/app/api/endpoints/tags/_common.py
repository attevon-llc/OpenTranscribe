"""Pieces every tag route module shares: the router, the write gate, response helpers.

All route modules register onto the one :data:`router`; the package
``__init__`` imports them in the order their paths must be matched.
"""

from __future__ import annotations

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
from app.services.tag_operations import TAG_NOT_FOUND
from app.services.tag_operations import TagMutationOutcome
from app.services.tag_operations import TagNotFoundError
from app.services.tag_operations import TagTenantMismatchError
from app.services.tag_scope import is_system_tag
from app.services.tag_scope import tag_in_tenant
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

    A system tag needs ``can_write_system``. Any other tag must be in the request
    tenant and be the caller's own, or an organization tag with the caller an
    org admin. Anything else answers 404, never 403, so probing cannot tell
    another tenant's tag from an unknown uuid.

    Returns:
        Ids in input order, duplicates kept.

    Raises:
        HTTPException: 404 "Tag not found" for any unknown or non-writable uuid.
    """
    rows = tags_by_uuid(db, tag_uuids)
    ids: list[int] = []
    for tag_uuid in tag_uuids:
        tag = rows.get(tag_uuid)
        if tag is None:
            raise _tag_not_found()
        if is_system_tag(tag):
            allowed = can_write_system
        else:
            allowed = tag_in_tenant(tag, user_id, organization_id) and (
                tag.user_id == user_id or (tag.organization_id is not None and is_org_admin)
            )
        if not allowed:
            raise _tag_not_found()
        ids.append(tag.id)
    return ids


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


def _tag_response(tag: Tag, user_id: int) -> TagSchema:
    """A tag as the caller sees it, ownership included, exactly as ``GET /tags`` labels it."""
    return TagSchema.model_validate(
        {
            "uuid": tag.uuid,
            "name": tag.name,
            "source": tag.source,
            "ownership": tag_ownership(tag, user_id),
        }
    )


def _mutation_response(outcome: TagMutationOutcome, user_id: int) -> TagMutationResult:
    """Serialize an operation's outcome, labelling its tag for the acting user."""
    result = TagMutationResult.model_validate(outcome)
    if outcome.tag is not None:
        result.tag = _tag_response(outcome.tag, user_id)
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
