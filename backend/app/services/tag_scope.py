"""Tag tenancy and visibility predicates — who a tag belongs to and who may see it.

The scoping half of the tag plane, split out of :mod:`app.services.tag_service`
(which keeps name resolution and the :func:`~app.services.tag_service.on_tags_changed`
hook). Every tenant boundary a tag query draws is drawn here:

* :func:`owned_or_system` — the narrow scope a name may be **resolved** against
  (the request's tenant plus the system vocabulary).
* :func:`visible_to` — the wider, read-only scope behind ``GET /tags``: shares and
  tags on accessible files, every arm bounded to the request's tenant.
* :func:`tag_ownership` / :func:`tenant_key` — classification of a row.
* :func:`accessible_file_ids_subquery` — the accessible-file gate that the
  impact preview, the usage counts and the list all share, so a confirmation
  dialog and the list it was opened from cannot disagree about scope.

These are the small predicates the whole tag plane agrees on, kept in one place so
``tag_service``, ``tag_operations`` and ``tag_collisions`` cannot drift into two
answers for one question.
"""

from typing import Literal

from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import MediaFile
from app.models.tag import FileTag
from app.models.tag import Tag


def accessible_file_ids_subquery(db: Session, user_id: int, organization_id: OrgScope = UNSCOPED):
    """Build the caller's accessible-file subquery (the same gate as ``GET /tags``).

    Every tag surface that scopes a count to what the caller can see — the
    impact preview, the usage counts behind the list — goes through this, so a
    confirmation dialog and the list it was opened from cannot disagree about
    which files are in scope.

    Args:
        db: Database session.
        user_id: The acting user.
        organization_id: Tenant scope (``None`` = personal, ``UNSCOPED`` =
            legacy caller, no gate).

    Returns:
        The subquery of accessible file ids, ready to wrap in ``select()``.
    """
    from app.services.permission_service import PermissionService

    return PermissionService.get_accessible_file_ids_subquery(
        db, user_id, organization_id=organization_id
    )


#: The caller's relationship to a tag — the single vocabulary the whole plane
#: uses. These are **both** the values ``Tag.ownership`` reports on the wire and
#: the values ``GET /tags?scope=`` accepts, deliberately: a filter whose terms
#: differ from the field it filters on is the kind of mismatch nobody notices
#: until the two disagree. The invariant is directly testable — every row a
#: scoped request returns must carry that same ownership.
#:
#: Not a boolean. ``is_shared`` already means "shared *with* me, not mine" on
#: ``CollectionWithCount``; reusing it here for "in the shared vocabulary" gave
#: one name two opposite meanings in one schema module.
#: Typed so the value flows into the schemas' ``Literal`` field without a cast:
#: ``TagOnSelection.ownership`` and ``GET /tags?scope=`` both declare these three
#: strings, and a bare ``str`` return made every call site an mypy ``arg-type``
#: error.
TagOwnership = Literal["mine", "system", "shared_with_me"]

OWNERSHIP_MINE: TagOwnership = "mine"
OWNERSHIP_SYSTEM: TagOwnership = "system"
OWNERSHIP_SHARED_WITH_ME: TagOwnership = "shared_with_me"
TAG_OWNERSHIPS = (OWNERSHIP_MINE, OWNERSHIP_SYSTEM, OWNERSHIP_SHARED_WITH_ME)


def is_system_tag(tag: Tag) -> bool:
    """Whether ``tag`` is system vocabulary — no owner **and** no tenant.

    ``user_id IS NULL`` alone is not enough since v420: an organization tag
    whose creator's account is gone keeps its org and loses its ``user_id``.
    """
    return tag.user_id is None and tag.organization_id is None


def system_tag() -> ColumnElement[bool]:
    """SQL form of :func:`is_system_tag`."""
    return and_(Tag.user_id.is_(None), Tag.organization_id.is_(None))


def in_tenant(user_id: int, organization_id: int | None) -> ColumnElement[bool]:
    """Tags belonging to the request's tenant, system vocabulary excluded.

    Organization context: every tag of that org, whoever created it — that is
    the point of org tags. Personal scope: the caller's own tags that belong to
    no organization.
    """
    if organization_id is not None:
        return Tag.organization_id == organization_id
    return and_(Tag.user_id == user_id, Tag.organization_id.is_(None))


def tag_in_tenant(tag: Tag, user_id: int, organization_id: int | None) -> bool:
    """Python form of :func:`in_tenant`, for a row already loaded."""
    if organization_id is not None:
        return tag.organization_id == organization_id
    return tag.organization_id is None and tag.user_id == user_id


def tenant_first():
    """ORDER BY terms putting a tenant row ahead of a same-named system row.

    ``organization_id`` then ``user_id``, both ASC NULLS LAST (Postgres's
    default for ASC): an org row beats the system row in org context, and an
    owned row beats it in personal scope.
    """
    return (Tag.organization_id, Tag.user_id)


def tag_ownership(tag: Tag, user_id: int) -> TagOwnership:
    """Classify a tag by the caller's relationship to it.

    The single definition, so the list, the collision clusters, and the
    file-detail payload cannot drift into three answers. Maps 1:1 onto the
    three visibility arms and onto what the caller may do:

    ===================  ==========================  ==================
    ``ownership``        Rule                        May mutate?
    ===================  ==========================  ======================
    ``mine``             ``Tag.user_id == user_id``  yes
    ``system``           no owner and no tenant      admin only
    ``shared_with_me``   anything else visible       no (404) — except an
                                                     org admin on an org tag
    ===================  ==========================  ======================

    ``shared_with_me`` covers a tag on a file someone shared with the caller, a
    tag granted through ``tag_share``, and — since v420 — a colleague's tag in
    the caller's organization. All three are visible and appliable; none is the
    caller's to rename, so the UI does not offer a rename that can only 404.

    Args:
        tag: The tag row.
        user_id: The acting user.

    Returns:
        One of :data:`TAG_OWNERSHIPS`.
    """
    if is_system_tag(tag):
        return OWNERSHIP_SYSTEM
    if tag.user_id == user_id:
        return OWNERSHIP_MINE
    return OWNERSHIP_SHARED_WITH_ME


def owned_or_system(user_id: int, organization_id: int | None) -> ColumnElement[bool]:
    """Predicate for the tags a request may **resolve a name against**.

    The request's tenant (:func:`in_tenant`) plus the system vocabulary. This is
    the narrow scope; ``GET /tags`` widens it to shared tags and to tags on an
    accessible file, which is a read-only right (:func:`visible_to`). Which of
    these rows a caller may *rename* is narrower still — see
    ``endpoints/tags/_common._writable_tag_ids``.

    ``organization_id`` is required, not defaulted: a caller that forgot it
    would silently resolve an org member's typed name into their personal
    vocabulary — the cross-tenant bug v420 exists to close.
    """
    return or_(in_tenant(user_id, organization_id), system_tag())


def shared_with(user_id: int):
    """Subquery of tag ids explicitly shared with ``user_id``, direct or via a group.

    The same two arms ``PermissionService`` applies to collections: a grant
    naming the user, or naming a group they belong to. Kept here so the read
    predicate and the "who is this shared with" listing cannot drift apart.
    """
    from sqlalchemy import select

    from app.models.group import UserGroupMember
    from app.models.sharing import TagShare

    member_groups = select(UserGroupMember.group_id).where(UserGroupMember.user_id == user_id)
    return select(TagShare.tag_id).where(
        or_(
            TagShare.target_user_id == user_id,
            TagShare.target_group_id.in_(member_groups),
        )
    )


def _tenant_bound(organization_id: int | None) -> ColumnElement[bool]:
    """The tenant a *widened* read arm may still reach: never another tenant's row."""
    if organization_id is not None:
        return Tag.organization_id == organization_id
    return Tag.organization_id.is_(None)


def visible_to(db: Session, user_id: int, organization_id: int | None) -> ColumnElement[bool]:
    """Predicate for the tags ``user_id`` is allowed to **read** in a tenant.

    Every arm is bounded to the request's tenant (plus system rows), so a tag
    never surfaces in another tenant — not through a share, and not through a
    file that legacy data left carrying another tenant's row.

    Wider than :func:`owned_or_system` by two arms:

    * a tag **explicitly shared** with the caller, directly or through a group
      (``v386_add_tag_share``). The point of sharing a tag is that the recipient
      uses that same word instead of coining a duplicate.
    * a tag attached to a file the caller can access. Tagging a shared file has
      to put that word in the recipient's picker, or they cannot filter by what
      they are looking at.

    Reading and rewriting are different rights — mutation stays on the narrow
    scope (``endpoints/tags.py:_writable_tag_ids``), since renaming a tag you can
    merely see rewrites its owner's vocabulary everywhere they use it. A share
    grants vocabulary, not administration.

    ``get_accessible_file_ids_subquery`` already covers files shared directly and
    via groups and applies the org tenant gate, so the file arm needs no second
    rule.

    ``get_accessible_file_ids_subquery`` does NOT know about quarantine — it
    covers ownership/sharing only — so the file arm excludes a quarantined
    file's own id explicitly (A2's leak class): without this, a same-named
    tag owned by someone ELSE and attached only to a file shared with the
    caller would keep surfacing in the caller's tag list after that file was
    taken down, even though the file itself now 404s for them.
    """
    from sqlalchemy import select

    quarantined_files = select(MediaFile.id).where(MediaFile.is_quarantined.is_(True))
    attached_to_accessible = select(FileTag.tag_id).where(
        FileTag.media_file_id.in_(
            select(accessible_file_ids_subquery(db, user_id, organization_id))
        ),
        FileTag.media_file_id.not_in(quarantined_files),
    )
    return or_(
        owned_or_system(user_id, organization_id),
        and_(
            _tenant_bound(organization_id),
            or_(Tag.id.in_(shared_with(user_id)), Tag.id.in_(attached_to_accessible)),
        ),
    )


def tenant_key(tag: Tag) -> tuple:
    """The tenant a tag belongs to: system, one organization, or one user's personal space."""
    if is_system_tag(tag):
        return ("system",)
    if tag.organization_id is not None:
        return ("org", int(tag.organization_id))
    return ("user", tag.user_id)
