"""FastAPI authentication dependencies (the DI surface for every router).

``get_current_active_user`` and friends are imported by ~30 endpoint
modules; this package deliberately has no ``deps.py``. Nothing here
registers a route, so importing it can never cycle back through the
package ``__init__``.
"""

import logging
import os
from datetime import UTC
from datetime import datetime
from uuid import UUID

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.security import OAuth2PasswordBearer
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from joserfc.jwt import JWTClaimsRegistry
from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.token_service import token_service
from app.core.config import settings
from app.core.security import accepted_algorithms
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import TokenPayload

logger = logging.getLogger(__name__)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_PREFIX}/auth/token", auto_error=False)


# ── account-lifecycle gate (FedRAMP AC-2 / IA-5) ────────────────────────────────
#
# ``must_change_password`` and ``account_expires_at`` were both written and never
# read: the admin "force change on next login" flag let the user sign in with the
# admin-chosen password and never prompted, and a time-boxed contractor account
# stayed usable forever. Both are enforced here, at the one dependency every
# user-facing route passes through.

#: Machine-readable codes carried in the 403 ``detail``. The SPA branches on
#: ``detail.code`` to render the forced-change screen; prose is not a contract,
#: and a client that has to string-match an English message breaks on rewording.
ERROR_CODE_PASSWORD_CHANGE_REQUIRED = "password_change_required"  # noqa: S105 # nosec B105
ERROR_CODE_ACCOUNT_EXPIRED = "account_expired"
ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED = "banner_acknowledgment_required"
#: Account exists and its credential worked, but an administrator has not admitted
#: it yet (``v379``; see ``app/auth/approval.py``). Its own code rather than a reuse
#: of ``account_expired`` because the remedy is somebody else's action, not the
#: user's, and the SPA has to say so.
ERROR_CODE_ACCOUNT_PENDING_APPROVAL = "account_pending_approval"
#: An administrator refused the account. Distinct from the code above because
#: telling a rejected applicant they are "pending" is simply false, and because the
#: two are enforced under different conditions — see :func:`_enforce_approval`.
ERROR_CODE_ACCOUNT_REJECTED = "account_rejected"
#: The trusted proxy is now asserting a different person than the one this session
#: was minted for — see :func:`_enforce_proxy_identity_consistency`.
ERROR_CODE_PROXY_IDENTITY_MISMATCH = "proxy_identity_mismatch"

#: Routes that stay reachable while ``must_change_password`` is set: the endpoint
#: that CLEARS the flag (``PUT /users/me`` — the self-service password change; its
#: GET twin is the caller's own profile, which the change screen renders), plus the
#: routes that end the session. Logout does not resolve through this dependency
#: today; it is listed so the exemption survives a refactor that gives it one.
#: Matched against the resolved route template, so a path parameter can never be
#: crafted to look like an exempt path.
PASSWORD_CHANGE_EXEMPT_PATHS = frozenset(
    {
        f"{settings.API_PREFIX}/users/me",
        f"{settings.API_PREFIX}/auth/logout",
        f"{settings.API_PREFIX}/auth/logout/all",
    }
)

#: Routes that stay reachable while the login banner is unacknowledged: the
#: endpoint that RECORDS the acknowledgment (without it the user can never clear
#: the gate), the banner text itself, and the routes that end the session. Same
#: route-template matching as above, for the same reason.
BANNER_EXEMPT_PATHS = frozenset(
    {
        f"{settings.API_PREFIX}/auth/banner",
        f"{settings.API_PREFIX}/auth/banner/acknowledge",
        f"{settings.API_PREFIX}/auth/logout",
        f"{settings.API_PREFIX}/auth/logout/all",
    }
)


def _route_path(request: Request | None) -> str:
    """Return the matched route template for *request*.

    Falls back to the raw URL path when no route has been matched yet (and to
    an empty string for a request stand-in), which fails safe: an unknown path
    is not in the exempt set, so the caller is refused.
    """
    scope = getattr(request, "scope", None)
    route = scope.get("route") if isinstance(scope, dict) else None
    path = getattr(route, "path", None) or getattr(getattr(request, "url", None), "path", "") or ""
    return path.rstrip("/") if len(path) > 1 else path


def _lifecycle_client_info(request: Request | None) -> tuple[str, str]:
    """Client IP / user agent for a lifecycle audit event, never raising.

    An audit record must not be the reason an authorization decision fails, so
    an unusual request object degrades to "unknown" instead of a 500.
    """
    try:
        return _get_client_info(request)  # type: ignore[arg-type]
    except Exception:
        logger.debug("Could not resolve client info for lifecycle audit", exc_info=True)
        return "unknown", "unknown"


def _audit_lifecycle_denial(
    event_type: AuditEventType,
    user: User,
    request: Request | None,
    error_code: str,
    details: dict,
) -> None:
    """Record an account-lifecycle access denial (FedRAMP AU-2)."""
    client_ip, user_agent = _lifecycle_client_info(request)
    audit_logger.log(
        event_type=event_type,
        outcome=AuditOutcome.FAILURE,
        user_id=getattr(user, "id", None),
        username=str(getattr(user, "email", "") or ""),
        source_ip=client_ip,
        user_agent=user_agent,
        error_code=error_code,
        details=details,
    )


def _enforce_account_expiry(user: User, request: Request | None) -> None:
    """Refuse a time-boxed account past its ``account_expires_at``.

    Unconditional — unlike a forced password change there is no self-service
    remedy, so no route is exempt.

    Raises:
        HTTPException: 403 with ``detail.code == account_expired``.
    """
    expires_at = getattr(user, "account_expires_at", None)
    if expires_at is None:
        return

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if datetime.now(UTC) < expires_at:
        return

    _audit_lifecycle_denial(
        AuditEventType.AUTH_ACCOUNT_EXPIRED,
        user,
        request,
        error_code="ACCOUNT_EXPIRED",
        details={"expired_at": expires_at.isoformat(), "path": _route_path(request)},
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": ERROR_CODE_ACCOUNT_EXPIRED,
            "message": (
                f"This account expired on {expires_at.date().isoformat()}. "
                "Contact an administrator to extend it."
            ),
        },
    )


def _enforce_approval(user: User, request: Request | None, db: Session) -> None:
    """Refuse an account an administrator has not admitted (or has refused).

    The two states are enforced under deliberately different conditions:

    * ``pending`` only bites while ``require_account_approval`` is on. Turning the
      setting off is the operator's escape hatch — it releases a queue they no
      longer want to work through — and leaving pending accounts stranded after
      the policy was withdrawn would make the control hard to reverse.
    * ``rejected`` bites unconditionally. It is a decision about one account, like
      deactivation, not a policy that can be switched off underneath it.

    Unconditional in the other sense too: no route is exempt. Unlike a forced
    password change there is nothing the user can do, so routing them anywhere
    would only be routing them to a screen that cannot help.

    Args:
        user: The authenticated account.
        request: Used for the audit record's client attribution.
        db: Session used to resolve the setting (already open for this request).

    Raises:
        HTTPException: 403 with ``detail.code`` ``account_pending_approval`` or
            ``account_rejected``.
    """
    from app.auth.approval import approval_required
    from app.auth.approval import is_pending
    from app.auth.approval import is_rejected

    if is_rejected(user):
        code, message = (
            ERROR_CODE_ACCOUNT_REJECTED,
            "This account was not approved. Contact an administrator.",
        )
    elif is_pending(user) and approval_required(db):
        code, message = (
            ERROR_CODE_ACCOUNT_PENDING_APPROVAL,
            "This account is awaiting administrator approval.",
        )
    else:
        return

    # Audited, unlike the banner gate: this fires for individually flagged
    # accounts rather than for every session of every user, so the volume is
    # bounded — and "a refused account kept trying" is exactly what an operator
    # reviewing an approval queue wants to see.
    _audit_lifecycle_denial(
        AuditEventType.AUTH_ACCOUNT_DISABLED,
        user,
        request,
        error_code=code.upper(),
        details={
            "approval_status": str(getattr(user, "approval_status", "")),
            "path": _route_path(request),
        },
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": code, "message": message},
    )


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
    from app.auth.constants import AUTH_TYPE_PROXY

    if request is None or str(getattr(user, "auth_type", "")) != AUTH_TYPE_PROXY:
        return

    from app.core.auth_settings import get_process_auth_settings

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


def _enforce_password_change(user: User, request: Request | None) -> None:
    """Confine a caller flagged ``must_change_password`` to the change endpoint.

    Raises:
        HTTPException: 403 with ``detail.code == password_change_required``.
    """
    if not getattr(user, "must_change_password", False):
        return

    path = _route_path(request)
    if path in PASSWORD_CHANGE_EXEMPT_PATHS:
        return

    _audit_lifecycle_denial(
        AuditEventType.AUTH_PASSWORD_EXPIRED,
        user,
        request,
        error_code="PASSWORD_CHANGE_REQUIRED",
        details={"reason": "must_change_password", "path": path},
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": ERROR_CODE_PASSWORD_CHANGE_REQUIRED,
            "message": "You must change your password before continuing.",
        },
    )


def _banner_requirement(db: Session | None) -> tuple[bool, datetime | None]:
    """Return ``(banner_enabled, text_last_changed_at)`` for this deployment.

    Reads the two ``auth_config`` rows in one query — the same DB-over-.env
    precedence the banner endpoints use, so a banner enabled in the admin UI is
    enforced, not just displayed.

    ``text_last_changed_at`` is the ``login_banner_text`` row's ``updated_at``,
    which is what makes an acknowledgment expire when the wording changes; see
    :func:`_enforce_banner_acknowledgment`. It is ``None`` when the text comes
    from ``.env`` (no row, hence no change history — and an ``.env`` edit needs a
    restart anyway).

    Args:
        db: Database session, or anything without a ``query`` when none is
            available. Falls back to the environment values rather than raising:
            this runs on every authenticated request.

    Returns:
        Whether the banner is enabled, and when its text last changed.
    """
    if db is None or not hasattr(db, "query"):
        return settings.LOGIN_BANNER_ENABLED, None

    try:
        from app.models.auth_config import AuthConfig

        rows = {
            row.config_key: row
            for row in db.query(AuthConfig)
            .filter(AuthConfig.config_key.in_(("login_banner_enabled", "login_banner_text")))
            .all()
        }
    except Exception:
        logger.debug("Could not read banner configuration; using .env", exc_info=True)
        return settings.LOGIN_BANNER_ENABLED, None

    enabled_row = rows.get("login_banner_enabled")
    if enabled_row is None or enabled_row.config_value is None:
        enabled = settings.LOGIN_BANNER_ENABLED
    else:
        from app.services.auth_config_service import BOOL_TRUE_VALUES

        enabled = str(enabled_row.config_value).strip().lower() in BOOL_TRUE_VALUES

    text_row = rows.get("login_banner_text")
    return enabled, getattr(text_row, "updated_at", None)


def _enforce_banner_acknowledgment(user: User, request: Request | None, db: Session) -> None:
    """Confine a caller who has not accepted the login banner (FedRAMP AC-8).

    ``banner_acknowledged_at`` was written by ``POST /auth/banner/acknowledge``
    — whose own docstring says it "must be called after login before granting
    full access" — and read by **nothing**. The SPA approximated the control with
    a ``sessionStorage`` flag, which clears per tab, is trivially removed, and
    never reaches the server at all, so the consent AC-8 requires was never
    actually a precondition for anything.

    An acknowledgment **expires when the banner text changes.** A user who
    accepted "UNCLASSIFIED — monitoring in effect" has not accepted a later
    "SECRET — no personal use"; treating the old click as consent to new wording
    is precisely the thing the control exists to prevent, and an admin editing
    the banner is a deliberate act with an obvious expectation. The comparison is
    against the ``login_banner_text`` row's ``updated_at``, so it costs no schema
    and no extra query.

    Raises:
        HTTPException: 403 with ``detail.code == banner_acknowledgment_required``.
    """
    enabled, text_changed_at = _banner_requirement(db)
    if not enabled:
        return

    path = _route_path(request)
    if path in BANNER_EXEMPT_PATHS:
        return

    acknowledged_at = getattr(user, "banner_acknowledged_at", None)
    if acknowledged_at is not None:
        if acknowledged_at.tzinfo is None:
            acknowledged_at = acknowledged_at.replace(tzinfo=UTC)
        changed_at = text_changed_at
        if changed_at is not None and changed_at.tzinfo is None:
            changed_at = changed_at.replace(tzinfo=UTC)
        if changed_at is None or acknowledged_at >= changed_at:
            return
        reason = "banner_text_changed"
    else:
        reason = "never_acknowledged"

    # Deliberately NOT audited. Unlike the two gates above — which fire for one
    # flagged account at a time — this refuses every request of every session
    # until the user clicks through, so per-request events would swamp the
    # OpenSearch audit index. The AC-8 artefact is the acknowledgment itself,
    # which `POST /auth/banner/acknowledge` already records.
    logger.debug("Banner acknowledgment required for user %s (%s)", user.id, reason)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED,
            "message": "You must acknowledge the login banner before continuing.",
            "reason": reason,
        },
    )


def _get_client_info(request: Request | None) -> tuple[str, str]:
    """Extract client IP and user agent from request.

    Args:
        request: FastAPI request object, or ``None`` where none is in scope.

    Returns:
        Tuple of (client_ip, user_agent); ``("unknown", "unknown")`` when *request* is
        ``None``. Callers audit from service-layer and background paths that genuinely
        have no request, and an audit record that cannot resolve an address must never
        turn the operation it is recording into a 500.
    """
    if request is None:
        return "unknown", "unknown"

    # Resolve through the trusted-proxy chain, not the raw peer: behind a reverse proxy
    # request.client.host is the PROXY, so every audited login recorded the proxy's
    # address instead of the user's (issue #284 A0.5).
    from app.utils.client_ip import resolve_client_ip

    client_ip = resolve_client_ip(request)
    user_agent = request.headers.get("User-Agent", "unknown")
    return client_ip, user_agent


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

    external_identity = verify_external_token(token, request)
    if external_identity is None:
        return None

    try:
        external_user = sync_external_user_to_db(db, external_identity)
    except PermissionError as e:
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


def get_current_user(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Get the current user from JWT token (Bearer header or httpOnly cookie).

    Checks Bearer header first (for API clients, Swagger UI), then falls
    back to the httpOnly access_token cookie (browser frontend).

    When TOKEN_REVOCATION_ENABLED is true, also checks if the token's JTI
    is on the revocation blacklist (FedRAMP AC-12 compliance).
    """
    from app.auth.cookies import get_access_token_from_cookie

    # Try Bearer header first, then fall back to httpOnly cookie
    if not token:
        token = get_access_token_from_cookie(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Cloud-edition seam: offer the token to registered external verifiers
    # before the local-JWT path. The community edition registers none, so this
    # is a no-op there.
    external_user = _authenticate_external_token(request, token, db)
    if external_user is not None:
        return external_user

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        key = OctKey.import_key(settings.JWT_SECRET_KEY)
        # ONE owner for "which algorithms are accepted" — core.security.
        # accepted_algorithms — shared with get_optional_current_user below,
        # core.security.verify_token (the WebSocket/SAML verifier) and
        # token_service.verify_token_with_fallback. This site used to hardcode
        # [settings.JWT_ALGORITHM] while verify_token ran its own FIPS-aware list, so
        # a FIPS-strict deployment authenticated HTTP requests and refused every
        # WebSocket handshake.
        token_obj = jwt.decode(token, key, algorithms=accepted_algorithms(TOKEN_TYPE_ACCESS))
        # joserfc verifies the signature/algorithm only — exp is not checked
        # automatically (unlike python-jose), so it's validated explicitly here.
        JWTClaimsRegistry(exp={"essential": True}).validate(token_obj.claims)
        payload = token_obj.claims
        user_uuid_str: str | None = payload.get("sub")  # UUID string from token
        user_role: str | None = payload.get("role")  # Extract role from token
        token_jti: str | None = payload.get("jti")  # JWT ID for revocation checking
        if user_uuid_str is None:
            raise credentials_exception

        # Purpose binding. The MFA half-token is handed to a client that has NOT
        # yet passed the second factor, and is signed with the same key/algorithm
        # as an access token — without this check it is a complete MFA bypass.
        # Refresh tokens are likewise only valid at /auth/token/refresh.
        if payload.get("type") != TOKEN_TYPE_ACCESS:
            logger.warning(
                "Rejected token with type=%r on an access-token path", payload.get("type")
            )
            raise credentials_exception

        # Check token revocation blacklist (FedRAMP AC-12). `issued_at` lets the
        # per-user revocation epoch invalidate a stateless access token, which has
        # no blacklist entry of its own.
        if (
            settings.TOKEN_REVOCATION_ENABLED
            and token_jti
            and token_service.is_token_revoked(
                token_jti,
                db=db,
                user_uuid=user_uuid_str,
                issued_at=payload.get("iat"),
            )
        ):
            logger.warning(f"Rejected revoked token (jti={token_jti[:8]}...)")
            raise credentials_exception

        # Validate UUID format
        try:
            user_uuid = UUID(user_uuid_str)
        except ValueError:
            raise credentials_exception from None

        token_data = TokenPayload(sub=user_uuid_str, jti=token_jti)
    except JoseError as e:
        raise credentials_exception from e

    try:
        # Look up user by UUID (indexed for performance)
        user = db.query(User).filter(User.uuid == user_uuid).first()
        if user is None:
            raise credentials_exception
        if not user.is_active:
            raise HTTPException(status_code=400, detail="Inactive user")

        # Database is the source of truth for roles - do NOT update DB from token
        # If role mismatch, the token may be stale (user should re-login)
        # This prevents privilege escalation via token manipulation
        if user_role and user.role != user_role:
            logger.warning(
                f"Role mismatch for user {user.id}: token has '{user_role}', "
                f"DB has '{user.role}'. Using DB role. User should re-login."
            )

        # Cloud contract: observability/access-log reads these off request.state.
        # org_id is None for self-hosted/local users; deps_context refines it to
        # our local Organization.id when org context applies. Access log only —
        # never a Prometheus label.
        request.state.user_id = user.id
        request.state.org_id = getattr(user, "external_org_id", None)
        return user  # type: ignore[no-any-return]
    except HTTPException:
        # FAIL CLOSED. `credentials_exception` (401, unknown user) and the 400
        # "Inactive user" raised above are *authorization decisions*, not
        # infrastructure errors — but they are HTTPExceptions, so the broad
        # handler below caught them and, under TESTING, replaced the denial with
        # a FABRICATED authenticated user. That made "unknown user" and
        # "deactivated user" both resolve to a valid session. Re-raise instead:
        # the mock-user shortcut now only covers what it was written for, an
        # unavailable database.
        raise
    except Exception as e:
        # Handle database connection errors or other issues
        logger.error(f"Error retrieving user: {e}")
        # In testing environment, we can create a mock user with the UUID from the token
        # TESTING enables auth shortcuts (here, a fabricated user). Gate on
        # is_hardened as well, so the flag can never take effect in a real
        # deployment even if it leaks into the environment (issue #284 A0.8).
        testing_environment = (
            os.environ.get("TESTING", "False").lower() == "true" and not settings.is_hardened
        )
        if testing_environment:
            logger.info(f"Creating mock user for testing with uuid {token_data.sub}")
            # For tests, create a basic user object with the UUID from the token
            user = User(
                uuid=UUID(token_data.sub),
                email="test@example.com",
                is_active=True,
                is_superuser=False,
            )
            return user  # type: ignore[no-any-return]
        # Re-raise the exception in production
        raise


def get_current_active_user(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Check that the current user is active and their account is usable.

    ``get_current_user`` answers "is this credential valid?"; this dependency
    answers "may this account act right now?" — the account-lifecycle gate lives
    here rather than there so that the credential-validating layer stays
    untouched (widening or narrowing it affects the WebSocket handshake, the
    session probe and the optional-auth path, none of which want a 403).

    ``db`` is the same session ``get_current_user`` already resolved (FastAPI
    caches a dependency per request), so the banner gate costs one extra query,
    not an extra connection.

    Raises:
        HTTPException: 400 for a deactivated account; 403 with a machine-readable
            ``detail.code`` for an unapproved or rejected account, an expired
            account, an unacknowledged login banner, or a required password change.
    """
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")

    # Before every lifecycle gate: if this session no longer belongs to the person
    # the proxy is asserting, none of the questions below are about the right user.
    _enforce_proxy_identity_consistency(current_user, request, db)
    # Approval first: an account that was never admitted has no business being
    # asked to acknowledge a banner or change a password, and the answer to all
    # three is the same screen ("wait for an administrator").
    _enforce_approval(current_user, request, db)
    # Expiry next: it has no self-service remedy, so being ALSO flagged for a
    # password change must not route the caller to a screen that cannot help.
    _enforce_account_expiry(current_user, request)
    # Then the banner: AC-8 wants consent recorded BEFORE access is granted, so
    # it precedes the password-change gate. Both have a remedy, and their exempt
    # sets differ, so a caller owing both clears them in this order.
    _enforce_banner_acknowledgment(current_user, request, db)
    _enforce_password_change(current_user, request)
    return current_user


def get_optional_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User | None:
    """Get the current user if a valid token is provided, otherwise return None.

    Checks Bearer header first, then falls back to httpOnly cookie.
    Used for endpoints that support both authenticated and unauthenticated
    access (e.g., public files can be accessed without auth, private files require auth).

    Returns:
        User object if valid token provided, None otherwise
    """
    from app.auth.cookies import get_access_token_from_cookie

    # Check for Authorization header first
    authorization = request.headers.get("Authorization")
    token: str | None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "")
    else:
        # Fall back to httpOnly cookie
        token = get_access_token_from_cookie(request)

    if not token:
        return None

    # Cloud-edition seam: offer the token to registered external verifiers
    # before the local-JWT path, mirroring get_current_user. The community
    # edition registers none, so this branch is a no-op there and behavior is
    # identical. Optional semantics are preserved: an inactive or missing
    # external user returns None rather than raising.
    from app.auth.provider_registry import has_verifiers

    if has_verifiers():
        from app.auth.external_sync import sync_external_user_to_db
        from app.auth.provider_registry import verify_external_token

        try:
            external_identity = verify_external_token(token, request)
            if external_identity is not None:
                external_user = sync_external_user_to_db(db, external_identity)
                if not external_user.is_active:
                    return None
                # Stash for resolve_org_context()/get_current_context() so org
                # scoping (e.g. thumbnail org-resolution) works for external
                # users on optional-auth routes without re-verifying the token.
                request.state.external_identity = external_identity
                request.state.user_id = external_user.id
                request.state.org_id = getattr(external_user, "external_org_id", None)
                return external_user
        except Exception as e:
            # Never let an external-auth problem fail an optional-auth route;
            # fall through to local JWT (and ultimately None).
            logger.debug(f"Error in optional external auth: {e}")

    try:
        key = OctKey.import_key(settings.JWT_SECRET_KEY)
        # Same single owner as get_current_user — see the comment there.
        token_obj = jwt.decode(token, key, algorithms=accepted_algorithms(TOKEN_TYPE_ACCESS))
        # joserfc verifies the signature/algorithm only — exp is not checked
        # automatically (unlike python-jose), so it's validated explicitly here.
        JWTClaimsRegistry(exp={"essential": True}).validate(token_obj.claims)
        payload = token_obj.claims
        user_uuid_str: str | None = payload.get("sub")
        token_jti: str | None = payload.get("jti")

        if user_uuid_str is None:
            return None

        # Same purpose binding as get_current_user — an optional-auth route must
        # not accept an MFA or refresh token either.
        if payload.get("type") != TOKEN_TYPE_ACCESS:
            return None

        # Check token revocation blacklist
        if (
            settings.TOKEN_REVOCATION_ENABLED
            and token_jti
            and token_service.is_token_revoked(token_jti, db=db, user_uuid=user_uuid_str)
        ):
            return None

        # Validate UUID format
        try:
            user_uuid = UUID(user_uuid_str)
        except ValueError:
            return None

        # Look up user by UUID
        user = db.query(User).filter(User.uuid == user_uuid).first()
        if user is None or not user.is_active:
            return None

        return user  # type: ignore[no-any-return]

    except JoseError:
        return None
    except Exception as e:
        logger.debug(f"Error in optional auth: {e}")
        return None


def get_current_admin_user(
    current_user: User = Depends(get_current_active_user),
) -> User:
    """
    Check if the current user is an admin or super_admin.

    Chains through ``get_current_active_user`` so the account-lifecycle gate
    applies here too — depending straight on ``get_current_user`` meant an admin
    flagged ``must_change_password`` (or past ``account_expires_at``) was refused
    on user routes but still had the whole admin surface.
    """
    if current_user.role not in ("admin", "super_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )
    return current_user


def get_current_active_superuser(
    current_user: User = Depends(get_current_active_user),
) -> User:
    """
    Check if the current user is a platform super_admin.

    role is the authorization source of truth; is_superuser is its derived
    mirror (role == super_admin). We gate on role so this stays consistent
    with the rest of the super_admin-tier endpoints. Chains through
    ``get_current_active_user`` for the same reason ``get_current_admin_user``
    does — the account-lifecycle gate must not have a privileged bypass.
    """
    if current_user.role != ROLE_SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions - super_admin required",
        )
    return current_user
