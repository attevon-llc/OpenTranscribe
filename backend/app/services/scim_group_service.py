"""Group writes for the SCIM surface (``/scim/v2/Groups``).

Split out of :mod:`app.services.scim_service`, whose docstring carries the rules both
halves obey; the shared refusal types, the audit helper and the source constants are
imported from there so there is still one implementation of each.

The rule specific to this file: **only ``source='scim'`` membership rows are ever
removed.** A row somebody added by hand in the groups UI, or one a directory pass
created, survives a SCIM removal — the mirror image of
``idp_group_mapping_service._reconcile_memberships``, which never touches a ``scim``
row. Whoever wrote the membership owns it.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.models.group import MEMBERSHIP_SOURCE_SCIM
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.services.scim_service import SCIMConflictError
from app.services.scim_service import _audit

logger = logging.getLogger(__name__)


def create_group(db: Session, *, display_name: str, owner_id: int, actor: str) -> UserGroup:
    """Create a group from ``POST /Groups``.

    *owner_id* is the administrator who issued the SCIM token — ``user_group.owner_id``
    is NOT NULL and a group has to belong to somebody who can see it in the groups UI.
    When that account has since been deleted the caller falls back to a super_admin;
    see ``api/endpoints/scim/groups.py``.

    Raises:
        SCIMConflictError: That owner already has a group with this name
            (``_user_group_owner_name_uc``).
    """
    group = UserGroup(name=display_name.strip(), owner_id=owner_id)
    db.add(group)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise SCIMConflictError(f"A group named {display_name!r} already exists") from exc
    db.refresh(group)
    _audit(
        AuditEventType.ADMIN_SETTINGS_CHANGE,
        actor=actor,
        target=display_name,
        action="scim_group_create",
        group_uuid=str(group.uuid),
    )
    return group


def set_group_members(db: Session, group: UserGroup, user_ids: set[int], *, actor: str) -> None:
    """Replace the group's SCIM-owned membership with exactly *user_ids*.

    Only rows whose ``source`` is ``scim`` are removed. A ``manual`` row (someone
    added them in the groups UI) and a directory-derived row are both left alone —
    the same ownership rule ``idp_group_mapping_service`` applies in the other
    direction, so the two systems can manage the same group without fighting.
    """
    existing = db.query(UserGroupMember).filter(UserGroupMember.group_id == group.id).all()
    scim_rows = {int(m.user_id): m for m in existing if str(m.source) == MEMBERSHIP_SOURCE_SCIM}
    already = {int(m.user_id) for m in existing}

    for user_id in sorted(user_ids - already):
        db.add(
            UserGroupMember(
                group_id=group.id,
                user_id=user_id,
                role="member",
                source=MEMBERSHIP_SOURCE_SCIM,
            )
        )
    for user_id in sorted(set(scim_rows) - user_ids):
        db.delete(scim_rows[user_id])
    db.commit()
    _audit(
        AuditEventType.ADMIN_SETTINGS_CHANGE,
        actor=actor,
        target=str(group.name),
        action="scim_group_members_set",
        group_uuid=str(group.uuid),
        member_count=len(user_ids),
    )


def add_group_members(db: Session, group: UserGroup, user_ids: set[int], *, actor: str) -> None:
    """Add SCIM-owned memberships, leaving every existing row untouched."""
    present = {
        int(m.user_id)
        for m in db.query(UserGroupMember).filter(UserGroupMember.group_id == group.id).all()
    }
    added = sorted(user_ids - present)
    for user_id in added:
        db.add(
            UserGroupMember(
                group_id=group.id,
                user_id=user_id,
                role="member",
                source=MEMBERSHIP_SOURCE_SCIM,
            )
        )
    db.commit()
    if added:
        _audit(
            AuditEventType.ADMIN_SETTINGS_CHANGE,
            actor=actor,
            target=str(group.name),
            action="scim_group_members_add",
            group_uuid=str(group.uuid),
            member_count=len(added),
        )


def remove_group_members(db: Session, group: UserGroup, user_ids: set[int], *, actor: str) -> None:
    """Remove SCIM-owned memberships only.

    A membership somebody created by hand survives a SCIM removal, for the same
    reason it survives a directory pass: whoever wrote the row owns it.
    """
    if not user_ids:
        return
    removed = (
        db.query(UserGroupMember)
        .filter(
            UserGroupMember.group_id == group.id,
            UserGroupMember.user_id.in_(user_ids),
            UserGroupMember.source == MEMBERSHIP_SOURCE_SCIM,
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    if removed:
        _audit(
            AuditEventType.ADMIN_SETTINGS_CHANGE,
            actor=actor,
            target=str(group.name),
            action="scim_group_members_remove",
            group_uuid=str(group.uuid),
            member_count=int(removed),
        )
