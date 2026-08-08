"""Side effects every credential- or privilege-changing endpoint owes.

Three rules were previously applied inconsistently across ``api/endpoints/users.py``,
``api/endpoints/admin.py`` and ``auth/password_reset.py``:

1. **Password policy.** ``validate_password`` was reachable only from ``UserCreate``
   (registration + admin create) and the self-service reset. ``UserUpdate`` has no
   validator, so ``PUT /users/me``, ``PUT /users/{uuid}`` and the admin reset all
   accepted a policy-violating password.
2. **Session revocation.** Only the self-service reset revoked sessions. Changing a
   password anywhere else left every other session alive — which is exactly the
   case revocation exists for: an attacker who already has a session keeps it
   through the victim's password change.
3. **Audit.** ``AuditLogger.log_password_change`` existed with **zero call sites**,
   and ``api/endpoints/users.py`` emitted no audit events at all, so role changes,
   deactivations and password changes made through it left no trace.

Endpoints call the helpers here instead of re-implementing any of it. Everything
runs inside the caller's transaction so a later failure rolls the whole unit back
(the ``_in_transaction`` variant of the revoker exists for exactly this reason —
see ``auth/token_service.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.password_policy import validate_password
from app.auth.token_service import token_service
from app.auth.utils import local_fallback_permitted_for
from app.auth.utils import local_password_allowed
from app.core.config import settings
from app.models.user import User

logger = logging.getLogger(__name__)


def enforce_password_policy(password: str, user: User) -> None:
    """Raise 400 unless *password* satisfies the configured policy.

    The policy rejects passwords containing the account's own email local-part or
    name parts, so the user is passed in rather than just the password.
    """
    result = validate_password(password, email=str(user.email), full_name=user.full_name)
    if not result.is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="; ".join(result.errors) or "Password does not meet policy requirements",
        )


def assert_password_auth_possible(user: User) -> None:
    """Raise 400 if this account cannot hold a local password at all.

    Guards the *write* side of the same rule ``auth.utils.local_password_allowed``
    guards at login. Without it an admin could plant a bcrypt hash on an LDAP row,
    which nothing else in the system expects to exist.
    """
    allowed, reason = local_password_allowed(
        str(user.auth_type), bool(getattr(user, "allow_local_fallback", False))
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot set a password for this account: {reason}",
        )


def assert_local_fallback_settable(auth_type: str | None) -> None:
    """Raise 400 if ``allow_local_fallback`` is meaningless for *auth_type*.

    The admin UI hides the toggle for local/LDAP accounts, but that is a client-side
    check only — the API accepted it, and on an LDAP account it was the first half
    of a local-password bypass.
    """
    if not local_fallback_permitted_for(auth_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "allow_local_fallback only applies to accounts whose identity is owned "
                "by PKI or an OIDC provider."
            ),
        )


def revoke_all_sessions(db: Session, user: User, *, reason: str) -> int:
    """Revoke every refresh token for *user*, inside the caller's transaction.

    Call this from anything that changes a credential or a privilege. Returns the
    number of sessions revoked; failures are logged and swallowed so a revocation
    problem never blocks the security change itself from being persisted.
    """
    try:
        revoked = token_service.revoke_all_user_tokens_in_transaction(
            db, user.id, user_uuid=str(user.uuid)
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Failed to revoke sessions for user %s (%s): %s", user.id, reason, exc)
        return 0

    if revoked:
        logger.info("Revoked %d session(s) for user %s (%s)", revoked, user.id, reason)
    return revoked


def reissue_current_session(
    db: Session,
    user: User,
    response: Response,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> None:
    """Give the caller a working session back after changing their OWN credential.

    :func:`revoke_all_sessions` is deliberately total: it kills the session the
    request arrived on as well, and stamps the per-user revocation epoch that
    invalidates stateless access tokens. That is correct for an admin acting on
    somebody else, but for a self-service change it signs the user out mid-flow —
    and when the change was a *forced* one, being bounced to the login screen is
    indistinguishable from the change having failed.

    Revoke everything, then mint exactly one new session: that is what "sign out
    everywhere else" means. Ordering is safe because the epoch check compares with
    ``<``, so a token minted in the same second as the epoch survives by design.

    Args:
        db: Session; ``create_refresh_token`` commits, so call this AFTER the
            caller's own commit.
        user: The account whose session is being re-established.
        response: The outgoing response; auth cookies are set on it.
        user_agent: Recorded on the new session row.
        ip_address: Recorded on the new session row.
    """
    from app.auth.cookies import set_auth_cookies
    from app.auth.direct_auth import create_access_token

    role = str(user.role)
    user_uuid = str(user.uuid)

    access_token = create_access_token(
        data={"sub": user_uuid, "role": role, "type": TOKEN_TYPE_ACCESS},
        expires_delta=timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token, _row = token_service.create_refresh_token(
        db=db,
        user_id=user.id,
        user_uuid=user_uuid,
        role=role,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    set_auth_cookies(response, access_token, refresh_token)
    logger.info("Re-issued the caller's session for user %s after a credential change", user.id)


def audit_password_change(
    user: User, actor: User, client_ip: str, user_agent: str, *, forced: bool = False
) -> None:
    """Record a password change. *actor* may differ from *user* on an admin reset."""
    audit_logger.log_password_change(
        user_id=user.id,
        username=str(user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        forced=forced,
    )
    if actor.id != user.id:
        audit_logger.log(
            event_type=AuditEventType.ADMIN_USER_UPDATE,
            outcome=AuditOutcome.SUCCESS,
            user_id=actor.id,
            username=str(actor.email),
            source_ip=client_ip,
            user_agent=user_agent,
            details={"action": "password_reset", "target_user": str(user.uuid)},
        )


def audit_role_change(
    user: User, actor: User, old_role: str, new_role: str, client_ip: str, user_agent: str
) -> None:
    """Record a role change from any endpoint, not just the admin role route."""
    audit_logger.log(
        event_type=AuditEventType.ADMIN_ROLE_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=actor.id,
        username=str(actor.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"target_user": str(user.uuid), "old_role": old_role, "new_role": new_role},
    )


def audit_account_status_change(
    user: User, actor: User, is_active: bool, client_ip: str, user_agent: str
) -> None:
    """Record an account being disabled or re-enabled."""
    audit_logger.log(
        event_type=AuditEventType.AUTH_ACCOUNT_DISABLED
        if not is_active
        else AuditEventType.AUTH_ACCOUNT_UNLOCK,
        outcome=AuditOutcome.SUCCESS,
        user_id=actor.id,
        username=str(actor.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"target_user": str(user.uuid), "is_active": is_active},
    )


def audit_expiration_change(
    user: User,
    actor: User,
    old_expires_at: str | None,
    new_expires_at: str | None,
    client_ip: str,
    user_agent: str,
) -> None:
    """Record a change to ``account_expires_at`` — the write half of AC-2's
    time-boxed-account control. ``dependencies.py`` already enforces the read
    half (refusing a request past this timestamp) and emits
    ``AUTH_ACCOUNT_EXPIRED`` when it fires; this is the administrative act that
    set the date being enforced, which needs its own trail regardless of
    whether the account ever actually reaches it.
    """
    audit_logger.log(
        event_type=AuditEventType.ADMIN_USER_UPDATE,
        outcome=AuditOutcome.SUCCESS,
        user_id=actor.id,
        username=str(actor.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={
            "action": "account_expiration_change",
            "target_user": str(user.uuid),
            "old_expires_at": old_expires_at,
            "new_expires_at": new_expires_at,
        },
    )


@dataclass(frozen=True)
class DeletedUser:
    """What the audit record needs, captured before the row is deleted.

    The event is emitted after the commit — deliberately, so a failed delete
    leaves no record of a deletion that did not happen — by which point the ORM
    object is expired and reading ``user.email`` would re-query a gone row.
    """

    uuid: str
    email: str

    @classmethod
    def of(cls, user: User) -> DeletedUser:
        return cls(uuid=str(user.uuid), email=str(user.email))


def audit_user_deleted(user: DeletedUser, actor: User, client_ip: str, user_agent: str) -> None:
    """Record a user deletion. ``ADMIN_USER_DELETE`` had no emitter before this."""
    audit_logger.log(
        event_type=AuditEventType.ADMIN_USER_DELETE,
        outcome=AuditOutcome.SUCCESS,
        user_id=actor.id,
        username=str(actor.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"target_user": user.uuid, "target_email": user.email},
    )


def notify_email_changed(old_email: str, new_email: str) -> None:
    """Tell the OLD address that the account's email was changed.

    Changing the address and then requesting a password reset is a complete
    account takeover, and it is silent unless the previous owner is told.
    Delivery failure must never block the change itself.
    """
    from app.services.email_service import email_service

    try:
        email_service.send_security_notice(
            old_email,
            "Your account email address was changed",
            f"The email address on your account was changed to {new_email}.",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Failed to send email-change notice to the previous address: %s", exc)
