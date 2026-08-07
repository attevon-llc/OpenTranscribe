"""Just-in-time provisioning of a proxy-asserted identity into the ``user`` table.

The identity key is the **email address** and nothing else — unlike PKI (subject DN)
or OIDC (``sub``), a trusted-header deployment has no second identifier to bind on.
That has one consequence worth stating plainly, because it is a deliberate departure
from ``PKI_ASSERTS_EMAIL_VERIFIED``: see :data:`PROXY_ASSERTS_EMAIL_VERIFIED`.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from fastapi import status

from app.auth.constants import AUTH_TYPE_PROXY
from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.proxy.assertion import REFUSAL_DETAIL
from app.auth.proxy.assertion import ProxyAssertion
from app.auth.proxy.config import ProxyConfig
from app.auth.roles import ROLE_USER
from app.auth.roles import role_implies_superuser

logger = logging.getLogger(__name__)

#: Whether the address a proxy asserts may be used to take over a **pre-existing**
#: account. Unlike the PKI path, which fails closed because its address is parsed out
#: of a DN string the proxy happened to supply, here the address IS the assertion:
#: it came from a peer in the CIDR allowlist that (optionally) also proved the shared
#: secret, and it is the only identifier the feature has. Reading it as unverified
#: would refuse every login by anyone whose account already exists — which is every
#: deployment that turns this on after using something else.
#:
#: The rule that still applies, unconditionally, is the first one in
#: ``auth/account_linking.py``: a ``super_admin`` account is **never** acquired
#: through an external source, verified address or not.
PROXY_ASSERTS_EMAIL_VERIFIED = True

#: Audit ``error_code`` when an unknown identity arrives and JIT is off.
REFUSED_NO_ACCOUNT = "PROXY_JIT_DISABLED"


def _create_proxy_user(db, assertion: ProxyAssertion):
    """Create a new account from a proxy assertion.

    Raises:
        ValueError: The row could neither be created nor found after a race.
    """
    from sqlalchemy.exc import IntegrityError

    from app.auth.approval import initial_approval_status
    from app.models.user import User

    logger.info("Creating new user from proxy assertion: %s", assertion.email)
    # External IdPs grant at most 'admin'; the role header is applied by
    # reconcile_user after this returns, so the row starts at the floor.
    user = User(
        email=assertion.email,
        full_name=assertion.full_name or assertion.email.split("@")[0],
        hashed_password=EXTERNAL_AUTH_NO_PASSWORD,
        auth_type=AUTH_TYPE_PROXY,
        role=ROLE_USER,
        is_active=True,
        is_superuser=role_implies_superuser(ROLE_USER),
        # Same rule as every other JIT path: the proxy decided this person is who
        # they say they are, not that this deployment wants an account for them.
        approval_status=initial_approval_status(db),
    )
    db.add(user)
    try:
        db.commit()
        return user
    except IntegrityError:
        db.rollback()
        logger.info("User %s was created concurrently; fetching the existing row", assertion.email)
        existing = db.query(User).filter(User.email == assertion.email).first()
        if not existing:
            raise ValueError(f"Failed to create or find proxy user: {assertion.email}") from None
        return existing


def _refuse(email: str, error_code: str, reason: str) -> None:
    """Audit and raise the single generic refusal."""
    from app.auth.audit import AuditEventType
    from app.auth.audit import AuditOutcome
    from app.auth.audit import audit_logger

    logger.warning("Refusing proxy login for %s: %s", email, reason)
    audit_logger.log(
        event_type=AuditEventType.AUTH_LOGIN_FAILURE,
        outcome=AuditOutcome.FAILURE,
        username=email,
        error_code=error_code,
        details={"auth_method": AUTH_TYPE_PROXY, "reason": reason},
    )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=REFUSAL_DETAIL)


def sync_proxy_user_to_db(db, assertion: ProxyAssertion, cfg: ProxyConfig):
    """Create or update the account a proxy assertion names, then reconcile it.

    Group membership and privilege go through
    ``services/idp_group_mapping_service.reconcile_user`` — the same reconciler LDAP
    and OIDC use, never a second one. Two flags on that call carry this method's
    specific contract:

    * ``reconcile_memberships`` is False when the groups header was **absent**. A
      proxy that does not send the header is not managing groups; reconciling
      against an empty list would strip every proxy-sourced membership the user has.
      An *empty* header is a different instruction and does reconcile to empty.
    * ``apply_role`` is False when neither a role header nor a group assertion is
      present, so a deployment that has not opted into header-driven privilege never
      has an existing ``admin`` silently demoted on login.

    Args:
        db: Database session.
        assertion: The validated assertion.
        cfg: Resolved proxy configuration.

    Returns:
        The ``User`` row for this identity.

    Raises:
        HTTPException: 401 when JIT provisioning is off and no account exists, or
            when linking to an existing account is refused. Both are the same
            response an untrusted peer gets — a distinct one would be an
            account-existence oracle.
    """
    from app.auth.account_linking import assert_email_link_permitted
    from app.auth.constants import AUTH_TYPE_LOCAL
    from app.models.group import MAPPING_SOURCE_PROXY
    from app.models.user import User
    from app.services.idp_group_mapping_service import reconcile_user

    user = db.query(User).filter(User.email == assertion.email).first()

    if user is None:
        if not cfg.jit_provisioning:
            _refuse(assertion.email, REFUSED_NO_ACCOUNT, "jit_provisioning_disabled")
        user = _create_proxy_user(db, assertion)
    else:
        # Every proxy login is an email match by construction, so the linking rule
        # is consulted on every one of them. It is a no-op for an account already
        # carrying auth_type='proxy'; what it still refuses is a super_admin.
        assert_email_link_permitted(
            user,
            provider=AUTH_TYPE_PROXY,
            source_identifier=assertion.email,
            email_verified=PROXY_ASSERTS_EMAIL_VERIFIED,
            failure_detail=REFUSAL_DETAIL,
        )
        if str(user.auth_type) == AUTH_TYPE_LOCAL:
            logger.warning(
                "SECURITY: converting local user %s to proxy auth. The account will "
                "authenticate through the reverse proxy from now on and its local "
                "password is cleared.",
                assertion.email,
            )
            user.hashed_password = EXTERNAL_AUTH_NO_PASSWORD  # type: ignore[assignment]
        user.auth_type = AUTH_TYPE_PROXY  # type: ignore[assignment]
        if assertion.full_name:
            user.full_name = assertion.full_name  # type: ignore[assignment]
        db.commit()

    reconcile_user(
        db,
        user,
        MAPPING_SOURCE_PROXY,
        list(assertion.groups),
        legacy_admin=assertion.is_admin,
        reason="proxy_login",
        reconcile_memberships=assertion.groups_asserted,
        apply_role=bool(assertion.role) or assertion.groups_asserted,
    )
    db.refresh(user)
    return user
