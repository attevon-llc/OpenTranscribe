import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.constants import TAG_SOURCE_MANUAL
from app.models.media import Tag
from app.schemas.media import TagMutationResult
from app.schemas.media import TagShareTarget
from app.services.tag_operations import TagNotFoundError
from app.services.tag_operations import TagTenantMismatchError
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import is_system_tag
from app.services.tag_service import resolve_or_create_tag
from app.services.tag_service import tag_in_tenant
from app.services.tag_service import visible_to
from app.utils.uuid_helpers import get_by_uuid

logger = logging.getLogger(__name__)


def _visible_to(db: Session, user_id: int, organization_id: int | None) -> ColumnElement[bool]:
    """Predicate for the tags ``user_id`` may see in the request's tenant.

    Delegates to ``tag_service.visible_to`` — the single definition, bounded to
    the tenant on every arm (issue #1050). This used to be a second, hand-kept
    copy that lacked the share arm; two answers to "can I see this tag" is the
    drift that copy invited.
    """
    return visible_to(db, user_id, organization_id)


def _resolve_tag(db: Session, name: str, user_id: int, organization_id: int | None) -> Tag:
    """Resolve a user-supplied name to a tag of the tenant, mapping a blank name to a 422.

    Resolution is normalized-exact (``app/services/tag_service.py``) and scoped
    to ``organization_id``'s vocabulary plus the system one, so a typed name can
    never resolve onto another tenant's row — and in an organization it resolves
    onto a colleague's row rather than coining a duplicate. A near match is never
    applied here — a person typed this name, so a fuzzy hit may only ever be
    offered as a suggestion.
    """
    try:
        return resolve_or_create_tag(
            db, name, user_id=user_id, organization_id=organization_id, source=TAG_SOURCE_MANUAL
        )
    except InvalidTagNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tag name is required",
        ) from exc


def _writable_tag_ids(
    db: Session,
    tag_uuids: list[UUID],
    *,
    user_id: int,
    is_admin: bool,
    organization_id: int | None,
    is_org_admin: bool = False,
) -> list[int]:
    """Resolve public tag UUIDs to internal ids the caller may **mutate**.

    Reading a tag and rewriting it are different rights. ``_visible_to`` admits
    tags shared with you, on files shared with you, and — in an organization —
    every colleague's tag, but renaming or deleting one rewrites it for everyone
    who uses it, so mutation is narrower:

    * your own tag, **in the request's tenant** — a tag you made in another
      organization is not reachable from this one (issue #1050);
    * any tag of the request's organization, for an **org admin** — the org's
      vocabulary is theirs to curate;
    * a system tag, for a deployment admin only (they are the shared vocabulary
      every account's picker shows, which is why ``cleanup_unused_tags`` is
      admin-gated and skips them).

    A tag that exists but is not writable 404s rather than 403s — the same answer
    an unknown UUID gets, so probing this endpoint cannot enumerate other
    accounts' or tenants' tags.
    """
    ids: list[int] = []
    for tag_uuid in tag_uuids:
        tag = get_by_uuid(db, Tag, tag_uuid, error_message="Tag not found")
        if is_system_tag(tag):
            allowed = is_admin
        elif not tag_in_tenant(tag, user_id, organization_id):
            allowed = False
        else:
            allowed = tag.user_id == user_id or (tag.organization_id is not None and is_org_admin)
        if not allowed:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
        ids.append(tag.id)
    return ids


def _share_target(share) -> TagShareTarget:
    """Project a grant onto the wire, naming the target rather than its id."""
    kind: Literal["user", "group"]
    if share.target_user_id is not None:
        target = share.target_user
        name = getattr(target, "full_name", None) or getattr(target, "email", "") or "user"
        kind = "user"
    else:
        target = share.target_group
        name = getattr(target, "name", "") or "group"
        kind = "group"
    shared_by = getattr(share.shared_by_user, "full_name", None) or getattr(
        share.shared_by_user, "email", None
    )
    return TagShareTarget(uuid=share.uuid, target_type=kind, display_name=name, shared_by=shared_by)


def _apply(operation, *args, result_model=TagMutationResult, **kwargs):
    """Run a tag operation, translating its service errors into HTTP ones."""
    try:
        return result_model.model_validate(operation(*args, **kwargs))
    except TagNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except TagTenantMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tags from different tenants cannot be merged",
        ) from exc
    except InvalidTagNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tag name is required",
        ) from exc


#: The single router every module registers onto.
#: Sub-routers were tried and rejected: FastAPI refuses an empty path when a
#: router is included without a prefix, and `POST ""` / `GET ""` are real routes
#: here (the prefix is applied once, at mount time in api/router.py).
router = APIRouter()
