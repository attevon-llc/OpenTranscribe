"""Tenant gates for per-user items shared "with everyone".

Media sources, organization context, LLM and ASR provider configurations, summary prompts.

Most of these items carry no ``organization_id`` of their own: a user flips ``is_shared`` and the
item becomes visible to other users. "Other users" means **the owner's tenant**, never the
whole instance — the same rule ``groups._same_tenant`` and ``GET /users/search`` apply:

* **Org context** (``org_id`` is an int): shared items whose owner is a member of THAT
  organization.
* **Personal scope** (``org_id`` is None): shared items whose owner belongs to no
  organization at all.

Community-edition invariance: the membership table is empty there, so every owner passes
the personal-scope gate and instance-wide sharing behaves exactly as before.

Because the item is not tenant-stamped, an owner who belongs to several organizations
shares with members of each of them — every one of those viewers could already see the
owner in their tenant directory.
"""

from typing import Any

from sqlalchemy import and_
from sqlalchemy import exists
from sqlalchemy import or_
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.models.organization import OrganizationMembership


def owner_in_tenant(owner_id_col: Any, org_id: int | None) -> ColumnElement[bool]:
    """SQL predicate: the row's owner (``owner_id_col``) belongs to tenant ``org_id``.

    For request paths, where the caller's ACTIVE tenant is known (``ctx.org_id``).
    """
    if org_id is not None:
        return exists().where(
            OrganizationMembership.user_id == owner_id_col,
            OrganizationMembership.organization_id == org_id,
        )
    return ~exists().where(OrganizationMembership.user_id == owner_id_col)


def owner_shares_tenant_with(owner_id_col: Any, user_id: int) -> ColumnElement[bool]:
    """SQL predicate: the row's owner and ``user_id`` share a tenant.

    For worker paths that run without a request context: they have a common
    organization, or neither belongs to any organization (personal scope).
    """
    owner_m = aliased(OrganizationMembership)
    user_m = aliased(OrganizationMembership)
    common_org = exists().where(
        owner_m.user_id == owner_id_col,
        user_m.user_id == user_id,
        owner_m.organization_id == user_m.organization_id,
    )
    both_personal = and_(
        ~exists().where(OrganizationMembership.user_id == owner_id_col),
        ~exists().where(OrganizationMembership.user_id == user_id),
    )
    return or_(common_org, both_personal)


def _stamp_matches(org_col: Any, org_id: int | None) -> ColumnElement[bool]:
    """A row's own ``organization_id`` stamp (if any) must name the active tenant."""
    unstamped: ColumnElement[bool] = org_col.is_(None)
    if org_id is None:
        return unstamped
    return or_(unstamped, org_col == org_id)


def shared_visible_in_tenant(
    owner_id_col: Any,
    shared_col: Any,
    user_id: int,
    org_id: int | None,
    *,
    org_col: Any = None,
) -> ColumnElement[bool]:
    """SQL predicate for request paths: the caller owns the row, or it is shared in-tenant.

    ``org_col`` is the row's ``organization_id`` stamp where the model has one; a row
    stamped for another organization is never visible, even if its owner belongs to
    both.
    """
    in_tenant = and_(shared_col.is_(True), owner_in_tenant(owner_id_col, org_id))
    if org_col is not None:
        in_tenant = and_(in_tenant, _stamp_matches(org_col, org_id))
    return or_(owner_id_col == user_id, in_tenant)


def shared_usable_by(
    owner_id_col: Any,
    shared_col: Any,
    user_id: int,
    *,
    org_col: Any = None,
) -> ColumnElement[bool]:
    """SQL predicate for worker paths: ``user_id`` owns the row, or it is shared in a common tenant.

    Resolves stored pointers (an "active config" id, a "use shared" user id) at task
    time. The pointer outlives membership changes and may predate this check, so it is
    re-validated on every use rather than trusted.
    """
    in_tenant = and_(shared_col.is_(True), owner_shares_tenant_with(owner_id_col, user_id))
    if org_col is not None:
        in_tenant = and_(
            in_tenant,
            or_(
                org_col.is_(None),
                exists().where(
                    OrganizationMembership.user_id == user_id,
                    OrganizationMembership.organization_id == org_col,
                ),
            ),
        )
    return or_(owner_id_col == user_id, in_tenant)


def user_in_tenant(db: Any, user_id: int, org_id: int | None) -> bool:
    """Is ``user_id`` in tenant ``org_id``? (a member of it; or, for None, of no org at all)

    Row-level twin of :func:`owner_in_tenant` for share *targets*: a grant may only name a
    user the grantor's tenant can already see (``GET /users/search`` lists exactly these).
    """
    result: bool = bool(db.query(owner_in_tenant(user_id, org_id)).scalar())
    return result


def group_members_outside_org(db: Any, group_id: int, org_id: int) -> int:
    """How many members of group ``group_id`` are NOT members of organization ``org_id``.

    Groups can span organizations, so granting an org-stamped resource to a group is
    only in-tenant when this is zero.
    """
    from app.models.group import UserGroupMember

    count: int = (
        db.query(UserGroupMember.user_id)
        .filter(
            UserGroupMember.group_id == group_id,
            ~exists().where(
                OrganizationMembership.user_id == UserGroupMember.user_id,
                OrganizationMembership.organization_id == org_id,
            ),
        )
        .count()
    )
    return count
