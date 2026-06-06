"""Generic JIT user provisioning for registry-based external providers.

Mirrors the battle-tested ``sync_keycloak_user_to_db`` pattern (lookup by
external id -> email -> create, with IntegrityError race recovery) but is
provider-agnostic so new managed IdPs (Clerk today) need no core changes
beyond a column mapping entry.

LDAP/Keycloak/PKI keep their existing dedicated sync paths — this module only
serves providers registered through ``app.auth.provider_registry``.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.provider_registry import ExternalIdentity
from app.models.user import User

logger = logging.getLogger(__name__)

# provider -> (User column holding the external subject id,
#              optional User column holding the last-seen org id)
PROVIDER_ID_COLUMNS: dict[str, tuple[str, Optional[str]]] = {
    "clerk": ("clerk_id", "clerk_org_id"),
}


def _apply_identity(user: User, identity: ExternalIdentity, id_col: str, org_col: Optional[str]):
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
    if identity.is_admin and user.role != "admin":
        logger.info(f"Promoting external user {identity.external_id} to platform admin")
        user.role = "admin"
        user.is_superuser = True


def sync_external_user_to_db(db: Session, identity: ExternalIdentity) -> User:
    """Create or update a user from a verified external identity (JIT).

    Lookup order: external id -> email -> create. Concurrent first-requests
    are race-safe via IntegrityError recovery on the unique external-id
    column. Existing local users matched by email are converted one-way to
    the external provider (local password cleared), mirroring the Keycloak
    conversion semantics.
    """
    from sqlalchemy.exc import IntegrityError

    try:
        id_col, org_col = PROVIDER_ID_COLUMNS[identity.provider]
    except KeyError:
        raise ValueError(
            f"No User column mapping for external provider '{identity.provider}'"
        ) from None

    email = identity.email or f"{identity.external_id}@{identity.provider}.local"

    user = db.query(User).filter(getattr(User, id_col) == identity.external_id).first()
    if not user and identity.email:
        user = db.query(User).filter(User.email == identity.email).first()

    if user:
        if user.auth_type == "local":
            logger.warning(
                f"SECURITY: Converting local user {user.email} to {identity.provider} auth. "
                "Local password will be cleared."
            )
            user.hashed_password = EXTERNAL_AUTH_NO_PASSWORD
        if identity.email and identity.email != user.email:
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
    # funnel through (including the cloud Clerk webhook, which calls this same
    # core function). Bound the label to the fixed provider registry so the
    # cardinality stays in {local,ldap,keycloak,pki,clerk}; anything else maps
    # to "external".
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
        is_superuser=identity.is_admin,
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
