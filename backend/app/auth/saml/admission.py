"""Group-based admission control for SAML logins.

The identical gap OIDC admission closes (see ``oidc/admission.py`` for the full
rationale), for a third identity source: completing the SAML flow answers "did the
IdP authenticate this person", never "does this deployment want them". Reuses
``oidc.admission``'s ``parse_group_list``/``check_group_admission`` — the group-list
syntax and blocked-wins-then-empty-allows-everyone semantics are protocol-agnostic,
so a second implementation here would only be a second place for the two to drift.
"""

from __future__ import annotations

import logging

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.constants import AUTH_TYPE_SAML
from app.auth.oidc.admission import check_group_admission
from app.auth.saml.assertion import SAMLUserData

logger = logging.getLogger(__name__)

#: Distinct from OIDC's and account_linking's error codes so the audit index can
#: separate "which policy refused this login" at a glance.
ADMISSION_REFUSED_ERROR_CODE = "SAML_ADMISSION_REFUSED"


def assert_saml_admission_permitted(
    saml_data: SAMLUserData,
    cfg,
    *,
    failure_detail: str,
) -> None:
    """Refuse a SAML identity this deployment does not admit.

    Args:
        saml_data: Verified assertion data; ``groups`` is the configured groups
            attribute's values and ``saml_subject`` identifies the caller (the
            assertion's ``NameID``).
        cfg: Resolved :class:`~app.auth.saml.config.SAMLConfig`.
        failure_detail: The ``HTTPException`` detail to raise. Must be identical to
            what this path returns for an ordinary credential failure — a distinct
            message would be an account-existence oracle.

    Raises:
        HTTPException: 401, when the identity is not admitted.
    """
    from fastapi import HTTPException
    from fastapi import status

    reason = check_group_admission(
        saml_data.get("groups") or [],
        allowed_groups=getattr(cfg, "allowed_groups", ""),
        blocked_groups=getattr(cfg, "blocked_groups", ""),
    )
    if reason is None:
        return

    subject = str(saml_data.get("saml_subject") or "unknown")
    logger.warning(
        "SECURITY: refusing SAML admission for subject %s (%s). Groups: %s",
        subject,
        reason,
        list(saml_data.get("groups") or []),
    )
    audit_logger.log(
        event_type=AuditEventType.AUTH_LOGIN_FAILURE,
        outcome=AuditOutcome.FAILURE,
        username=str(saml_data.get("email") or subject),
        error_code=ADMISSION_REFUSED_ERROR_CODE,
        details={
            "auth_method": AUTH_TYPE_SAML,
            "reason": reason,
            "saml_subject": subject,
            "groups": list(saml_data.get("groups") or []),
        },
    )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=failure_detail)
