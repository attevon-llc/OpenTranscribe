"""Just-in-time provisioning of OIDC identities into the ``user`` table."""

import logging
from datetime import datetime

from app.auth.constants import AUTH_TYPE_OIDC
from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.oidc.claims import OIDCUserData
from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_USER
from app.auth.roles import role_implies_superuser

logger = logging.getLogger(__name__)

#: Detail returned when an email-matched link is refused. Byte-identical to the 401
#: the OIDC callback returns for an unusable token, so a refusal cannot be used to
#: probe which addresses already exist (see ``auth/account_linking.py``).
LINK_REFUSED_DETAIL = "Invalid access token"  # noqa: S105 # nosec B105


def _parse_cert_timestamp(value: str | None, field: str) -> datetime | None:
    """Parse an ISO-8601 certificate timestamp, or None when absent/malformed."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.warning(f"Invalid {field} format: {value}")
        return None


def _apply_cert_metadata(user, oidc_data: OIDCUserData) -> None:
    """Copy any certificate claims the IdP brokered onto the user row."""
    for claim, attribute in (
        ("cert_dn", "pki_subject_dn"),
        ("cert_serial", "pki_serial_number"),
        ("cert_issuer", "pki_issuer_dn"),
        ("cert_org", "pki_organization"),
        ("cert_ou", "pki_organizational_unit"),
    ):
        value = oidc_data.get(claim)
        if value:
            setattr(user, attribute, value)

    not_before = _parse_cert_timestamp(oidc_data.get("cert_valid_from"), "cert_valid_from")
    if not_before:
        user.pki_not_before = not_before
    not_after = _parse_cert_timestamp(oidc_data.get("cert_valid_until"), "cert_valid_until")
    if not_after:
        user.pki_not_after = not_after

    fingerprint = oidc_data.get("cert_fingerprint")
    if fingerprint:
        user.pki_fingerprint_sha256 = fingerprint.replace(":", "")


def _create_oidc_user(db, oidc_data: OIDCUserData, *, is_admin: bool):
    """Create a new user from OIDC claims.

    Args:
        db: Database session.
        oidc_data: Claims from the verified ID token.
        is_admin: The *effective* admin signal — the legacy ``oidc_admin_role`` (or
            PKI admin DN) rule OR-ed with any ``group_mapping`` that grants ``admin``.
            Computed by :func:`sync_oidc_user_to_db`.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models.user import User

    subject = oidc_data["oidc_subject"]
    email = oidc_data["email"] or f"{oidc_data['username']}@oidc.local"

    logger.info(f"Creating new user from OIDC: {subject} ({email})")

    fingerprint = oidc_data.get("cert_fingerprint")
    # External IdPs grant at most 'admin'; super_admin is local-only.
    role = ROLE_ADMIN if is_admin else ROLE_USER
    user = User(
        email=email,
        full_name=oidc_data["full_name"] or oidc_data["username"] or email.split("@")[0],
        hashed_password=EXTERNAL_AUTH_NO_PASSWORD,
        auth_type=AUTH_TYPE_OIDC,
        oidc_subject=subject,
        pki_subject_dn=oidc_data.get("cert_dn"),
        pki_serial_number=oidc_data.get("cert_serial"),
        pki_issuer_dn=oidc_data.get("cert_issuer"),
        pki_organization=oidc_data.get("cert_org"),
        pki_organizational_unit=oidc_data.get("cert_ou"),
        pki_not_before=_parse_cert_timestamp(oidc_data.get("cert_valid_from"), "cert_valid_from"),
        pki_not_after=_parse_cert_timestamp(oidc_data.get("cert_valid_until"), "cert_valid_until"),
        pki_fingerprint_sha256=fingerprint.replace(":", "") if fingerprint else None,
        role=role,
        is_active=True,
        is_superuser=role_implies_superuser(role),
    )
    db.add(user)

    try:
        db.commit()
        return user
    except IntegrityError:
        db.rollback()
        logger.info(f"User {subject} was created by concurrent request, fetching existing user")
        user = db.query(User).filter(User.oidc_subject == subject).first()
        if not user:
            user = db.query(User).filter(User.email == email).first()
        if not user:
            raise ValueError(f"Failed to create or find OIDC user: {subject}") from None
        return user


def _update_oidc_user(db, user, oidc_data: OIDCUserData):
    """Update an existing user's OIDC identity and certificate metadata.

    Privilege is deliberately NOT decided here — see the same note on
    ``ldap_auth._update_ldap_user``. It is applied by
    ``services/idp_group_mapping_service.reconcile_user``, which
    :func:`sync_oidc_user_to_db` calls for every login.
    """
    subject = oidc_data["oidc_subject"]
    email = oidc_data["email"]

    logger.info(f"Updating existing user from OIDC: {subject} ({email})")

    if email and email != user.email:
        logger.warning(
            f"SECURITY: User email changed during OIDC login. "
            f"oidc_subject={subject}, old_email={user.email}, new_email={email}"
        )
        user.email = email
    if oidc_data["full_name"]:
        user.full_name = oidc_data["full_name"]
    user.oidc_subject = subject
    user.auth_type = AUTH_TYPE_OIDC

    _apply_cert_metadata(user, oidc_data)

    db.commit()
    return user


def _convert_local_user_to_oidc(db, user, oidc_data: OIDCUserData):
    """Convert an existing local user to OIDC authentication."""
    subject = oidc_data["oidc_subject"]
    email = oidc_data["email"]

    logger.info(f"Converting local user {user.email} to OIDC auth: {subject}")

    user.auth_type = AUTH_TYPE_OIDC
    user.oidc_subject = subject
    user.hashed_password = EXTERNAL_AUTH_NO_PASSWORD

    if email and email != user.email:
        logger.warning(
            f"SECURITY: User email changed during OIDC conversion. "
            f"oidc_subject={subject}, old_email={user.email}, new_email={email}"
        )
        user.email = email
    if oidc_data["full_name"]:
        user.full_name = oidc_data["full_name"]

    # Privilege is applied by reconcile_user after this returns.
    db.commit()
    return user


def sync_oidc_user_to_db(db, oidc_data: OIDCUserData):
    """Create or update a user in the database from verified OIDC claims.

    Handles creating new users, updating existing OIDC users, converting local users
    to OIDC, and race conditions — and then reconciles group membership and privilege
    against the configured ``group_mapping`` rows (``v376``).
    ``oidc_data["roles"]`` is the full list read from the configurable roles claim
    (``realm_access.roles`` by default, or the provider's ``groups`` claim); until
    v376 only ``is_admin`` survived it.

    Lookup is ``oidc_subject`` first, then email. The email fallback links this
    identity to a **pre-existing** account, so it goes through
    ``account_linking.assert_email_link_permitted`` — the same single rule LDAP and
    PKI use. See that module for why a refusal fails the login rather than creating a
    second account, and for the operator remedy.

    With no mappings configured this behaves exactly as before: no membership
    changes, and ``oidc_admin_role`` alone decides ``admin``.

    Raises:
        HTTPException: 401, when an email-matched link is refused.
    """
    from app.auth.account_linking import assert_email_link_permitted
    from app.auth.constants import AUTH_TYPE_LOCAL
    from app.models.group import MAPPING_SOURCE_OIDC
    from app.models.user import User
    from app.services.idp_group_mapping_service import reconcile_user
    from app.services.idp_group_mapping_service import resolve_grants

    subject = oidc_data["oidc_subject"]
    email = oidc_data["email"]
    roles = oidc_data.get("roles") or []

    user = db.query(User).filter(User.oidc_subject == subject).first()
    if not user and email:
        user = db.query(User).filter(User.email == email).first()
        if user:
            assert_email_link_permitted(
                user,
                provider=AUTH_TYPE_OIDC,
                source_identifier=subject,
                email_verified=bool(oidc_data.get("email_verified")),
                failure_detail=LINK_REFUSED_DETAIL,
            )

    # Resolved before the row is written so a brand-new account is created at the
    # right role instead of being created and then immediately promoted.
    grants = resolve_grants(db, MAPPING_SOURCE_OIDC, roles)
    is_admin = bool(oidc_data["is_admin"]) or grants.grants_admin

    if not user:
        user = _create_oidc_user(db, oidc_data, is_admin=is_admin)
    elif user.auth_type == AUTH_TYPE_LOCAL:
        logger.warning(
            f"SECURITY: Converting local user {email} to OIDC auth. "
            "User will now authenticate exclusively via the identity provider. "
            "Local password will be cleared."
        )
        user = _convert_local_user_to_oidc(db, user, oidc_data)
    else:
        user = _update_oidc_user(db, user, oidc_data)

    reconcile_user(
        db,
        user,
        MAPPING_SOURCE_OIDC,
        roles,
        legacy_admin=bool(oidc_data["is_admin"]),
        reason="idp_login",
    )

    db.refresh(user)
    return user
