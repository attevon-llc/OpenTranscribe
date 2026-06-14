"""Canonical role definitions and the single ``is_superuser`` derivation.

``User.role`` is the **sole source of truth** for authorization. ``is_superuser``
is a derived mirror of ``role == super_admin``, kept in sync at every write and
enforced by a DB CHECK constraint (see migration ``v369``). Never set
``is_superuser`` independently of ``role`` — always go through
:func:`role_implies_superuser`.

Authorization tiers:
- ``user``         — normal account.
- ``admin``        — org/team admin: user management, settings, data maintenance.
- ``super_admin``  — platform owner: auth config, role changes, system settings.
                     Local-only; external IdPs may grant at most ``admin``.
"""

from __future__ import annotations

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLE_SUPER_ADMIN = "super_admin"

#: All valid role values (order = ascending privilege).
VALID_ROLES = (ROLE_USER, ROLE_ADMIN, ROLE_SUPER_ADMIN)

#: Roles that count as "admin or above" (the get_current_admin_user tier).
ELEVATED_ROLES = (ROLE_ADMIN, ROLE_SUPER_ADMIN)


def role_implies_superuser(role: str | None) -> bool:
    """Return the canonical ``is_superuser`` value for a role.

    ``is_superuser`` is True if and only if the role is ``super_admin``.
    """
    return role == ROLE_SUPER_ADMIN
