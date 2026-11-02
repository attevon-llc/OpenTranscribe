"""FastAPI authentication dependencies (the DI surface for every router).

``get_current_active_user`` and friends are imported by ~30 endpoint
modules; this package deliberately has no ``deps.py``. Nothing here
registers a route, so importing it can never cycle back through the
package ``__init__``.

The account-lifecycle gates live in ``lifecycle_gates`` and the external-token /
proxy-identity checks in ``external_token``; both are imported into this module's
globals and called from here.
"""

import logging
import os
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

from app.api.endpoints.auth.external_token import _authenticate_external_token
from app.api.endpoints.auth.external_token import _enforce_proxy_identity_consistency
from app.api.endpoints.auth.lifecycle_gates import _enforce_account_expiry
from app.api.endpoints.auth.lifecycle_gates import _enforce_approval
from app.api.endpoints.auth.lifecycle_gates import _enforce_banner_acknowledgment
from app.api.endpoints.auth.lifecycle_gates import _enforce_password_change
from app.api.endpoints.auth.lifecycle_gates import admitted_on_optional_auth
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.token_service import token_service
from app.core.auth_types import TOKEN_TYPE_ACCESS
from app.core.config import settings
from app.core.exceptions import DomainError
from app.core.security import accepted_algorithms
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import TokenPayload

logger = logging.getLogger(__name__)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_PREFIX}/auth/token", auto_error=False)


def _load_token_user(
    request: Request,
    db: Session,
    user_uuid: UUID,
    user_role: str | None,
    token_data: TokenPayload,
    credentials_exception: HTTPException,
) -> User:
    """Load the account a decoded access token names, failing closed on a denial."""
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
    except (HTTPException, DomainError):
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

    return _load_token_user(request, db, user_uuid, user_role, token_data, credentials_exception)


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

    A pending, rejected or expired account is anonymous here, as it is refused at
    ``get_current_active_user``; see ``lifecycle_gates.admitted_on_optional_auth``.

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
        from app.auth.external_session import external_session_expired
        from app.auth.external_sync import sync_external_user_to_db
        from app.auth.provider_registry import verify_external_token

        try:
            external_identity = verify_external_token(token, request)
            if external_identity is not None:
                if external_session_expired(external_identity):
                    return None
                external_user = sync_external_user_to_db(db, external_identity)
                if not external_user.is_active or not admitted_on_optional_auth(external_user, db):
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

        # Check token revocation blacklist. Same as get_current_user, `issued_at` is what
        # lets the per-user revocation epoch (logout-all, password reset, admin
        # termination) cancel a still-valid stateless access token.
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
            return None

        # Validate UUID format
        try:
            user_uuid = UUID(user_uuid_str)
        except ValueError:
            return None

        # Look up user by UUID
        user = db.query(User).filter(User.uuid == user_uuid).first()
        if user is None or not user.is_active or not admitted_on_optional_auth(user, db):
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
