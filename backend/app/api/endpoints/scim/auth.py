"""Bearer-token authentication for the SCIM surface.

Deliberately **not** the session dependency tree. A SCIM caller is an integration,
not a person: it has no ``User`` row, no role, no session to revoke, and none of the
account-lifecycle gates (approval, banner acknowledgment, forced password change)
mean anything for it. Reusing ``get_current_active_user`` would have required
inventing a service account to hang those gates off.

What replaces the role check is that this surface **cannot express** the privileged
operations. There is no route here that changes a role, edits auth configuration, or
reads the audit log, and ``scim_service`` refuses ``super_admin`` at every write
(``idp_group_mapping_service.assert_grantable_role``).

On rate limiting
----------------
These routes are **not** rate limited, and that is a decision rather than an
oversight. slowapi's limiter is per-IP; an IdP provisions from a small pool of
egress addresses and pushes bursts of hundreds of requests during an initial import,
so a per-IP limit would throttle the whole tenant to protect against a credential
that is 256 random bits and hashed at rest. The controls that do apply are the
token (unguessable, revocable, optionally expiring) and the fact that a token can
neither escalate privilege nor read transcript content. Consequently no handler here
declares ``response: Response`` — the requirement in
``tests/unit/test_rate_limited_endpoints_declare_response.py`` follows from
``@limiter.limit``, which none of these carry.
"""

from __future__ import annotations

import logging

from fastapi import Depends
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.scim.errors import SCIMError
from app.db.base import get_db
from app.models.scim_token import SCIMToken
from app.services.scim_token_service import verify_token

logger = logging.getLogger(__name__)

#: Sent on every 401 here. RFC 6750 §3; without it some connectors will not even
#: report the failure as an authentication problem.
_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": 'Bearer realm="scim"'}


def _bearer_credential(request: Request) -> str | None:
    """Extract the raw bearer token, if the header is well-formed.

    Cookies are ignored on purpose: a browser session must never authenticate a
    provisioning call, or any authenticated user's browser could be made to drive
    SCIM through a cross-site request.
    """
    header = request.headers.get("Authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def require_scim_token(
    request: Request,
    db: Session = Depends(get_db),
) -> SCIMToken:
    """Resolve and validate the SCIM bearer token, or refuse.

    Args:
        request: The incoming request.
        db: Database session.

    Returns:
        The verified :class:`~app.models.scim_token.SCIMToken` row, so handlers can
        attribute what they write to the integration that asked for it.

    Raises:
        SCIMError: 401 for a missing, malformed, unknown, revoked or expired token.
            All five share one message: distinguishing them tells an unauthenticated
            caller whether a token exists.
    """
    token = verify_token(db, _bearer_credential(request))
    if token is None:
        logger.warning(
            "Refused a SCIM request with a missing or unusable bearer token (%s %s)",
            request.method,
            request.url.path,
        )
        raise SCIMError(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or missing SCIM bearer token",
            headers=_UNAUTHORIZED_HEADERS,
        )
    return token
