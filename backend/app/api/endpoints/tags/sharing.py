"""Granting a tag to specific users and groups (v386).

Shared helpers and the visibility rules live in ``_common``.
"""

from uuid import UUID

from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.tags._common import _share_target
from app.api.endpoints.tags._common import _writable_tag_ids
from app.api.endpoints.tags._common import router
from app.db.base import get_db
from app.models.media import Tag
from app.models.user import User
from app.schemas.media import TagShareCreate
from app.schemas.media import TagShareTarget
from app.services.tag_service import on_tags_changed
from app.services.tag_sharing import TagShareError
from app.services.tag_sharing import list_shares
from app.services.tag_sharing import revoke_share
from app.services.tag_sharing import share_tag
from app.utils.uuid_helpers import get_by_uuid


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
