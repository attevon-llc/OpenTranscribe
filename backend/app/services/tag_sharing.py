"""Sharing a tag with specific users and groups.

The middle tier the tag model was missing. Before this, a tag was either yours
alone or published to the entire deployment (``user_id IS NULL``) — so giving
one word to a colleague or a team meant publishing it to everybody, or letting
each person coin their own copy. The second is exactly the duplication this
feature exists to stop.

Mirrors collection sharing throughout: the same target shape (exactly one of
user or group), the same "direct grant OR group membership" read rule, and the
same revoke semantics. See :class:`app.models.sharing.TagShare` for why there is
no permission column — a tag share grants *vocabulary*, not administration.
"""

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import OpenTranscribeError
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.media import Tag
from app.models.sharing import TagShare
from app.models.user import User
from app.utils.tenant_sharing import group_members_outside_org
from app.utils.tenant_sharing import user_in_tenant

logger = logging.getLogger(__name__)

TARGET_USER = "user"
TARGET_GROUP = "group"


class TagShareError(OpenTranscribeError):
    """A share could not be created as asked."""


def list_shares(db: Session, tag_id: int) -> list[TagShare]:
    """Every grant on a tag, newest first."""
    return (
        db.query(TagShare)
        .filter(TagShare.tag_id == tag_id)
        .order_by(TagShare.created_at.desc(), TagShare.id.desc())
        .all()
    )


def share_tag(
    db: Session,
    tag: Tag,
    *,
    shared_by_id: int,
    target_user_id: int | None = None,
    target_group_id: int | None = None,
) -> TagShare:
    """Grant ``tag`` to one user or one group.

    Idempotent: re-sharing with the same target returns the existing grant
    rather than raising, so a double-click does not surface as an error. The
    partial unique indexes are what make that safe under a race — the second
    writer loses the insert and reads the winner back.

    Args:
        db: Database session. Committed here.
        tag: The tag being shared.
        shared_by_id: Who granted it — kept for attribution, and so revoking a
            user does not orphan the audit trail silently.
        target_user_id: Grant to this user, or
        target_group_id: grant to this group. Exactly one, never both.

    Returns:
        The grant, existing or new.

    Raises:
        TagShareError: Neither or both targets given, the target is unknown, the
            sharer is not in the target group, or the target reaches outside the
            organization an org tag belongs to.
    """
    if bool(target_user_id) == bool(target_group_id):
        raise TagShareError("A share names exactly one user or one group")

    if target_user_id is not None:
        if db.query(User).filter(User.id == target_user_id).first() is None:
            raise TagShareError("Unknown user")
        # Sharing with yourself is a no-op that would otherwise sit in the list
        # looking like a real grant.
        if target_user_id == tag.user_id:
            raise TagShareError("That tag already belongs to this user")
        # An organization's tag stays inside that organization.
        if tag.organization_id is not None and not user_in_tenant(
            db, target_user_id, int(tag.organization_id)
        ):
            raise TagShareError(
                "Cannot share an organization tag with a user who is not a member "
                "of that organization"
            )
    elif target_group_id is not None:
        if db.query(UserGroup).filter(UserGroup.id == target_group_id).first() is None:
            raise TagShareError("Unknown group")
        is_member = (
            db.query(UserGroupMember.id)
            .filter(
                UserGroupMember.group_id == target_group_id,
                UserGroupMember.user_id == shared_by_id,
            )
            .first()
        )
        if is_member is None:
            raise TagShareError("You must be a member of the group to share with it")
        if tag.organization_id is not None:
            outside = group_members_outside_org(db, int(target_group_id), int(tag.organization_id))
            if outside:
                raise TagShareError(
                    "Cannot share an organization tag with this group: "
                    f"{outside} group member(s) are not members of that organization"
                )

    existing = (
        db.query(TagShare)
        .filter(
            TagShare.tag_id == tag.id,
            TagShare.target_user_id == target_user_id,
            TagShare.target_group_id == target_group_id,
        )
        .first()
    )
    if existing is not None:
        return existing

    share = TagShare(
        tag_id=tag.id,
        shared_by_id=shared_by_id,
        target_type=TARGET_USER if target_user_id is not None else TARGET_GROUP,
        target_user_id=target_user_id,
        target_group_id=target_group_id,
    )
    db.add(share)
    try:
        db.commit()
    except IntegrityError:
        # Lost the race on `_tag_share_*_uc`; the winner's grant is equivalent.
        db.rollback()
        winner = (
            db.query(TagShare)
            .filter(
                TagShare.tag_id == tag.id,
                TagShare.target_user_id == target_user_id,
                TagShare.target_group_id == target_group_id,
            )
            .first()
        )
        if winner is None:
            raise
        return winner
    db.refresh(share)
    return share


def revoke_share(db: Session, tag_id: int, share_uuid) -> bool:
    """Remove one grant. Returns False when it was already gone.

    Revoking does not touch the tag or any association: a recipient who applied
    the tag to their own files keeps those files tagged. Only their ability to
    reach the tag *by name* in the picker goes away — and any file they tagged
    with it keeps the tag visible to them through the file arm of
    ``visible_to``, which is correct: they can still see the word on their own
    media.
    """
    share = (
        db.query(TagShare).filter(TagShare.tag_id == tag_id, TagShare.uuid == share_uuid).first()
    )
    if share is None:
        return False
    db.delete(share)
    db.commit()
    return True
