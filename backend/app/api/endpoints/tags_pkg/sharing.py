import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.api.endpoints.auth import get_current_active_user
from app.core.constants import TAG_SOURCE_MANUAL
from app.db.base import get_db
from app.models.media import FileTag
from app.models.media import Tag
from app.models.user import User
from app.schemas.media import TagMutationResult
from app.schemas.media import TagShareCreate
from app.schemas.media import TagShareTarget
from app.services.tag_operations import TagNotFoundError
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import accessible_file_ids_subquery
from app.services.tag_service import on_tags_changed
from app.services.tag_service import resolve_or_create_tag
from app.services.tag_sharing import TagShareError
from app.services.tag_sharing import list_shares
from app.services.tag_sharing import revoke_share
from app.services.tag_sharing import share_tag
from app.utils.uuid_helpers import get_by_uuid

logger = logging.getLogger(__name__)


def _owned_or_system(user_id: int) -> ColumnElement[bool]:
    """Tags the user may write against: their own, plus the system vocabulary."""
    return or_(Tag.user_id == user_id, Tag.user_id.is_(None))


def _visible_to(db: Session, user_id: int, organization_id: Any) -> ColumnElement[bool]:
    """Predicate for the tags ``user_id`` is allowed to see.

    A tag is visible when it is a system tag (``user_id IS NULL``), owned by the
    caller, or attached to a file the caller can access.
    ``get_accessible_file_ids_subquery`` already covers files shared directly and
    via groups and applies the org tenant gate, so sharing needs no extra rule
    here — do not add a parallel one.
    """
    accessible_files = accessible_file_ids_subquery(db, user_id, organization_id)
    attached_to_accessible = select(FileTag.tag_id).where(
        FileTag.media_file_id.in_(select(accessible_files))
    )
    return or_(_owned_or_system(user_id), Tag.id.in_(attached_to_accessible))


def _resolve_tag(db: Session, name: str, user_id: int) -> Tag:
    """Resolve a user-supplied name to a tag, mapping a blank name to a 422.

    Resolution is normalized-exact (``app/services/tag_service.py``) and scoped
    to the caller's own vocabulary plus the system one, so a typed name can never
    resolve onto another account's row. A near match is never applied here — a
    person typed this name, so a fuzzy hit may only ever be offered as a
    suggestion.
    """
    try:
        return resolve_or_create_tag(db, name, user_id=user_id, source=TAG_SOURCE_MANUAL)
    except InvalidTagNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tag name is required",
        ) from exc


def _writable_tag_ids(
    db: Session, tag_uuids: list[UUID], *, user_id: int, is_admin: bool
) -> list[int]:
    """Resolve public tag UUIDs to internal ids the caller may **mutate**.

    Reading a tag and rewriting it are different rights. ``_visible_to`` admits
    any tag attached to a file shared with you, but renaming or deleting one of
    those rewrites its owner's vocabulary everywhere they use it, so mutation is
    narrower: your own tags always, system tags for an admin only (they are the
    shared vocabulary every account's picker shows, which is why
    ``cleanup_unused_tags`` is admin-gated and skips them).

    A tag that exists but is not writable 404s rather than 403s — the same answer
    an unknown UUID gets, so probing this endpoint cannot enumerate other
    accounts' tags.
    """
    ids: list[int] = []
    for tag_uuid in tag_uuids:
        tag = get_by_uuid(db, Tag, tag_uuid, error_message="Tag not found")
        owned = tag.user_id == user_id
        system = tag.user_id is None
        if not (owned or (system and is_admin)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
        ids.append(tag.id)
    return ids


def _share_target(share) -> TagShareTarget:
    """Project a grant onto the wire, naming the target rather than its id."""
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
    except InvalidTagNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tag name is required",
        ) from exc


router = APIRouter()

router = APIRouter()


@router.get("/{tag_uuid}/shares", response_model=list[TagShareTarget])
def list_tag_shares(
    tag_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Who this tag is shared with. Owner (or admin, for a system tag) only.

    Reuses the writable gate: who you have given a tag to is the owner's
    business, not every recipient's.
    """
    tag_id = _writable_tag_ids(
        db, [tag_uuid], user_id=current_user.id, is_admin=current_user.is_admin
    )[0]
    return [_share_target(share) for share in list_shares(db, tag_id)]


@router.post("/{tag_uuid}/shares", response_model=TagShareTarget)
def create_tag_share(
    tag_uuid: UUID,
    payload: TagShareCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Share a tag with one user or one group.

    The middle tier between "mine alone" and "published to the whole
    deployment": the recipient can see the tag, filter by it and apply it, so
    they use your word instead of coining a duplicate. Renaming, merging and
    deleting stay with you.
    """
    tag_id = _writable_tag_ids(
        db, [tag_uuid], user_id=current_user.id, is_admin=current_user.is_admin
    )[0]
    tag = db.query(Tag).filter(Tag.id == tag_id).one()

    target_user_id = None
    target_group_id = None
    if payload.target_user_uuid is not None:
        target_user_id = get_by_uuid(
            db, User, payload.target_user_uuid, error_message="User not found"
        ).id
    if payload.target_group_uuid is not None:
        from app.models.group import UserGroup

        target_group_id = get_by_uuid(
            db, UserGroup, payload.target_group_uuid, error_message="Group not found"
        ).id

    try:
        share = share_tag(
            db,
            tag,
            shared_by_id=current_user.id,
            target_user_id=target_user_id,
            target_group_id=target_group_id,
        )
    except TagShareError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    on_tags_changed(db, user_id=current_user.id)
    return _share_target(share)


@router.delete("/{tag_uuid}/shares/{share_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_tag_share(
    tag_uuid: UUID,
    share_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Revoke one grant.

    The tag and every association survive: a recipient who applied it to their
    own files keeps those files tagged, and still sees the word on their own
    media through the file arm of ``visible_to``. Only reaching it by name in
    the picker goes away.
    """
    tag_id = _writable_tag_ids(
        db, [tag_uuid], user_id=current_user.id, is_admin=current_user.is_admin
    )[0]
    if not revoke_share(db, tag_id, share_uuid):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found")
    on_tags_changed(db, user_id=current_user.id)
