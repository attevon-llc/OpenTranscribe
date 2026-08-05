"""FastAPI authentication dependencies (the DI surface for every router).

``get_current_active_user`` and friends are imported by ~30 endpoint
modules; this package deliberately has no ``deps.py``. Nothing here
registers a route, so importing it can never cycle back through the
package ``__init__``.
"""

import logging
import os
from uuid import UUID

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from jose import jwt
from sqlalchemy.orm import Session

from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.token_service import token_service
from app.core.config import settings
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import TokenPayload

logger = logging.getLogger(__name__)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_PREFIX}/auth/token", auto_error=False)


def _get_client_info(request: Request) -> tuple[str, str]:
    """Extract client IP and user agent from request.

    Args:
        request: FastAPI request object

    Returns:
        Tuple of (client_ip, user_agent)
    """
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
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_uuid_str: str = payload.get("sub")  # UUID string from token
        user_role: str = payload.get("role")  # Extract role from token
        token_jti: str = payload.get("jti")  # JWT ID for revocation checking
        if user_uuid_str is None:
            raise credentials_exception

        # Check token revocation blacklist (FedRAMP AC-12)
        if (
            settings.TOKEN_REVOCATION_ENABLED
            and token_jti
            and token_service.is_token_revoked(token_jti)
        ):
            logger.warning(f"Rejected revoked token (jti={token_jti[:8]}...)")
            raise credentials_exception

        # Validate UUID format
        try:
            user_uuid = UUID(user_uuid_str)
        except ValueError:
            raise credentials_exception from None

        token_data = TokenPayload(sub=user_uuid_str, jti=token_jti)
    except JWTError as e:
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
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Check if the current user is active
    """
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
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
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_uuid_str: str = payload.get("sub")
        token_jti: str = payload.get("jti")

        if user_uuid_str is None:
            return None

        # Check token revocation blacklist
        if (
            settings.TOKEN_REVOCATION_ENABLED
            and token_jti
            and token_service.is_token_revoked(token_jti)
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

    except JWTError:
        return None
    except Exception as e:
        logger.debug(f"Error in optional auth: {e}")
        return None


def get_current_admin_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Check if the current user is an admin or super_admin
    """
    if current_user.role not in ("admin", "super_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )
    return current_user


def get_current_active_superuser(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Check if the current user is a platform super_admin.

    role is the authorization source of truth; is_superuser is its derived
    mirror (role == super_admin). We gate on role so this stays consistent
    with the rest of the super_admin-tier endpoints.
    """
    if current_user.role != ROLE_SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions - super_admin required",
        )
    return current_user
