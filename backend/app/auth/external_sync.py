"""Generic JIT user provisioning for registry-based external providers.

Mirrors the battle-tested ``sync_keycloak_user_to_db`` pattern (lookup by
external id -> email -> create, with IntegrityError race recovery) but is
provider-agnostic: any managed IdP registered through
``app.auth.provider_registry`` maps onto the generic ``external_id`` /
``external_org_id`` columns with no core changes.

LDAP/Keycloak/PKI keep their existing dedicated sync paths — this module only
serves providers registered through ``app.auth.provider_registry``.
"""

import logging

from sqlalchemy.orm import Session

from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.provider_registry import ExternalIdentity
from app.auth.roles import ELEVATED_ROLES
from app.auth.roles import ROLE_ADMIN
from app.auth.roles import role_implies_superuser
from app.models.user import User

logger = logging.getLogger(__name__)

# Generic User columns every registry-based external provider maps onto:
# (column holding the external subject id, column holding the last-seen org id).
# Provider-agnostic — the same pair serves any managed IdP.
EXTERNAL_ID_COLUMN = "external_id"
EXTERNAL_ORG_ID_COLUMN: str | None = "external_org_id"


def _apply_identity(user: User, identity: ExternalIdentity, id_col: str, org_col: str | None):
    """Stamp provider identity + profile fields onto a user row."""
    setattr(user, id_col, identity.external_id)
    if org_col is not None and identity.org_id is not None:
        # Convenience/last-seen only — authorization always re-derives org
        # context from the verified token + membership mirror, never this.
        setattr(user, org_col, identity.org_id)
    if identity.full_name:
        user.full_name = identity.full_name
    user.auth_type = identity.provider
    # PLATFORM admin only — customer org roles (org:admin) intentionally do
    # NOT map here; org-admin is a tenant capability, not a platform role.
    # External IdPs grant at most 'admin' (never super_admin, which is local-only),
    # and we never demote an existing local super_admin/admin.
    if identity.is_admin and user.role not in ELEVATED_ROLES:
        logger.info(f"Promoting external user {identity.external_id} to platform admin")
        user.role = ROLE_ADMIN
    # is_superuser is the derived mirror of (role == super_admin).
    user.is_superuser = role_implies_superuser(user.role)


def sync_external_user_to_db(db: Session, identity: ExternalIdentity) -> User:
    """Create or update a user from a verified external identity (JIT).

    Lookup order: external id -> email -> create. Concurrent first-requests
    are race-safe via IntegrityError recovery on the unique external-id
    column. Existing local users matched by email are converted one-way to
    the external provider (local password cleared), mirroring the Keycloak
    conversion semantics — but ONLY when the IdP asserts the email is
    verified, and NEVER for super_admin accounts (platform-owner accounts
    must be linked deliberately, not via self-serve IdP registration).

    Raises:
        PermissionError: when an email-matched link is refused (unverified
            email or super_admin target). Callers treat this as 401.
    """
    from sqlalchemy.exc import IntegrityError

    id_col, org_col = EXTERNAL_ID_COLUMN, EXTERNAL_ORG_ID_COLUMN

    email = identity.email or f"{identity.external_id}@{identity.provider}.local"

    user = db.query(User).filter(getattr(User, id_col) == identity.external_id).first()
    matched_by_email = False
    if not user and identity.email:
        user = db.query(User).filter(User.email == identity.email).first()
        matched_by_email = user is not None

    if user and matched_by_email:
        # Email-match links an external identity to a PRE-EXISTING account —
        # the account-takeover vector if the IdP address isn't verified.
        if not identity.email_verified:
            logger.warning(
                f"SECURITY: Refusing to link {identity.provider} identity "
                f"{identity.external_id} to existing account {user.email}: "
                "IdP did not assert the email address as verified."
            )
            raise PermissionError("External identity email is not verified")
        if user.role == "super_admin":
            logger.warning(
                f"SECURITY: Refusing to link {identity.provider} identity "
                f"{identity.external_id} to super_admin account {user.email} "
                "via email match. Platform-owner accounts are never JIT-linked."
            )
            raise PermissionError("Account cannot be linked via external identity")

    if user:
        if user.auth_type == "local":
            logger.warning(
                f"SECURITY: Converting local user {user.email} to {identity.provider} auth. "
                "Local password will be cleared."
            )
            user.hashed_password = EXTERNAL_AUTH_NO_PASSWORD
        if identity.email and identity.email != user.email and identity.email_verified:
            logger.warning(
                f"SECURITY: User email changed during {identity.provider} login. "
                f"external_id={identity.external_id}, "
                f"old_email={user.email}, new_email={identity.email}"
            )
            user.email = identity.email
        _apply_identity(user, identity, id_col, org_col)
        db.commit()
        db.refresh(user)
        return user  # type: ignore[no-any-return]

    logger.info(f"Creating new user from {identity.provider}: {identity.external_id} ({email})")
    # Product metric: external/JIT signup. The single point ALL external methods
    # funnel through (including any cloud-edition webhook, which calls this same
    # core function). Bound the label to the fixed core provider set so the
    # cardinality stays in {local,ldap,keycloak,pki}; anything else maps to
    # "external".
    from app.auth.constants import VALID_AUTH_TYPES
    from app.core.metrics import user_signups_total

    method = identity.provider if identity.provider in VALID_AUTH_TYPES else "external"
    user_signups_total.labels(method=method).inc()

    user = User(
        email=email,
        full_name=identity.full_name or email.split("@")[0],
        hashed_password=EXTERNAL_AUTH_NO_PASSWORD,
        auth_type=identity.provider,
        role="admin" if identity.is_admin else "user",
        # is_superuser mirrors super_admin; external IdPs grant at most 'admin'.
        is_superuser=False,
        is_active=True,
    )
    setattr(user, id_col, identity.external_id)
    if org_col is not None and identity.org_id is not None:
        setattr(user, org_col, identity.org_id)
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
        return user
    except IntegrityError:
        # Concurrent first-request created the row — fetch and reuse it.
        db.rollback()
        logger.info(
            f"User {identity.external_id} was created by a concurrent request, fetching existing"
        )
        user = db.query(User).filter(getattr(User, id_col) == identity.external_id).first()
        if not user:
            user = db.query(User).filter(User.email == email).first()
        if not user:
            raise ValueError(
                f"Failed to create or find {identity.provider} user: {identity.external_id}"
            ) from None
        return user  # type: ignore[no-any-return]
