"""Group-based admission control for OIDC logins.

The gap this closes
-------------------
``provisioning.sync_oidc_user_to_db`` created an account for **any** identity that
completed the flow. Point a deployment at a corporate identity provider — which is
the entire reason someone configures OIDC — and every person in that tenant
got an OpenTranscribe account, active and GPU-capable, on first sign-in. Whether the
identity provider authenticated them was never the same question as whether this
deployment wanted them.

LDAP has had the equivalent guard since it shipped (``ldap_user_groups`` ->
``ldap_auth._check_group_access``), so this is the missing half of a rule the product
already had, not a new policy. The vocabulary matches deliberately: semicolon
delimiter (:func:`app.auth.ldap_auth._parse_group_list`), case-insensitive
whitespace-stripped exact match, empty allow-list means "no requirement".

The rules
---------
1. **Blocked wins.** A value in ``blocked_groups`` denies access, whatever else the
   token carries. "Blocked" means *refused*, not "exempt from the allow-list" — a
   deployment that admits a whole tenant still needs a way to keep one group out.
2. **An empty allow-list admits everyone.** This preserves today's behaviour on
   upgrade. Reading empty as "admit nobody" would lock out every existing OIDC
   deployment the moment it took the update, which is the sort of fix that gets
   reverted rather than adopted.
3. **Only a non-empty allow-list restricts**, and then membership of at least one
   listed value is required.

Where it runs
-------------
At the very top of ``sync_oidc_user_to_db``: before an account is created, and
before an existing account is linked. Both matter. Creating first and checking
afterwards would leave a row behind for an identity that was refused, and linking
first would hand a refused identity a foothold on somebody's existing account.

The refusal
-----------
A single generic 401 — byte-identical to the one the callback returns for an
unusable token (:data:`app.auth.oidc.provisioning.LINK_REFUSED_DETAIL`). A distinct
message would answer "does this deployment know about me?" for anyone who can reach
the IdP, which is an account-existence oracle. The reason is recorded in the audit
log instead, where an operator can read it and an attacker cannot.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.constants import AUTH_TYPE_OIDC

logger = logging.getLogger(__name__)

#: Audit ``error_code`` for a refused admission. Distinct from
#: ``ACCOUNT_LINK_REFUSED`` (``auth/account_linking.py``) so the two reasons a
#: well-formed OIDC token still fails are separable in the audit index.
ADMISSION_REFUSED_ERROR_CODE = "OIDC_ADMISSION_REFUSED"

#: ``reason`` recorded when the identity holds a blocked group.
REASON_BLOCKED = "blocked_group"
#: ``reason`` recorded when the identity holds none of the required groups.
REASON_NOT_ALLOWED = "not_in_allowed_groups"


def parse_group_list(value: str | None) -> list[str]:
    """Split a semicolon-delimited group list into its entries.

    Semicolons, not commas: an LDAP/AD distinguished name contains commas as
    component separators, and providers that broker such a directory emit the DN
    verbatim in the groups claim. Sharing the delimiter with
    ``ldap_auth._parse_group_list`` means an operator configuring both methods
    writes one syntax, not two.

    Args:
        value: Raw configuration string, possibly ``None`` or blank.

    Returns:
        Entries with surrounding whitespace stripped and blanks dropped.
    """
    if not value:
        return []
    return [entry.strip() for entry in value.split(";") if entry.strip()]


def _normalized(values: Iterable[str]) -> set[str]:
    """Case-folded, whitespace-stripped set, for exact-match comparison."""
    return {str(value).strip().lower() for value in values if str(value).strip()}


def check_group_admission(
    claim_values: Iterable[str],
    *,
    allowed_groups: str | None,
    blocked_groups: str | None,
) -> str | None:
    """Decide whether an identity asserting *claim_values* may be admitted.

    Args:
        claim_values: Group/role names read from the configured roles claim.
        allowed_groups: Semicolon-delimited allow-list. Empty admits everyone.
        blocked_groups: Semicolon-delimited deny-list, evaluated first.

    Returns:
        ``None`` when the identity is admitted, otherwise the machine-readable
        refusal reason (:data:`REASON_BLOCKED` or :data:`REASON_NOT_ALLOWED`).
    """
    present = _normalized(claim_values)

    blocked = _normalized(parse_group_list(blocked_groups))
    if blocked & present:
        return REASON_BLOCKED

    allowed = _normalized(parse_group_list(allowed_groups))
    if not allowed:
        # The upgrade-safe default, and the documented one: an allow-list is only
        # a restriction once an operator writes something in it.
        return None

    if allowed & present:
        return None
    return REASON_NOT_ALLOWED


def assert_oidc_admission_permitted(
    oidc_data,
    cfg,
    *,
    failure_detail: str,
) -> None:
    """Refuse an OIDC identity this deployment does not admit.

    Runs before any row is created or linked. Audited as an ordinary login failure
    so it lands in the same place an operator already looks, carrying the reason
    the response deliberately withholds.

    Args:
        oidc_data: Verified claims (``OIDCUserData``); ``roles`` is the configured
            claim's contents and ``oidc_subject`` identifies the caller.
        cfg: Resolved :class:`~app.auth.oidc.config.OIDCConfig`.
        failure_detail: The ``HTTPException`` detail to raise. Must be identical to
            what this path returns for an ordinary credential failure.

    Raises:
        HTTPException: 401, when the identity is not admitted.
    """
    from fastapi import HTTPException
    from fastapi import status

    reason = check_group_admission(
        oidc_data.get("roles") or [],
        allowed_groups=getattr(cfg, "allowed_groups", ""),
        blocked_groups=getattr(cfg, "blocked_groups", ""),
    )
    if reason is None:
        return

    subject = str(oidc_data.get("oidc_subject") or "unknown")
    logger.warning(
        "SECURITY: refusing OIDC admission for subject %s (%s). Claim values: %s",
        subject,
        reason,
        list(oidc_data.get("roles") or []),
    )
    audit_logger.log(
        event_type=AuditEventType.AUTH_LOGIN_FAILURE,
        outcome=AuditOutcome.FAILURE,
        username=str(oidc_data.get("email") or subject),
        error_code=ADMISSION_REFUSED_ERROR_CODE,
        details={
            "auth_method": AUTH_TYPE_OIDC,
            "reason": reason,
            "oidc_subject": subject,
            "claim_values": list(oidc_data.get("roles") or []),
        },
    )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=failure_detail)
