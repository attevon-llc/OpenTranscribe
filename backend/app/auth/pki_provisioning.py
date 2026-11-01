"""Just-in-time provisioning of PKI users: create, update, or convert a local account.

``sync_pki_user_to_db`` looks a certificate identity up by ``pki_subject_dn``, then by
email (gated by ``auth/account_linking.py``), and creates, updates, or converts the
account, deriving the role from ``PKIUserData["is_admin"]``.
"""

import logging

from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.pki_auth import PKI_ASSERTS_EMAIL_VERIFIED
from app.auth.pki_certificate import PKIUserData
from app.auth.roles import ELEVATED_ROLES
from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_USER
from app.auth.roles import role_implies_superuser
from app.core.auth_types import AUTH_TYPE_PKI

logger = logging.getLogger(__name__)


def _create_pki_user(db, pki_data: PKIUserData):
    """
    Create a new user from PKI certificate data.

    Args:
        db: Database session
        pki_data: PKI user data

    Returns:
        Created User object

    Raises:
        ValueError: If user cannot be created or found after race condition
    """
    from sqlalchemy.exc import IntegrityError

    from app.auth.approval import initial_approval_status
    from app.models.user import User

    subject_dn = pki_data["subject_dn"]
    email = pki_data["email"]

    # If no email in cert, use CN@pki.local as placeholder
    if not email:
        # Replace spaces with dots for email format
        cn_email_safe = pki_data["common_name"].replace(" ", ".").lower()
        email = f"{cn_email_safe}@pki.local"

    logger.info(f"Creating new user from PKI: {subject_dn}")

    # Parse validity dates from ISO strings if available
    pki_not_before = None
    pki_not_after = None
    not_before_str = pki_data.get("not_before")
    if not_before_str:
        from datetime import datetime

        pki_not_before = datetime.fromisoformat(not_before_str)
    not_after_str = pki_data.get("not_after")
    if not_after_str:
        from datetime import datetime

        pki_not_after = datetime.fromisoformat(not_after_str)

    fingerprint = pki_data.get("fingerprint")
    # External IdPs grant at most 'admin'; super_admin is local-only.
    role = ROLE_ADMIN if pki_data["is_admin"] else ROLE_USER
    user = User(
        email=email,
        full_name=pki_data["common_name"] or email.split("@")[0],
        hashed_password=EXTERNAL_AUTH_NO_PASSWORD,
        auth_type=AUTH_TYPE_PKI,
        pki_subject_dn=subject_dn,
        # Certificate metadata fields
        pki_serial_number=pki_data.get("serial_number"),
        pki_issuer_dn=pki_data.get("issuer_dn"),
        pki_organization=pki_data.get("organization"),
        pki_organizational_unit=pki_data.get("organizational_unit"),
        pki_common_name=pki_data["common_name"],
        pki_not_before=pki_not_before,
        pki_not_after=pki_not_after,
        pki_fingerprint_sha256=fingerprint.replace(":", "") if fingerprint else None,
        role=role,
        is_active=True,
        is_superuser=role_implies_superuser(role),
        # Same rule as the LDAP and OIDC JIT paths: holding a certificate the CA
        # issued is not the same as this deployment wanting an account here.
        approval_status=initial_approval_status(db),
    )
    db.add(user)

    try:
        db.commit()
        return user
    except IntegrityError:
        # Race condition: user was created by concurrent request
        db.rollback()
        logger.info(
            f"User with DN {subject_dn} was created by concurrent request, fetching existing user"
        )
        user = db.query(User).filter(User.pki_subject_dn == subject_dn).first()
        if not user:
            user = db.query(User).filter(User.email == email).first()
        if not user:
            raise ValueError(f"Failed to create or find PKI user: {subject_dn}") from None
        return user


def _update_pki_user(db, user, pki_data: PKIUserData):
    """
    Update an existing user's PKI data and certificate metadata.

    Args:
        db: Database session
        user: Existing User object
        pki_data: PKI user data with certificate metadata

    Returns:
        Updated User object
    """
    subject_dn = pki_data["subject_dn"]
    cert_email = pki_data["email"]

    logger.info(f"Updating existing user from PKI: {subject_dn}")

    # Log if certificate email differs from stored email for audit purposes
    # PKI users are identified by DN, not email, so we don't auto-update email
    if cert_email and cert_email != user.email:
        logger.warning(
            f"SECURITY: PKI certificate email differs from stored email. "
            f"subject_dn={subject_dn}, stored_email={user.email}, cert_email={cert_email}. "
            "Email not updated - user identified by DN."
        )

    user.pki_subject_dn = subject_dn
    user.auth_type = AUTH_TYPE_PKI

    # Update certificate metadata if available
    if pki_data.get("serial_number"):
        user.pki_serial_number = pki_data["serial_number"]
    if pki_data.get("issuer_dn"):
        user.pki_issuer_dn = pki_data["issuer_dn"]
    if pki_data.get("organization"):
        user.pki_organization = pki_data["organization"]
    if pki_data.get("organizational_unit"):
        user.pki_organizational_unit = pki_data["organizational_unit"]
    if pki_data["common_name"]:
        user.pki_common_name = pki_data["common_name"]
    not_before_str = pki_data.get("not_before")
    if not_before_str:
        from datetime import datetime

        user.pki_not_before = datetime.fromisoformat(not_before_str)
    not_after_str = pki_data.get("not_after")
    if not_after_str:
        from datetime import datetime

        user.pki_not_after = datetime.fromisoformat(not_after_str)
    fingerprint = pki_data.get("fingerprint")
    if fingerprint:
        # Store fingerprint without colons for consistent storage
        user.pki_fingerprint_sha256 = fingerprint.replace(":", "")

    # Update admin role based on PKI_ADMIN_DNS list
    if pki_data["is_admin"]:
        # External IdPs grant at most 'admin'; never demote a local super_admin.
        if user.role not in ELEVATED_ROLES:
            logger.info(f"Promoting PKI user {subject_dn} to admin")
            user.role = ROLE_ADMIN
        user.is_superuser = role_implies_superuser(user.role)
    elif user.role == ROLE_ADMIN:
        # Demote if user was admin but no longer in PKI_ADMIN_DNS
        logger.info(f"Demoting PKI user {subject_dn} from admin (removed from PKI_ADMIN_DNS)")
        user.role = ROLE_USER
        user.is_superuser = role_implies_superuser(user.role)

    db.commit()
    return user


def _convert_local_user_to_pki(db, user, pki_data: PKIUserData):
    """
    Convert an existing local user to PKI authentication.

    This is called when a user with auth_type='local' authenticates
    via PKI certificate. The user is converted to PKI auth, which means:
    - auth_type is set to 'pki'
    - hashed_password is cleared (PKI users don't have local passwords)
    - pki_subject_dn is set from the certificate
    - Certificate metadata is stored
    - Admin role is updated based on PKI_ADMIN_DNS

    Args:
        db: Database session
        user: Existing User object with auth_type='local'
        pki_data: PKI user data from certificate

    Returns:
        Updated User object
    """
    subject_dn = pki_data["subject_dn"]

    logger.info(f"Converting local user {user.email} to PKI auth: {subject_dn}")

    # Convert to PKI authentication
    user.auth_type = AUTH_TYPE_PKI
    user.pki_subject_dn = subject_dn
    user.hashed_password = EXTERNAL_AUTH_NO_PASSWORD  # Clear local password

    # Store certificate metadata
    if pki_data.get("serial_number"):
        user.pki_serial_number = pki_data["serial_number"]
    if pki_data.get("issuer_dn"):
        user.pki_issuer_dn = pki_data["issuer_dn"]
    if pki_data.get("organization"):
        user.pki_organization = pki_data["organization"]
    if pki_data.get("organizational_unit"):
        user.pki_organizational_unit = pki_data["organizational_unit"]
    if pki_data["common_name"]:
        user.pki_common_name = pki_data["common_name"]
    not_before_str = pki_data.get("not_before")
    if not_before_str:
        from datetime import datetime

        user.pki_not_before = datetime.fromisoformat(not_before_str)
    not_after_str = pki_data.get("not_after")
    if not_after_str:
        from datetime import datetime

        user.pki_not_after = datetime.fromisoformat(not_after_str)
    fingerprint = pki_data.get("fingerprint")
    if fingerprint:
        # Store fingerprint without colons for consistent storage
        user.pki_fingerprint_sha256 = fingerprint.replace(":", "")

    # Update admin role based on PKI_ADMIN_DNS list
    if pki_data["is_admin"]:
        # External IdPs grant at most 'admin'; never demote a local super_admin.
        if user.role not in ELEVATED_ROLES:
            logger.info(f"Promoting converted PKI user {subject_dn} to admin")
            user.role = ROLE_ADMIN
        user.is_superuser = role_implies_superuser(user.role)
    elif user.role == ROLE_ADMIN:
        # Demote if user was admin locally but not in PKI_ADMIN_DNS
        logger.info(f"Demoting converted PKI user {subject_dn} from admin (not in PKI_ADMIN_DNS)")
        user.role = ROLE_USER
        user.is_superuser = role_implies_superuser(user.role)

    db.commit()
    return user


def sync_pki_user_to_db(db, pki_data: PKIUserData):
    """
    Create or update a user in the database from PKI certificate data.

    Handles:
    - Creating new users on first PKI login
    - Updating existing PKI users
    - Protecting existing local users from being converted
    - Admin role promotion and demotion based on PKI_ADMIN_DNS
    - Race conditions when multiple concurrent logins occur

    Lookup is ``pki_subject_dn`` first, then email. The email fallback links a
    certificate to a **pre-existing** account, so it goes through
    ``account_linking.assert_email_link_permitted`` — see that module for the rule,
    why a refusal fails the login instead of creating a second account, and the
    operator remedy.

    Args:
        db: Database session
        pki_data: User data from certificate

    Returns:
        User: The created or updated User object

    Raises:
        ValueError: If user cannot be created or found after race condition
        AuthenticationError: 401, when an email-matched link is refused. Deliberately the
            same response the PKI login endpoint returns for an invalid certificate,
            so a refusal is not an oracle for "this address exists".
    """
    from app.auth.account_linking import assert_email_link_permitted
    from app.auth.account_linking import assert_provider_id_link_permitted
    from app.core.auth_types import AUTH_TYPE_LOCAL
    from app.models.user import User

    subject_dn = pki_data["subject_dn"]
    email = pki_data["email"]

    # Check if user exists by pki_subject_dn first (most specific)
    user = db.query(User).filter(User.pki_subject_dn == subject_dn).first()
    if user:
        # A stored pki_subject_dn is not necessarily a deliberate admin link — JIT
        # provisioning stamps it on ordinary first logins too, so a reissued/
        # reassigned DN still needs the corroboration/super_admin guard.
        assert_provider_id_link_permitted(
            user,
            provider=AUTH_TYPE_PKI,
            source_identifier=subject_dn,
            asserted_email=email,
            failure_detail="Invalid or missing client certificate",
            failure_headers={"WWW-Authenticate": "Certificate"},
        )
    if not user and email:
        # The email fallback links this certificate to a PRE-EXISTING account,
        # so it is gated too.
        user = db.query(User).filter(User.email == email).first()
        if user:
            assert_email_link_permitted(
                user,
                provider=AUTH_TYPE_PKI,
                source_identifier=subject_dn,
                email_verified=PKI_ASSERTS_EMAIL_VERIFIED,
                failure_detail="Invalid or missing client certificate",
                failure_headers={"WWW-Authenticate": "Certificate"},
            )

    if not user:
        user = _create_pki_user(db, pki_data)
    elif user.auth_type == AUTH_TYPE_LOCAL:
        # Convert local users to PKI auth when they authenticate via PKI certificate
        # This ensures they use PKI going forward and cannot change their password
        # (since PKI users don't have local passwords)
        logger.warning(
            f"SECURITY: Converting local user {email} to PKI auth. "
            "User will now authenticate exclusively via PKI certificate. "
            "Local password will be cleared."
        )
        user = _convert_local_user_to_pki(db, user, pki_data)
    else:
        user = _update_pki_user(db, user, pki_data)

    db.refresh(user)
    return user
