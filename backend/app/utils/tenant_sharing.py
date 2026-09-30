"""Tenant gates for per-user items shared "with everyone" (media sources, org context).

These items carry no ``organization_id`` of their own: a user flips ``is_shared`` and the
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
