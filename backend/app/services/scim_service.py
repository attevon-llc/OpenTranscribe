"""Every write the SCIM surface performs, and the rules it may not bypass.

The endpoints in ``api/endpoints/scim/`` are transport: they parse, they page, they
render. Everything that changes a row is here, because provisioning must be subject
to exactly the same rules as the admin UI:

* **Deactivation revokes sessions.** ``active: false`` goes through
  ``account_security_service.revoke_all_sessions`` — the same helper the admin user
  list uses. Disabling an account while its refresh token keeps rotating is the
  failure mode that helper exists to prevent.
* **``super_admin`` is untouchable.** No SCIM call may create one, promote to one, or
  deactivate one. The last of those mirrors ``directory_sync_service`` rule 2 and is
  the reason a compromised provisioning token cannot lock the deployment's owner out.
* **Roles are never written at all.** The SCIM core schema has no role attribute, and
  this module does not invent one; the only privilege path from an external system
  remains ``group_mapping`` + ``idp_group_mapping_service``, whose cap is enforced by
  ``assert_grantable_role`` and by ``ck_group_mapping_role_capped``.
* **Deletion is deactivation.** ``DELETE /Users/{id}`` disables. Real erasure is
  ``gdpr_erasure_service`` behind a deliberate administrator action, never a
  provisioning connector's idea of "remove from scope".

Every mutation is audited with the issuing token's name as the actor, the same way
``idp_group_mapping_service`` audits a directory as the actor rather than inventing a
``User`` to hang the event on.

Group writes live in the sibling :mod:`app.services.scim_group_service`, which imports
the shared helpers from here; the two split only because one file was outgrowing the
~300-line rule.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import ROLE_USER
from app.auth.roles import role_implies_superuser
from app.models.user import User
from app.services.account_security_service import revoke_all_sessions

logger = logging.getLogger(__name__)


class SCIMConflictError(ValueError):
    """A uniqueness constraint refused the write (409)."""


class SCIMForbiddenError(ValueError):
    """A rule refuses this write outright (403)."""


def _audit(
    event: AuditEventType,
    *,
    actor: str,
    target: str,
    target_user_id: int | None = None,
    **details,
) -> None:
    """Record a provisioning mutation with the token as the actor.

    `user_id` stays None: the actor is a SCIM token, not a person, and inventing
    a user id for it would attribute the change to whoever the connector happened
    to touch. The subject goes in `target_username` / `target_user_id` (issue
    #443) rather than only in `username`, which read as the actor.
    """
    audit_logger.log(
        event_type=event,
        outcome=AuditOutcome.SUCCESS,
        username=target,
        target_user_id=target_user_id,
        target_username=target,
        details={"actor": f"scim:{actor}", "source": "scim", **details},
    )


def _assert_not_super_admin(user: User, action: str) -> None:
    """Refuse any SCIM write that would touch a platform owner."""
    if str(user.role) == ROLE_SUPER_ADMIN:
        raise SCIMForbiddenError(
            f"Refusing to {action} a super_admin account through SCIM. "
            "The platform owner is managed locally by design."
        )


def create_user(
    db: Session,
    *,
    email: str,
    display_name: str | None,
    external_id: str | None,
    active: bool,
    actor: str,
) -> User:
    """Create an account from a SCIM ``POST /Users``.

    The account is created with ``auth_type='local'`` and **no usable password**: it
    is pre-provisioned, not sign-in-ready. The user's first sign-in through whichever
    external method the deployment runs converts it, subject to the one account
    linking rule in ``auth/account_linking.py``. Inventing an ``auth_type`` here
    would be guessing which IdP the SCIM client speaks for.

    ``approval_status`` is left at the column default (``approved``): an identity
    provider provisioning an account into this deployment *is* the admission
    decision, exactly as an administrator creating one in the admin UI is. The
    approval queue exists for self-service and JIT paths where nobody decided.

    Raises:
        SCIMConflictError: The address already belongs to an account.
    """
    user = User(
        email=email,
        full_name=display_name or email.split("@")[0],
        hashed_password=EXTERNAL_AUTH_NO_PASSWORD,
        auth_type=AUTH_TYPE_LOCAL,
        external_id=external_id,
        role=ROLE_USER,
        is_active=active,
        is_superuser=role_implies_superuser(ROLE_USER),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise SCIMConflictError(f"A user with userName {email!r} already exists") from exc
    db.refresh(user)
    _audit(
        AuditEventType.ADMIN_USER_CREATE,
        actor=actor,
        target=email,
        target_user_id=int(user.id),
        user_uuid=str(user.uuid),
    )
    logger.info("SCIM created user %s", email)
    return user


def update_user(
    db: Session,
    user: User,
    *,
    email: str | None = None,
    display_name: str | None = None,
    external_id: str | None = None,
    active: bool | None = None,
    actor: str,
    full_replace: bool = False,
) -> User:
    """Apply a SCIM update to *user*, revoking sessions if it deactivates.

    Two distinct wire semantics share this function, selected by *full_replace*:

    * ``full_replace=False`` (the default, used by ``PATCH``) is a **merge**: only
      attributes the caller actually supplied are written, and ``None`` means "not
      mentioned" for every field. This is the historical behavior and is what
      ``PATCH`` — which is inherently an incremental operation — must keep doing.
    * ``full_replace=True`` (used by ``PUT``) is RFC 7644 §3.5.1's genuine resource
      replacement: every attribute is set to exactly what the caller supplied, and
      an attribute the request omitted is **cleared to its default**, not left as
      whatever the resource already held. Concretely: ``external_id`` omitted means
      ``None`` (cleared), ``display_name`` omitted falls back to the email's local
      part (the same default ``create_user`` applies), and ``active`` omitted means
      ``True`` ("absent means active", the same default RFC 7643 §4.1.1 and
      ``create_user``'s docstring already apply on create). ``email`` has no
      "cleared" value — it is the account's SCIM identifier and a NOT NULL, unique
      column — so the caller (``replace_user``) must resolve and pass a real
      address before calling this; there is nothing here to fall back to.

    Raises:
        SCIMForbiddenError: The target is a ``super_admin`` and the write would disable
            it, or would change the address it authenticates with.
        SCIMConflictError: The new address already belongs to another account.
    """
    changed: dict[str, object] = {}

    if email and email != str(user.email):
        _assert_not_super_admin(user, "change the userName of")
        changed["email"] = email
        user.email = email  # type: ignore[assignment]

    if full_replace:
        if display_name != str(user.full_name or ""):
            changed["full_name"] = display_name
            user.full_name = display_name  # type: ignore[assignment]
        if external_id != user.external_id:
            changed["external_id"] = external_id
            user.external_id = external_id  # type: ignore[assignment]
        active_value: bool | None = True if active is None else bool(active)
    else:
        if display_name is not None and display_name != str(user.full_name or ""):
            changed["full_name"] = display_name
            user.full_name = display_name  # type: ignore[assignment]
        if external_id is not None and external_id != user.external_id:
            changed["external_id"] = external_id
            user.external_id = external_id  # type: ignore[assignment]
        active_value = active  # None means "not mentioned" under a merge

    deactivating = active_value is not None and bool(active_value) != bool(user.is_active)
    if deactivating:
        if not active_value:
            _assert_not_super_admin(user, "deactivate")
        changed["is_active"] = bool(active_value)
        user.is_active = bool(active_value)  # type: ignore[assignment]

    if not changed:
        return user

    revoked = 0
    if changed.get("is_active") is False:
        # The whole point of `active: false`. Open WebUI's SCIM sets a role to
        # 'pending' instead and leaves the session alive; we have a real flag and a
        # real revocation path, so both are used.
        revoked = revoke_all_sessions(db, user, reason="scim_deactivate")

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise SCIMConflictError("That userName already belongs to another user") from exc
    db.refresh(user)

    event = (
        AuditEventType.AUTH_ACCOUNT_DISABLED
        if changed.get("is_active") is False
        else AuditEventType.ADMIN_USER_UPDATE
    )
    _audit(
        event,
        actor=actor,
        target=str(user.email),
        target_user_id=int(user.id),
        user_uuid=str(user.uuid),
        changed=sorted(changed),
        sessions_revoked=revoked,
    )
    logger.info("SCIM updated user %s (%s)", user.email, ", ".join(sorted(changed)))
    return user
