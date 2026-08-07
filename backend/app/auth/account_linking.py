"""One rule for "may this external identity take over an existing account?".

Every external auth path (LDAP, PKI, and the registry-based JIT seam in
``external_sync.py``) resolves a user by provider id first and then, if that misses,
falls back to matching ``User.email``. That fallback is the account-takeover vector:
the email is an **attribute of the external source**, so whoever can write it —
a directory administrator setting ``mail``, a self-service directory where users edit
their own address, or anyone who can get a certificate issued — can point it at an
existing account and inherit it, including its content and its privileges.

``external_sync.sync_external_user_to_db`` already established the rule for the cloud
JIT seam and ``constants.CLOUD_SEAM_VERSION`` (v2) documents it:

1. link only when **the source asserts the address is verified**, and
2. **never** link a ``super_admin`` account by email, ever.

This module applies the same rule to the two paths that were still missing it. It is
the single implementation; do not add a third.

What happens on refusal
-----------------------
The **login fails**. Specifically it does *not* fall through to "create a new
account": the refusal happens precisely when an account with that email already
exists, so creating one would either collide on the unique email index or leave two
accounts for one person — a silent, confusing duplicate is a worse outcome than a
refused login, and it would also hand the attacker a fresh account with the victim's
address on it.

The refusal is audited as ``AUTH_LOGIN_FAILURE`` with ``error_code
ACCOUNT_LINK_REFUSED`` so it is greppable, and surfaces to the caller as the *same*
generic failure that path returns for a bad credential — a distinct error would tell
an attacker "that address exists and is privileged", which is exactly what the guard
is protecting.

Operator remedy: link the account deliberately instead of by email coincidence —
set the account's provider identifier (``ldap_uid`` / ``pki_subject_dn``) from the
admin UI, or change one of the two addresses. That is a decision an administrator
makes, not one an external directory makes on its own.
"""

import logging

from fastapi import HTTPException
from fastapi import status

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.roles import ROLE_SUPER_ADMIN

logger = logging.getLogger(__name__)

#: Audit ``error_code`` for a refused email-match link.
LINK_REFUSED_ERROR_CODE = "ACCOUNT_LINK_REFUSED"


def assert_email_link_permitted(
    user,
    *,
    provider: str,
    source_identifier: str,
    email_verified: bool,
    failure_detail: str,
    failure_headers: dict[str, str] | None = None,
) -> None:
    """Refuse an email-matched link that the source is not entitled to make.

    Call this **only** on the email-match branch. An account already carrying the
    provider's own identifier (``ldap_uid``, ``pki_subject_dn``, ``external_id``) was
    linked deliberately at some earlier point and is not re-litigated here.

    Args:
        user: The pre-existing ``User`` row that the email matched.
        provider: Auth-type string of the external source, for the log/audit record.
        source_identifier: The external identity being asserted (LDAP uid, subject DN).
        email_verified: Whether the source asserts this address as verified. Fails
            closed: pass ``False`` whenever the source has no verified-address concept.
        failure_detail: The ``HTTPException`` detail to raise. Must be byte-identical
            to what this auth path returns for an ordinary credential failure.
        failure_headers: Optional response headers to match that same failure.

    Raises:
        HTTPException: 401, when the link is refused.
    """
    reason: str | None = None
    if str(getattr(user, "role", "")) == ROLE_SUPER_ADMIN:
        # Unconditional, and checked first: a platform-owner account is never
        # acquired through an external directory, verified address or not.
        reason = "super_admin_never_linked"
    elif not email_verified:
        reason = "email_not_verified"

    if reason is None:
        return

    logger.warning(
        "SECURITY: refusing to link %s identity %s to existing account %s (%s)",
        provider,
        source_identifier,
        getattr(user, "email", "?"),
        reason,
    )
    audit_logger.log(
        event_type=AuditEventType.AUTH_LOGIN_FAILURE,
        outcome=AuditOutcome.FAILURE,
        user_id=getattr(user, "id", None),
        username=source_identifier,
        error_code=LINK_REFUSED_ERROR_CODE,
        details={
            "auth_method": provider,
            "reason": reason,
            "matched_by": "email",
        },
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=failure_detail,
        headers=failure_headers,
    )
