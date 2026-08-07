"""Turn directory groups into in-app groups and privileges — one implementation.

Both directory paths already carry the caller's full group list (``LdapUserData.groups``
from ``memberOf``, ``OIDCUserData.roles`` from the configurable roles claim) and
until now discarded everything but ``is_admin``. This module is what consumes the rest:
a :class:`~app.models.group.GroupMapping` row binds one claim value to a
``UserGroup`` and/or a granted role, and :func:`reconcile_user` applies the result.

Two callers share this one implementation — login (``auth/ldap_auth.py``,
``auth/oidc/provisioning.py``) and the periodic sweep
(``services/directory_sync_service.py``). There is deliberately no second copy: a
login-only version would never revoke, and a sweep-only version would leave a
freshly-promoted user waiting a day for their groups.

Invariants this module owns:

* **``super_admin`` is unreachable from any IdP.** :func:`assert_grantable_role`
  refuses it before anything is persisted, and ``ck_group_mapping_role_capped``
  (``v376``) refuses it at the database. A super_admin account is also never
  demoted here — it is the break-glass account for the directory that might be
  the thing that is broken.
* **Hand-added memberships are untouchable.** Only rows whose ``source`` is a
  directory are ever removed; a ``manual`` row survives every pass, and a mapping
  that would duplicate one leaves it manual rather than claiming it.
* **A privilege change revokes sessions**, through
  ``services/account_security_service.revoke_all_sessions``, and is audited. The
  actor is the directory rather than a ``User``, which is why the audit event is
  emitted here instead of through ``audit_role_change`` (that helper requires an
  acting account and would have to invent one).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.roles import ELEVATED_ROLES
from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import ROLE_USER
from app.auth.roles import role_implies_superuser
from app.models.group import MAPPING_SOURCE_LDAP
from app.models.group import MAPPING_SOURCES
from app.models.group import MEMBERSHIP_SOURCE_MANUAL
from app.models.group import GroupMapping
from app.models.group import UserGroupMember
from app.services.account_security_service import revoke_all_sessions

if TYPE_CHECKING:
    from app.models.user import User

logger = logging.getLogger(__name__)

#: The roles a mapping may grant, weakest first. ``super_admin`` is absent by
#: design and adding it here would be a privilege-escalation bug, not a feature.
GRANTABLE_ROLES = (ROLE_USER, ROLE_ADMIN)


class RoleNotGrantableError(ValueError):
    """A mapping tried to grant a role no directory is allowed to grant."""


def assert_grantable_role(role: str | None) -> str | None:
    """Return *role* unchanged, or raise if an IdP may not grant it.

    Args:
        role: ``user``, ``admin`` or ``None`` (group membership only).

    Returns:
        The same value, normalized to ``None`` for an empty string.

    Raises:
        RoleNotGrantableError: For ``super_admin`` or any unknown value.
    """
    if not role:
        return None
    if role not in GRANTABLE_ROLES:
        raise RoleNotGrantableError(
            f"An identity provider may grant at most '{ROLE_ADMIN}'; "
            f"'{role}' is not grantable (super_admin is local-only by design)"
        )
    return role


def normalize_claim_value(source: str, value: str) -> str:
    """Fold a claim value into its comparison form.

    LDAP distinguished names are case-insensitive — the existing membership check
    (``ldap_auth._is_member_of_groups``) already lowercases both sides, and
    ``uq_group_mapping_ldap_claim_ci`` keeps the table honest about it. OIDC role
    and group strings are opaque, case-sensitive identifiers, so they are compared
    verbatim; folding them would silently merge ``Legal`` and ``legal``.
    """
    stripped = value.strip()
    return stripped.lower() if source == MAPPING_SOURCE_LDAP else stripped


# =============================================================================
# Resolution
# =============================================================================
@dataclass(frozen=True)
class DirectoryGrants:
    """What one user's claim list resolves to, before anything is written."""

    source: str
    matched_claims: tuple[str, ...] = ()
    group_ids: frozenset[int] = frozenset()
    role: str | None = None

    @property
    def grants_admin(self) -> bool:
        """True when at least one matched mapping grants ``admin``."""
        return self.role == ROLE_ADMIN


def resolve_grants(db: Session, source: str, claim_values: object) -> DirectoryGrants:
    """Resolve a directory claim list against the configured mappings.

    Args:
        db: Session used for the (single) mapping query.
        source: ``ldap`` or ``oidc``.
        claim_values: The group DNs / role names the IdP asserted. Anything
            non-iterable is treated as an empty list so a malformed claim can
            never raise inside a login.

    Returns:
        The union of every matched mapping's group ids, and the strongest role any
        of them grants (already capped at ``admin``).
    """
    if source not in MAPPING_SOURCES:
        raise ValueError(f"Unknown directory source: {source!r}")
    claims = [str(v) for v in claim_values] if isinstance(claim_values, (list, tuple, set)) else []
    if not claims:
        return DirectoryGrants(source=source)

    index: dict[str, list[GroupMapping]] = {}
    for mapping in db.query(GroupMapping).filter(GroupMapping.source == source).all():
        index.setdefault(normalize_claim_value(source, str(mapping.claim_value)), []).append(
            mapping
        )
    if not index:
        return DirectoryGrants(source=source)

    matched: list[GroupMapping] = []
    seen_claims: list[str] = []
    for claim in claims:
        hits = index.get(normalize_claim_value(source, claim))
        if hits:
            matched.extend(hits)
            seen_claims.append(claim)

    group_ids = {int(m.user_group_id) for m in matched if m.user_group_id is not None}
    role: str | None = None
    for mapping in matched:
        granted = str(mapping.grants_role) if mapping.grants_role else None
        if granted == ROLE_ADMIN:
            role = ROLE_ADMIN
            break
        if granted == ROLE_USER:
            role = ROLE_USER
    return DirectoryGrants(
        source=source,
        matched_claims=tuple(seen_claims),
        group_ids=frozenset(group_ids),
        role=role,
    )


# =============================================================================
# Application
# =============================================================================
@dataclass
class ReconciliationResult:
    """What one reconciliation pass did (or would do, under ``dry_run``)."""

    source: str
    matched_claims: tuple[str, ...] = ()
    groups_added: list[int] = field(default_factory=list)
    groups_removed: list[int] = field(default_factory=list)
    role_before: str | None = None
    role_after: str | None = None
    sessions_revoked: int = 0
    applied: bool = True

    @property
    def changed(self) -> bool:
        """True when the pass found anything to do."""
        return bool(self.groups_added or self.groups_removed or self.role_changed)

    @property
    def role_changed(self) -> bool:
        return self.role_after is not None and self.role_after != self.role_before

    def as_dict(self) -> dict:
        """JSON-serializable form for the sweep report and the admin UI."""
        return {
            "source": self.source,
            "matched_claims": list(self.matched_claims),
            "groups_added": self.groups_added,
            "groups_removed": self.groups_removed,
            "role_before": self.role_before,
            "role_after": self.role_after if self.role_changed else None,
            "sessions_revoked": self.sessions_revoked,
            "applied": self.applied,
        }


def _reconcile_memberships(
    db: Session, user: User, source: str, group_ids: frozenset[int], *, dry_run: bool
) -> tuple[list[int], list[int]]:
    """Add missing directory memberships and drop the ones the directory dropped.

    A membership is **added** when a matched mapping names a group the user is not
    already in — as ``source=<directory>``, so the next pass owns it. It is
    **removed** when a directory-sourced row's group is no longer in the resolved
    set, including when the mapping itself was deleted. A ``manual`` row is never
    added over, never removed, and never converted: if an admin already put the
    user in that group by hand, that decision outlives the directory.

    Returns:
        ``(added_group_ids, removed_group_ids)``.
    """
    existing = db.query(UserGroupMember).filter(UserGroupMember.user_id == user.id).all()
    manual_group_ids = {
        int(m.group_id) for m in existing if str(m.source) == MEMBERSHIP_SOURCE_MANUAL
    }
    derived = {int(m.group_id): m for m in existing if str(m.source) != MEMBERSHIP_SOURCE_MANUAL}

    to_add = sorted(group_ids - derived.keys() - manual_group_ids)
    to_remove = sorted(gid for gid in derived if gid not in group_ids)
    if dry_run or not (to_add or to_remove):
        return to_add, to_remove

    for group_id in to_add:
        db.add(UserGroupMember(group_id=group_id, user_id=user.id, role="member", source=source))
    for group_id in to_remove:
        db.delete(derived[group_id])
    db.commit()
    logger.info(
        "Directory reconciliation for %s (%s): +%d group(s), -%d group(s)",
        user.email,
        source,
        len(to_add),
        len(to_remove),
    )
    return to_add, to_remove


def _apply_role(
    db: Session, user: User, *, grants_admin: bool, source: str, reason: str, dry_run: bool
) -> tuple[str, str | None, int]:
    """Promote to / demote from ``admin`` from a directory signal.

    ``super_admin`` is returned untouched in both directions: no IdP may mint one,
    and none may take one away either — that account is the way back in when the
    directory is the thing that is broken.

    Returns:
        ``(role_before, role_after_or_None, sessions_revoked)``.
    """
    current = str(user.role)
    if current == ROLE_SUPER_ADMIN:
        return current, None, 0

    if grants_admin:
        desired = current if current in ELEVATED_ROLES else ROLE_ADMIN
    else:
        desired = ROLE_USER if current == ROLE_ADMIN else current
    if desired == current:
        return current, None, 0
    if dry_run:
        return current, desired, 0

    user.role = desired  # type: ignore[assignment]
    # Never written independently — v369's ck_user_superuser_matches_role rejects it.
    user.is_superuser = role_implies_superuser(desired)  # type: ignore[assignment]
    revoked = revoke_all_sessions(db, user, reason=f"{reason}:role_change")
    db.commit()

    audit_logger.log(
        event_type=AuditEventType.ADMIN_ROLE_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=user.id,
        username=str(user.email),
        details={
            "actor": reason,
            "source": source,
            "old_role": current,
            "new_role": desired,
            "sessions_revoked": revoked,
        },
    )
    logger.info(
        "Directory reconciliation changed %s from %s to %s (%s); %d session(s) revoked",
        user.email,
        current,
        desired,
        reason,
        revoked,
    )
    return current, desired, revoked


def reconcile_user(
    db: Session,
    user: User,
    source: str,
    claim_values: object,
    *,
    legacy_admin: bool = False,
    reason: str = "idp_login",
    dry_run: bool = False,
) -> ReconciliationResult:
    """Bring one account's groups and privilege in line with the directory.

    Args:
        db: Session owning the transaction.
        user: The account to reconcile (already created/updated by the caller).
        source: ``ldap`` or ``oidc``.
        claim_values: The group/role strings the IdP asserted for this login.
        legacy_admin: The pre-existing admin signal — ``ldap_admin_users`` /
            ``ldap_admin_groups`` for LDAP, ``oidc_admin_role`` (or a PKI admin
            DN) for OIDC. OR-ed with the mapped grant, so a deployment that has not
            created any mapping behaves exactly as it did before ``v376``.
        reason: Actor string recorded in the audit event (``idp_login`` /
            ``directory_sync``).
        dry_run: Compute the plan and change nothing.

    Returns:
        A :class:`ReconciliationResult` describing what was (or would be) done.
    """
    grants = resolve_grants(db, source, claim_values)
    added, removed = _reconcile_memberships(db, user, source, grants.group_ids, dry_run=dry_run)
    before, after, revoked = _apply_role(
        db,
        user,
        grants_admin=legacy_admin or grants.grants_admin,
        source=source,
        reason=reason,
        dry_run=dry_run,
    )
    return ReconciliationResult(
        source=source,
        matched_claims=grants.matched_claims,
        groups_added=added,
        groups_removed=removed,
        role_before=before,
        role_after=after,
        sessions_revoked=revoked,
        applied=not dry_run,
    )
