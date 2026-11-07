"""Tenancy and visibility rules for tags.

A tag belongs to exactly one of three tenants:

* **system**: no owner and no organization. Shared vocabulary every account sees.
* **organization**: ``organization_id`` set. ``user_id`` only records who created
  it and may be NULL once that account is deleted, so a NULL owner alone never
  makes a tag a system tag.
* **personal**: no organization, owner set.

Every predicate the tag plane uses to decide which tenant's rows a request may
resolve, list or mutate is defined here once, in both SQL and Python form where
both are needed, so the two can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Final
from typing import Literal

from sqlalchemy import ColumnElement
from sqlalchemy import Select
from sqlalchemy import Subquery
from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.group import UserGroupMember
from app.models.media import MediaFile
from app.models.sharing import TagShare
from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.file_availability import available_pred
from app.services.permission_service import PermissionService

TagOwnership = Literal["mine", "system", "shared_with_me"]

OWNERSHIP_MINE: Final = "mine"
OWNERSHIP_SYSTEM: Final = "system"
OWNERSHIP_SHARED_WITH_ME: Final = "shared_with_me"

#: The ownership labels in wire order; also the values of ``Tag.ownership``.
TAG_OWNERSHIPS: Final[tuple[TagOwnership, ...]] = (
    OWNERSHIP_MINE,
    OWNERSHIP_SYSTEM,
    OWNERSHIP_SHARED_WITH_ME,
)


def is_system_tag(tag: Any) -> bool:
    """Whether ``tag`` is shared system vocabulary (no owner and no organization)."""
    return tag.user_id is None and tag.organization_id is None


def system_tag() -> ColumnElement[bool]:
    """SQL form of :func:`is_system_tag`."""
    return and_(Tag.user_id.is_(None), Tag.organization_id.is_(None))


def in_tenant(user_id: int, organization_id: int | None) -> ColumnElement[bool]:
    """Tags that belong to the request tenant, excluding system tags.

    Args:
        user_id: The caller.
        organization_id: The request tenant; None is the caller's personal space.

    Returns:
        In an organization, every tag of that organization whoever created it;
        in personal scope, only the caller's own organization-less tags.
    """
    if organization_id is not None:
        return Tag.organization_id == organization_id
    return and_(Tag.user_id == user_id, Tag.organization_id.is_(None))


def tag_in_tenant(tag: Any, user_id: int, organization_id: int | None) -> bool:
    """Python form of :func:`in_tenant` for an already loaded row."""
    if organization_id is not None:
        return bool(tag.organization_id == organization_id)
    return tag.organization_id is None and tag.user_id == user_id


@dataclass(frozen=True)
class TagWriteRights:
    """Who may rename, merge, delete or share which tag, for one request.

    The one rule the write routes enforce and the read routes report as
    ``can_write``. A system tag needs ``can_write_system``. Any other tag must
    be in the request tenant and be the caller's own, or an organization tag
    with the caller an org admin. ``refused`` turns every write off (a support
    grant, whose requests may not change tenant content).
    """

    user_id: int
    organization_id: int | None
    can_write_system: bool
    is_org_admin: bool = False
    refused: bool = False

    def permits(self, tag: Any) -> bool:
        """Whether the caller may change ``tag``."""
        if self.refused:
            return False
        if is_system_tag(tag):
            return self.can_write_system
        return tag_in_tenant(tag, self.user_id, self.organization_id) and (
            tag.user_id == self.user_id or (tag.organization_id is not None and self.is_org_admin)
        )


def tenant_first() -> tuple[InstrumentedAttribute[int | None], InstrumentedAttribute[int | None]]:
    """ORDER BY terms that put a tenant row ahead of a same-named system row.

    Both columns sort ascending with Postgres' default NULLS LAST, and a system
    row is NULL in both. Splat into ``order_by(*tenant_first())``.
    """
    return (Tag.organization_id, Tag.user_id)


def tag_ownership(tag: Any, user_id: int) -> TagOwnership:
    """Classify ``tag`` relative to the caller.

    Returns:
        ``"system"`` for a system tag, ``"mine"`` when the caller created it,
        otherwise ``"shared_with_me"`` (a colleague's or unattributed org tag,
        or one reached through a share or a shared file).
    """
    if is_system_tag(tag):
        return OWNERSHIP_SYSTEM
    if tag.user_id == user_id:
        return OWNERSHIP_MINE
    return OWNERSHIP_SHARED_WITH_ME


def owned_or_system(user_id: int, organization_id: int | None) -> ColumnElement[bool]:
    """The request tenant's tags plus the system vocabulary.

    This is the scope resolution, suggestions and the collision pass work in.
    """
    return or_(in_tenant(user_id, organization_id), system_tag())


def shared_with(user_id: int) -> Select[tuple[int]]:
    """Ids of tags granted to ``user_id`` directly or through one of their groups."""
    groups = select(UserGroupMember.group_id).where(UserGroupMember.user_id == user_id)
    return select(TagShare.tag_id).where(
        or_(TagShare.target_user_id == user_id, TagShare.target_group_id.in_(groups))
    )


def _tenant_bound(organization_id: int | None) -> ColumnElement[bool]:
    if organization_id is not None:
        return Tag.organization_id == organization_id
    return Tag.organization_id.is_(None)


def tenant_bound_or_system(organization_id: int | None) -> ColumnElement[bool]:
    """Tags a file of tenant ``organization_id`` may carry.

    Any tag of that organization (any creator), or for a personal file any
    personal tag (any owner), plus system tags.
    """
    return or_(_tenant_bound(organization_id), system_tag())


def accessible_file_ids_subquery(
    db: Session, user_id: int, organization_id: OrgScope = UNSCOPED
) -> Subquery:
    """Subquery of the file ids ``user_id`` may open in ``organization_id``.

    Covers ownership, collection shares, group shares and the tenant gate, but
    not quarantine. The method is resolved on each call so a test can replace it.
    """
    return PermissionService.get_accessible_file_ids_subquery(
        db, user_id, organization_id=organization_id
    )


def visible_to(db: Session, user_id: int, organization_id: int | None) -> ColumnElement[bool]:
    """Tags the caller may read in the request tenant.

    The tenant's own tags and system tags are always readable. A tag of the
    same tenant created by someone else is readable when it was shared with the
    caller, or when it sits on an available file the caller may open (``file_availability``). A
    share or a shared file never carries a tag across the tenant boundary.
    """
    accessible = accessible_file_ids_subquery(db, user_id, organization_id)
    on_open_file = (
        select(FileTag.tag_id)
        .join(MediaFile, MediaFile.id == FileTag.media_file_id)
        .where(
            FileTag.media_file_id.in_(select(accessible)),
            available_pred(),
        )
    )
    return or_(
        owned_or_system(user_id, organization_id),
        and_(
            _tenant_bound(organization_id),
            or_(Tag.id.in_(shared_with(user_id)), Tag.id.in_(on_open_file)),
        ),
    )


def tenant_key(tag: Any) -> tuple[Any, ...]:
    """A hashable name for the tenant ``tag`` belongs to.

    Returns:
        ``("system",)``, ``("org", org_id)`` or ``("user", user_id)``.
    """
    if tag.organization_id is not None:
        return ("org", int(tag.organization_id))
    if tag.user_id is None:
        return ("system",)
    return ("user", tag.user_id)
