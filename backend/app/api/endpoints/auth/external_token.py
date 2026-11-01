"""Session-identity checks that sit beside the local-JWT path.

The external-verifier seam (cloud edition: an external bearer token, its absolute
session timeout and JIT sync) and the trusted-proxy identity consistency check.
Imports from ``lifecycle_gates`` only; nothing there imports back.
"""

import logging

from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth.lifecycle_gates import _audit_lifecycle_denial
from app.api.endpoints.auth.lifecycle_gates import _get_client_info
from app.api.endpoints.auth.lifecycle_gates import _route_path
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.auth.provider_registry import ExternalIdentity
from app.models.user import User

logger = logging.getLogger(__name__)

#: The trusted proxy is now asserting a different person than the one this session
#: was minted for — see :func:`_enforce_proxy_identity_consistency`.
ERROR_CODE_PROXY_IDENTITY_MISMATCH = "proxy_identity_mismatch"


def _enforce_proxy_identity_consistency(user: User, request: Request | None, db: Session) -> None:
    """Refuse — and revoke — when the proxy now asserts somebody else.

    Trusted-header authentication consults the header once, at sign-in, and then
    relies on an ordinary session. That is the right architecture (a per-request
    identity derivation is a second authorization path to keep correct), but it has
    one failure mode: signing out of the upstream identity provider and back in as a
    different person leaves the previous app session live and usable. Open WebUI
    shipped exactly that bug and retrofitted a per-request equality check in v0.6.14
    (issue #14406); this is that check, written up front.

    Three deliberate narrowings, each of which would otherwise be a denial of
    service rather than a control:

    * **Only accounts whose ``auth_type`` is ``proxy``.** A local ``super_admin``
      break-glass session must not be terminated by a header it never used.
    * **Only a header from a trusted peer.** Honouring an untrusted one would let
      anyone who can reach the backend log out any proxy user at will.
    * **Absence is not an assertion.** A request that did not traverse the proxy
      carries no header, which is not the same as carrying a different identity.
      Only a *present and different* address revokes.

    Revocation rather than a bare 401 is the point: the session is now known to
    belong to nobody, and leaving the refresh token rotating would let the previous
    identity keep renewing it.

    Args:
        user: The authenticated account.
        request: The incoming request; ``None`` (test stand-ins) short-circuits.
        db: The request's session, used for the revocation.

    Raises:
        HTTPException: 401 with ``detail.code == proxy_identity_mismatch``.
    """
    from app.core.auth_types import AUTH_TYPE_PROXY

    if request is None or str(getattr(user, "auth_type", "")) != AUTH_TYPE_PROXY:
        return

    from app.core.process_auth_settings import get_process_auth_settings

    # Process-level, not a per-request DB read: this runs on every authenticated
    # request, and the layered cache is the same one the password policy and the
    # lockout counter use.
    proxy_settings = get_process_auth_settings()
    if not proxy_settings.proxy_enabled:
        return

    asserted = request.headers.get(proxy_settings.proxy_email_header)
    if not asserted:
        return

    from app.auth.header_trust import header_source_is_trusted
    from app.auth.header_trust import parse_trusted_proxies

    networks = parse_trusted_proxies(proxy_settings.proxy_trusted_proxies)
    if not header_source_is_trusted(request, networks):
        return

    if asserted.strip().lower() == str(user.email).strip().lower():
        return

    from app.services.account_security_service import revoke_all_sessions

    revoked = revoke_all_sessions(db, user, reason="proxy_identity_mismatch")
    db.commit()
    _audit_lifecycle_denial(
        AuditEventType.AUTH_SESSION_TERMINATED,
        user,
        request,
        error_code="PROXY_IDENTITY_MISMATCH",
        details={
            "asserted_identity": asserted.strip().lower(),
            "sessions_revoked": revoked,
            "path": _route_path(request),
        },
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": ERROR_CODE_PROXY_IDENTITY_MISMATCH,
            "message": "Identity mismatch. Please sign in again.",
        },
    )


def _enforce_external_absolute_timeout(request: Request, identity: ExternalIdentity) -> None:
    """401 ``session_expired`` when an external session outlived the absolute timeout.

    See ``app.auth.external_session`` (issue #1106).
    """
    from app.auth.external_session import ERROR_CODE_SESSION_EXPIRED
    from app.auth.external_session import external_session_expired

    if not external_session_expired(identity):
        return

    client_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.AUTH_SESSION_EXPIRED,
        outcome=AuditOutcome.FAILURE,
        username=identity.email,
        source_ip=client_ip,
        user_agent=user_agent,
        details={"provider": identity.provider, "reason": "absolute_timeout"},
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": ERROR_CODE_SESSION_EXPIRED,
            "message": "Your session has expired. Sign in again.",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authenticate_external_token(request: Request, token: str, db: Session) -> User | None:
    """Resolve a bearer token via the external-verifier seam (cloud edition).

    Returns the JIT-synced user when a registered verifier claims the token,
    or ``None`` to fall through to the local-JWT path. Sync failures — a
    refused link (unverified email match / protected account) or a DB error —
    surface as clean 401s, matching the optional-auth and WebSocket branches'
    containment.
    """
    from app.auth.provider_registry import has_verifiers

    if not has_verifiers():
        return None

    from app.auth.external_sync import sync_external_user_to_db
    from app.auth.provider_registry import verify_external_token
    from app.core.exceptions import ExternalIdentityLinkRefusedError

    external_identity = verify_external_token(token, request)
    if external_identity is None:
        return None

    # Before JIT sync, so an expired session writes nothing.
    _enforce_external_absolute_timeout(request, external_identity)

    try:
        external_user = sync_external_user_to_db(db, external_identity)
    except ExternalIdentityLinkRefusedError as e:
        # Deliberately narrowed from the builtin PermissionError (#914 STEP 6):
        # a real EACCES anywhere in this call chain is also a PermissionError
        # (it's an OSError subclass), and this branch renders its `str(e)`
        # straight into the response. A genuine filesystem-permission failure
        # now falls through to the generic `except Exception` below instead.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except Exception:
        logger.exception("External JIT sync failed; rejecting credential")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not provision external identity",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    if not external_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    # Stash for get_current_context() so org scoping doesn't re-verify.
    request.state.external_identity = external_identity
    # Cloud contract: the access log / observability layer reads these
    # off request.state. user_id is always set; org_id is the provider's
    # raw org string here and is refined to our local Organization.id in
    # deps_context.get_current_context() when org context applies. Never
    # used as a Prometheus label — access log only.
    request.state.user_id = external_user.id
    request.state.org_id = getattr(external_user, "external_org_id", None)
    return external_user
