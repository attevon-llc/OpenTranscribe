"""Just-in-time provisioning of SAML identities into the ``user`` table.

Mirrors ``app.auth.oidc.provisioning`` — same lookup order (provider identifier
first, then email with the shared ``account_linking`` guard), same admission-before-
create-or-link ordering, same generic refusal detail.

**Deliberately narrower than OIDC's provisioning** in one respect: it does not call
``services/idp_group_mapping_service`` (the ``group_mapping`` table). That table's
``source`` column is CHECK-constrained to a closed set (``ldap``, ``oidc``,
``proxy``), and widening it is a separate, independently reviewable schema change —
not bundled into this one. SAML gets its own simple allow/block-group admission gate
(``saml/admission.py``, identical semantics to OIDC's pre-group_mapping equivalent)
and its own ``admin_group`` -> role rule, evaluated here directly. Full group-mapping
UI integration for SAML is future work.
"""

import logging

from app.auth.constants import AUTH_TYPE_SAML
from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_USER
from app.auth.roles import role_implies_superuser
from app.auth.saml.assertion import SAMLUserData

logger = logging.getLogger(__name__)

#: Byte-identical to the 401 the ACS endpoint returns for an unusable assertion, so
#: a refusal cannot be used to probe which addresses already exist (see
#: ``auth/account_linking.py``).
LINK_REFUSED_DETAIL = "Invalid SAML assertion"  # noqa: S105 # nosec B105


def _create_saml_user(db, saml_data: SAMLUserData, *, is_admin: bool):
    """Create a new user from a verified SAML assertion."""
    from sqlalchemy.exc import IntegrityError

    from app.auth.approval import initial_approval_status
    from app.models.user import User

    subject = saml_data["saml_subject"]
    email = saml_data["email"] or f"{subject}@saml.local"

    logger.info(f"Creating new user from SAML: {subject} ({email})")

    # External IdPs grant at most 'admin'; super_admin is local-only.
    role = ROLE_ADMIN if is_admin else ROLE_USER
    user = User(
        email=email,
        full_name=saml_data["full_name"] or email.split("@")[0],
        hashed_password=EXTERNAL_AUTH_NO_PASSWORD,
        auth_type=AUTH_TYPE_SAML,
        saml_subject=subject,
        role=role,
        is_active=True,
        is_superuser=role_implies_superuser(role),
        # JIT provisioning is exactly the path administrator approval exists for:
        # the IdP decided this person is who they say they are, not that this
        # deployment wants them. 'approved' unless the setting is on.
        approval_status=initial_approval_status(db),
    )
    db.add(user)

    try:
        db.commit()
        return user
    except IntegrityError:
        db.rollback()
        logger.info(f"User {subject} was created by concurrent request, fetching existing user")
        user = db.query(User).filter(User.saml_subject == subject).first()
        if not user:
            user = db.query(User).filter(User.email == email).first()
        if not user:
            raise ValueError(f"Failed to create or find SAML user: {subject}") from None
        return user


def _update_saml_user(db, user, saml_data: SAMLUserData, *, is_admin: bool):
    """Update an existing SAML user's identity and role from a fresh assertion."""
    subject = saml_data["saml_subject"]
    email = saml_data["email"]

    logger.info(f"Updating existing user from SAML: {subject} ({email})")

    if email and email != user.email:
        logger.warning(
            f"SECURITY: User email changed during SAML login. "
            f"saml_subject={subject}, old_email={user.email}, new_email={email}"
        )
        user.email = email
    if saml_data["full_name"]:
        user.full_name = saml_data["full_name"]
    user.saml_subject = subject
    user.auth_type = AUTH_TYPE_SAML

    # Directory group membership can only add admin here, never remove it — this
    # module has no membership reconciliation (unlike OIDC's group_mapping path),
    # so silently demoting an admin whose group assertion briefly changed shape
    # would be a privilege change nobody could audit back to a cause.
    if is_admin and user.role == ROLE_USER:
        user.role = ROLE_ADMIN
        user.is_superuser = role_implies_superuser(ROLE_ADMIN)

    db.commit()
    return user


def _convert_local_user_to_saml(db, user, saml_data: SAMLUserData, *, is_admin: bool):
    """Convert an existing local user to SAML authentication."""
    subject = saml_data["saml_subject"]
    email = saml_data["email"]

    logger.info(f"Converting local user {user.email} to SAML auth: {subject}")

    user.auth_type = AUTH_TYPE_SAML
    user.saml_subject = subject
    user.hashed_password = EXTERNAL_AUTH_NO_PASSWORD

    if email and email != user.email:
        logger.warning(
            f"SECURITY: User email changed during SAML conversion. "
            f"saml_subject={subject}, old_email={user.email}, new_email={email}"
        )
        user.email = email
    if saml_data["full_name"]:
        user.full_name = saml_data["full_name"]
    if is_admin and user.role == ROLE_USER:
        user.role = ROLE_ADMIN
        user.is_superuser = role_implies_superuser(ROLE_ADMIN)

    db.commit()
    return user


def sync_saml_user_to_db(db, saml_data: SAMLUserData, cfg=None):
    """Create or update a user in the database from a verified SAML assertion.

    Admission is decided first, against ``saml_allowed_groups``/``saml_blocked_groups``
    (:mod:`app.auth.saml.admission`) — before either the create or the link branch,
    for the same reason OIDC's provisioning orders it that way: creating first would
    leave a row behind for a refused identity, and linking first would hand one a
    foothold on an existing account.

    Lookup is ``saml_subject`` first, then email. The email fallback links this
    identity to a pre-existing account and goes through
    ``account_linking.assert_email_link_permitted`` with
    ``email_verified=SAML_ASSERTS_EMAIL_VERIFIED`` (always ``False``) — so a SAML
    login can never take over an existing account by email match, only a
    deliberate admin action via ``PUT /api/admin/users/{uuid}/link-identity`` can.

    Args:
        db: Database session.
        saml_data: Data from the verified assertion.
        cfg: Resolved :class:`~app.auth.saml.config.SAMLConfig`. Resolved from the
            database when omitted.

    Raises:
        HTTPException: 401, when admission is refused or an email-matched link is.
    """
    from app.auth.account_linking import assert_email_link_permitted
    from app.auth.account_linking import assert_provider_id_link_permitted
    from app.auth.constants import AUTH_TYPE_LOCAL
    from app.auth.saml.admission import assert_saml_admission_permitted
    from app.auth.saml.config import SAMLConfig
    from app.models.user import User

    if cfg is None:
        cfg = SAMLConfig.from_db(db)
    assert_saml_admission_permitted(saml_data, cfg, failure_detail=LINK_REFUSED_DETAIL)

    subject = saml_data["saml_subject"]
    email = saml_data["email"]
    is_admin = bool(saml_data["is_admin"])

    user = db.query(User).filter(User.saml_subject == subject).first()
    if user:
        # A stored saml_subject is not necessarily a deliberate admin link — JIT
        # provisioning stamps it on ordinary first logins too, so a replayed or
        # reassigned NameID still needs the corroboration/super_admin guard.
        assert_provider_id_link_permitted(
            user,
            provider=AUTH_TYPE_SAML,
            source_identifier=subject,
            asserted_email=email,
            failure_detail=LINK_REFUSED_DETAIL,
        )
    if not user and email:
        user = db.query(User).filter(User.email == email).first()
        if user:
            assert_email_link_permitted(
                user,
                provider=AUTH_TYPE_SAML,
                source_identifier=subject,
                email_verified=saml_data["email_verified"],
                failure_detail=LINK_REFUSED_DETAIL,
            )

    if not user:
        user = _create_saml_user(db, saml_data, is_admin=is_admin)
    elif user.auth_type == AUTH_TYPE_LOCAL:
        logger.warning(
            f"SECURITY: Converting local user {email} to SAML auth. "
            "User will now authenticate exclusively via the identity provider. "
            "Local password will be cleared."
        )
        user = _convert_local_user_to_saml(db, user, saml_data, is_admin=is_admin)
    else:
        user = _update_saml_user(db, user, saml_data, is_admin=is_admin)

    db.refresh(user)
    return user
