"""Admin invitation logic — provisioning without the admin typing a password.

The replacement for "an admin sets a password and tells you over Slack". An
admin names an address plus the target ``role``/``auth_type``; the recipient
proves control of the address and chooses their own credential (or is handed to
the IdP, for an external ``auth_type``).

This exists because disabling self-registration was only half a feature:
``POST /api/admin/users`` could not set ``auth_type``, so every admin-created
account was ``local`` and could not authenticate at all on a deployment where
local passwords are off.

Token handling mirrors :mod:`app.auth.password_reset`: SHA-256 hash at rest,
single use, expiring, and one generic failure message for every rejection.
"""

import hashlib
import logging
import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import Session

from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.password_history import add_password_to_history
from app.auth.password_policy import validate_password
from app.auth.roles import role_implies_superuser
from app.auth.utils import local_password_allowed
from app.core.auth_mail import INVITATION_TASK
from app.core.auth_mail import dispatch_auth_mail
from app.core.auth_types import AUTH_TYPE_LOCAL
from app.core.config import settings
from app.core.exceptions import EmailTransientError
from app.core.security import get_password_hash
from app.models.invitation import UserInvitation
from app.models.user import User
from app.services.email_service import EmailDeliveryError
from app.services.email_service import email_service

logger = logging.getLogger(__name__)

#: Every rejection — unknown token, expired, revoked, already used, address
#: already registered — returns exactly this. Any variation tells the holder of
#: a guessed token which guesses were closer.
GENERIC_INVALID = "This invitation link is invalid, expired, or has already been used."


def _hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def invitation_status(invitation: UserInvitation) -> str:
    """Pre-computed display status (fat backend, thin frontend)."""
    if invitation.used_at is not None:
        return "accepted"
    if invitation.revoked_at is not None:
        return "revoked"
    return "pending" if invitation.is_redeemable() else "expired"


def create_invitation(
    db: Session,
    *,
    email: str,
    full_name: str | None,
    role: str,
    auth_type: str,
    expires_in_hours: int,
    created_by: User,
    ip_address: str,
) -> tuple[UserInvitation | None, str | None]:
    """Issue one invitation and queue the email carrying its link.

    Whether mail can be sent at all is decided here, from configuration, before the
    invitation is created: an administrator is told synchronously (503) that nothing can
    go out. The send itself runs in the ``auth_mail.invitation`` task, which mints the
    single-use token (the row is created with a throwaway hash nobody holds) so the token
    is never a task argument.

    Args:
        db: Database session.
        email: Address to invite.
        full_name: Optional display name pre-filled for the invitee.
        role: Target role; the *caller's* privilege is checked at the endpoint.
        auth_type: Target auth type; external types never get a local password.
        expires_in_hours: Lifetime of the link.
        created_by: The inviting admin.
        ip_address: Client IP, recorded for audit.

    Returns:
        ``(invitation, None)`` on success, ``(None, error)`` when the address is
        already registered. Enumeration is not a concern here — the caller is an
        authenticated admin who can list users anyway.
    """
    if db.query(User).filter(User.email == email).first():
        return None, "That email address already has an account."

    email_service.require_credential_mail(f"{settings.FRONTEND_URL}/accept-invite")

    # One outstanding invitation per address: re-inviting must invalidate the
    # previous link, or a revoked-and-resent invite leaves the old one live.
    now = datetime.now(UTC)
    db.query(UserInvitation).filter(
        UserInvitation.email == email,
        UserInvitation.used_at.is_(None),
        UserInvitation.revoked_at.is_(None),
    ).update({"revoked_at": now})

    # Placeholder hash: the real token is minted by the delivery task, so this row cannot be
    # redeemed until (and unless) the mail is handed to a transport.
    invitation = UserInvitation(
        email=email,
        full_name=full_name,
        role=role,
        auth_type=auth_type,
        token_hash=_hash(secrets.token_urlsafe(32)),
        expires_at=now + timedelta(hours=expires_in_hours),
        created_by_id=created_by.id,
        ip_address=ip_address,
    )
    db.add(invitation)
    db.commit()
    db.refresh(invitation)

    dispatch_auth_mail(INVITATION_TASK, int(invitation.id), expires_in_hours)
    logger.info("Invitation issued for %s (role=%s, auth_type=%s)", email, role, auth_type)
    return invitation, None


def deliver_invitation(
    db: Session, invitation_id: int, expires_in_hours: int, *, final_attempt: bool = True
) -> None:
    """Mint the single-use link for an invitation and mail it (Celery task body).

    A revoked, used or missing invitation is skipped. Each attempt mints a fresh token,
    which supersedes the previous one, so a retry never leaves two live links.

    Raises:
        EmailTransientError: The send failed transiently and attempts remain.
    """
    invitation = db.query(UserInvitation).filter(UserInvitation.id == invitation_id).first()
    if invitation is None or invitation.used_at is not None or invitation.revoked_at is not None:
        return
    inviter = db.query(User).filter(User.id == invitation.created_by_id).first()

    raw_token = secrets.token_urlsafe(32)
    invitation.token_hash = _hash(raw_token)
    db.commit()

    try:
        email_service.send_invitation(
            str(invitation.email),
            f"{settings.FRONTEND_URL}/accept-invite?token={raw_token}",
            inviter=str(inviter.email) if inviter is not None else "An administrator",
            expires_in_hours=expires_in_hours,
            requires_password=invitation.auth_type == AUTH_TYPE_LOCAL,
        )
    except EmailTransientError:
        if not final_attempt:
            raise
        logger.error("Invitation %s could not be delivered after retries", invitation.id)
    except EmailDeliveryError:
        logger.error("Invitation %s could not be delivered", invitation.id)


def revoke_invitation(db: Session, invitation: UserInvitation) -> None:
    """Mark an invitation unusable. Accepted invitations are left alone."""
    if invitation.used_at is None and invitation.revoked_at is None:
        invitation.revoked_at = datetime.now(UTC)
        db.commit()


def lookup_invitation(db: Session, raw_token: str) -> UserInvitation | None:
    """Resolve a raw token to a redeemable invitation, or None.

    Redeemability is evaluated in Python (:meth:`UserInvitation.is_redeemable`)
    rather than in the WHERE clause so callers cannot tell "no such token" from
    "expired" by timing the query differently.
    """
    if not raw_token:
        return None
    invitation = (
        db.query(UserInvitation).filter(UserInvitation.token_hash == _hash(raw_token)).first()
    )
    if invitation is None or not invitation.is_redeemable():
        return None
    return invitation


def accept_invitation(
    db: Session,
    raw_token: str,
    password: str | None,
    full_name: str | None,
) -> tuple[User | None, str | None]:
    """Redeem an invitation and create the account it describes.

    Args:
        db: Database session.
        raw_token: The token from the invitation link.
        password: Chosen password. Required for a ``local`` invitation, refused
            for any other ``auth_type``.
        full_name: Optional override of the name the admin pre-filled.

    Returns:
        ``(user, None)`` on success, ``(None, error)`` otherwise. The error is
        :data:`GENERIC_INVALID` for every token-state failure; a password-policy
        failure returns its own message, which is only reachable by someone who
        already holds a valid token.
    """
    invitation = lookup_invitation(db, raw_token)
    if invitation is None:
        return None, GENERIC_INVALID

    if db.query(User).filter(User.email == invitation.email).first():
        # The address was claimed after the invite was sent. Burn the link.
        invitation.revoked_at = datetime.now(UTC)
        db.commit()
        return None, GENERIC_INVALID

    auth_type = str(invitation.auth_type)
    wants_password, _reason = local_password_allowed(auth_type, False)

    if wants_password:
        if not password:
            return None, "A password is required to accept this invitation."
        result = validate_password(
            password=password,
            email=str(invitation.email),
            full_name=full_name or invitation.full_name,
        )
        if not result.is_valid:
            # Checked BEFORE the invitation is consumed: a weak first attempt
            # must not burn the link.
            return None, "; ".join(result.errors)
    elif password:
        return None, f"auth_type={auth_type!r} accounts do not hold a local password."

    now = datetime.now(UTC)
    # Both guards above already make `wants_password` and a non-empty `password`
    # equivalent; restating it here is what lets the type checker see it.
    password_hash = (
        get_password_hash(password) if wants_password and password else EXTERNAL_AUTH_NO_PASSWORD
    )
    role = str(invitation.role)
    user = User(
        email=invitation.email,
        full_name=full_name or invitation.full_name,
        hashed_password=password_hash,
        role=role,
        is_superuser=role_implies_superuser(role),
        auth_type=auth_type,
        allow_local_fallback=False,
        is_active=True,
        # Redeeming a link that was mailed to the address IS proof of control,
        # so an invited account never has to verify separately.
        email_verified=True,
        email_verified_at=now,
        password_changed_at=now if wants_password else None,
    )
    db.add(user)
    db.flush()

    if wants_password:
        # Admin-created accounts previously wrote no history row at all, so the
        # very first password could be re-set to itself forever (FedRAMP IA-5).
        add_password_to_history(db, int(user.id), password_hash)

    invitation.used_at = now
    invitation.created_user_id = user.id
    db.commit()
    db.refresh(user)

    logger.info("Invitation accepted for user %s (role=%s, auth_type=%s)", user.id, role, auth_type)
    return user, None
