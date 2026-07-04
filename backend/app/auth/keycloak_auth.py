"""
Keycloak/OIDC authentication module.

Handles authentication via OpenID Connect with Keycloak.
Supports PKCE (RFC 7636) for OAuth 2.1 compliance.
Configuration is loaded from database first, falling back to environment variables.
"""

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import TypedDict
from urllib.parse import urlencode

import httpx
from jose import JWTError
from jose import jwt

from app.auth.constants import AUTH_TYPE_KEYCLOAK
from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.core.config import settings as env_settings

logger = logging.getLogger(__name__)


# PKCE Constants (RFC 7636)
PKCE_CODE_VERIFIER_MIN_LENGTH = 43
PKCE_CODE_VERIFIER_MAX_LENGTH = 128
PKCE_CODE_VERIFIER_LENGTH = 64  # Recommended length for security
# RFC 7636 Section 4.1: unreserved characters for code verifier
PKCE_UNRESERVED_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


@dataclass(frozen=True)
class KeycloakConfig:
    """Immutable Keycloak/OIDC configuration resolved from database or environment.

    Created once per request, passed to all helper functions.
    No global state mutation.
    """

    enabled: bool = False
    server_url: str = ""
    internal_url: str = ""
    realm: str = "opentranscribe"
    client_id: str = ""
    client_secret: str = ""
    callback_url: str = ""
    admin_role: str = "admin"
    timeout: int = 30
    use_pkce: bool = True
    verify_issuer: bool = True
    verify_audience: bool = False
    audience: str = ""

    @classmethod
    def from_env(cls) -> "KeycloakConfig":
        """Create config from environment variables only."""
        return cls(
            enabled=env_settings.KEYCLOAK_ENABLED,
            server_url=env_settings.KEYCLOAK_SERVER_URL,
            internal_url=getattr(env_settings, "KEYCLOAK_INTERNAL_URL", ""),
            realm=env_settings.KEYCLOAK_REALM,
            client_id=env_settings.KEYCLOAK_CLIENT_ID,
            client_secret=env_settings.KEYCLOAK_CLIENT_SECRET,
            callback_url=env_settings.KEYCLOAK_CALLBACK_URL,
            admin_role=env_settings.KEYCLOAK_ADMIN_ROLE,
            timeout=env_settings.KEYCLOAK_TIMEOUT,
            use_pkce=env_settings.KEYCLOAK_USE_PKCE,
            verify_issuer=getattr(env_settings, "KEYCLOAK_VERIFY_ISSUER", True),
            verify_audience=getattr(env_settings, "KEYCLOAK_VERIFY_AUDIENCE", False),
            audience=getattr(env_settings, "KEYCLOAK_AUDIENCE", ""),
        )

    @classmethod
    def from_db(cls, db) -> "KeycloakConfig":
        """Create config from database with env fallback.

        Uses DynamicAuthSettings which checks DB > .env > defaults.
        """
        from app.core.auth_settings import get_auth_settings

        auth = get_auth_settings(db)

        def _get(key: str, default):
            val = auth.get(key)
            return val if val is not None else default

        def _get_bool(key: str, default: bool) -> bool:
            val = auth.get(key)
            if val is None:
                return default
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes", "on")
            return bool(val)

        def _get_int(key: str, default: int) -> int:
            val = auth.get(key)
            if val is None:
                return default
            try:
                return int(val)
            except (ValueError, TypeError):
                return default

        return cls(
            enabled=_get_bool("keycloak_enabled", env_settings.KEYCLOAK_ENABLED),
            server_url=str(_get("keycloak_server_url", env_settings.KEYCLOAK_SERVER_URL) or ""),
            internal_url=str(
                _get("keycloak_internal_url", getattr(env_settings, "KEYCLOAK_INTERNAL_URL", ""))
                or ""
            ),
            realm=str(_get("keycloak_realm", env_settings.KEYCLOAK_REALM) or "opentranscribe"),
            client_id=str(_get("keycloak_client_id", env_settings.KEYCLOAK_CLIENT_ID) or ""),
            client_secret=str(
                _get("keycloak_client_secret", env_settings.KEYCLOAK_CLIENT_SECRET) or ""
            ),
            callback_url=str(
                _get("keycloak_callback_url", env_settings.KEYCLOAK_CALLBACK_URL) or ""
            ),
            admin_role=str(
                _get("keycloak_admin_role", env_settings.KEYCLOAK_ADMIN_ROLE) or "admin"
            ),
            timeout=_get_int("keycloak_timeout", env_settings.KEYCLOAK_TIMEOUT),
            use_pkce=_get_bool("keycloak_use_pkce", env_settings.KEYCLOAK_USE_PKCE),
            verify_issuer=_get_bool(
                "keycloak_verify_issuer", getattr(env_settings, "KEYCLOAK_VERIFY_ISSUER", True)
            ),
            verify_audience=_get_bool(
                "keycloak_verify_audience",
                getattr(env_settings, "KEYCLOAK_VERIFY_AUDIENCE", False),
            ),
            audience=str(
                _get("keycloak_audience", getattr(env_settings, "KEYCLOAK_AUDIENCE", "")) or ""
            ),
        )


def validate_pkce_code_verifier(code_verifier: str) -> bool:
    """Validate that a PKCE code verifier meets RFC 7636 requirements.

    RFC 7636 Section 4.1 specifies:
    - Length: 43-128 characters
    - Characters: Only unreserved characters (A-Z, a-z, 0-9, -, ., _, ~)
    """
    if not code_verifier:
        logger.warning("PKCE code_verifier is empty")
        return False

    if len(code_verifier) < PKCE_CODE_VERIFIER_MIN_LENGTH:
        logger.warning(
            f"PKCE code_verifier too short: {len(code_verifier)} chars "
            f"(min: {PKCE_CODE_VERIFIER_MIN_LENGTH})"
        )
        return False

    if len(code_verifier) > PKCE_CODE_VERIFIER_MAX_LENGTH:
        logger.warning(
            f"PKCE code_verifier too long: {len(code_verifier)} chars "
            f"(max: {PKCE_CODE_VERIFIER_MAX_LENGTH})"
        )
        return False

    invalid_chars = set(code_verifier) - PKCE_UNRESERVED_CHARS
    if invalid_chars:
        logger.warning(f"PKCE code_verifier contains invalid characters: {invalid_chars!r}")
        return False

    return True


class KeycloakUserData(TypedDict):
    """User data extracted from Keycloak token."""

    keycloak_id: str
    email: str
    full_name: str
    username: str
    is_admin: bool
    roles: list[str]
    cert_dn: str | None
    cert_serial: str | None
    cert_issuer: str | None
    cert_org: str | None
    cert_ou: str | None
    cert_valid_from: str | None
    cert_valid_until: str | None
    cert_fingerprint: str | None


@dataclass
class KeycloakTokens:
    """Tokens returned from Keycloak."""

    access_token: str
    refresh_token: str
    id_token: str
    expires_in: int
    token_type: str


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code verifier and code challenge pair (RFC 7636)."""
    random_bytes = secrets.token_bytes(48)
    code_verifier = base64.urlsafe_b64encode(random_bytes).rstrip(b"=").decode("ascii")
    sha256_digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(sha256_digest).rstrip(b"=").decode("ascii")

    logger.debug(
        f"Generated PKCE pair: verifier length={len(code_verifier)}, "
        f"challenge length={len(code_challenge)}"
    )
    return code_verifier, code_challenge


def _get_keycloak_urls(cfg: KeycloakConfig, internal: bool = False) -> dict:
    """Get Keycloak endpoint URLs.

    Args:
        cfg: Keycloak configuration
        internal: If True, use internal URL for backend-to-Keycloak communication.
    """
    base_url = cfg.internal_url if internal and cfg.internal_url else cfg.server_url

    base = f"{base_url}/realms/{cfg.realm}"
    return {
        "authorization": f"{cfg.server_url}/realms/{cfg.realm}/protocol/openid-connect/auth",
        "token": f"{base}/protocol/openid-connect/token",
        "userinfo": f"{base}/protocol/openid-connect/userinfo",
        "logout": f"{base}/protocol/openid-connect/logout",
        "certs": f"{base}/protocol/openid-connect/certs",
    }


def get_authorization_url(state: str, cfg: KeycloakConfig | None = None) -> tuple[str, str | None]:
    """Generate the Keycloak authorization URL for OIDC login.

    Supports PKCE (RFC 7636) for OAuth 2.1 compliance.

    Args:
        state: Random state parameter for CSRF protection
        cfg: Keycloak configuration (if None, loads from env)

    Returns:
        (authorization_url, code_verifier or None if PKCE disabled)
    """
    if cfg is None:
        cfg = KeycloakConfig.from_env()

    urls = _get_keycloak_urls(cfg)
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.callback_url,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    }

    code_verifier = None
    if cfg.use_pkce:
        code_verifier, code_challenge = generate_pkce_pair()
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
        logger.debug("PKCE enabled: added code_challenge to authorization URL")

    return f"{urls['authorization']}?{urlencode(params)}", code_verifier


async def exchange_code_for_tokens(
    code: str,
    code_verifier: str | None = None,
    cfg: KeycloakConfig | None = None,
) -> KeycloakTokens | None:
    """Exchange authorization code for tokens.

    Supports PKCE (RFC 7636) for OAuth 2.1 compliance.

    Args:
        code: Authorization code from Keycloak callback
        code_verifier: PKCE code verifier (required if PKCE was used)
        cfg: Keycloak configuration (if None, loads from env)
    """
    if cfg is None:
        cfg = KeycloakConfig.from_env()

    urls = _get_keycloak_urls(cfg, internal=True)

    token_data = {
        "grant_type": "authorization_code",
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "code": code,
        "redirect_uri": cfg.callback_url,
    }

    if code_verifier:
        if not validate_pkce_code_verifier(code_verifier):
            logger.error("PKCE code_verifier validation failed - rejecting token exchange")
            return None
        token_data["code_verifier"] = code_verifier
        logger.debug("PKCE enabled: added code_verifier to token exchange request")

    async with httpx.AsyncClient(timeout=cfg.timeout) as client:
        try:
            response = await client.post(urls["token"], data=token_data)
            response.raise_for_status()
            data = response.json()

            return KeycloakTokens(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token", ""),
                id_token=data.get("id_token", ""),
                expires_in=data.get("expires_in", 300),
                token_type=data.get("token_type", "Bearer"),
            )
        except httpx.HTTPError as e:
            logger.error(f"Failed to exchange code for tokens: {e}")
            return None


async def get_keycloak_jwks(cfg: KeycloakConfig | None = None) -> dict | None:
    """Fetch Keycloak public keys (JWKS)."""
    if cfg is None:
        cfg = KeycloakConfig.from_env()

    urls = _get_keycloak_urls(cfg, internal=True)

    async with httpx.AsyncClient(timeout=cfg.timeout) as client:
        try:
            response = await client.get(urls["certs"])
            response.raise_for_status()
            jwks: dict = response.json()
            return jwks
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch Keycloak JWKS: {e}")
            return None


async def call_keycloak_logout(
    keycloak_refresh_token: str,
    cfg: KeycloakConfig | None = None,
) -> bool:
    """Call Keycloak's logout endpoint to terminate the federated session.

    Uses the stored Keycloak refresh token to perform a back-channel logout,
    ensuring the user's Keycloak session is fully terminated when they log out
    of OpenTranscribe.

    Args:
        keycloak_refresh_token: Decrypted Keycloak refresh token.
        cfg: Keycloak configuration. If None, loads from env.

    Returns:
        True if Keycloak session was successfully terminated, False otherwise.
        Failure is non-fatal — the local session is always cleared regardless.
    """
    if cfg is None:
        cfg = KeycloakConfig.from_env()

    urls = _get_keycloak_urls(cfg, internal=True)
    logout_data = {
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "refresh_token": keycloak_refresh_token,
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(urls["logout"], data=logout_data)
            if response.status_code in (200, 204):
                logger.info("Keycloak federated logout successful")
                return True
            logger.warning(
                f"Keycloak logout returned unexpected status {response.status_code}: "
                f"{response.text[:200]}"
            )
            return False
    except httpx.TimeoutException:
        logger.warning("Keycloak logout timed out (5s) — local session still cleared")
        return False
    except httpx.HTTPError as e:
        logger.warning(f"Keycloak logout HTTP error: {e} — local session still cleared")
        return False
    except Exception as e:
        logger.error(f"Unexpected error during Keycloak logout: {type(e).__name__}: {e}")
        return False


def _extract_certificate_claims(token_claims: dict) -> dict:
    """Extract certificate claims from Keycloak OIDC token.

    When Keycloak is configured with X.509 certificate authentication,
    certificate metadata may be included in the token claims.
    """
    return {
        "cert_dn": token_claims.get("cert_dn") or token_claims.get("x509_cert_dn"),
        "cert_serial": token_claims.get("cert_serial") or token_claims.get("x509_cert_serial"),
        "cert_issuer": token_claims.get("cert_issuer") or token_claims.get("x509_cert_issuer"),
        "cert_org": token_claims.get("cert_org") or token_claims.get("x509_cert_org"),
        "cert_ou": token_claims.get("cert_ou") or token_claims.get("x509_cert_ou"),
        "cert_valid_from": token_claims.get("cert_valid_from")
        or token_claims.get("x509_cert_not_before"),
        "cert_valid_until": token_claims.get("cert_valid_until")
        or token_claims.get("x509_cert_not_after"),
        "cert_fingerprint": token_claims.get("cert_fingerprint")
        or token_claims.get("x509_cert_sha256_fingerprint"),
    }


async def validate_token(
    access_token: str, cfg: KeycloakConfig | None = None
) -> KeycloakUserData | None:
    """Validate Keycloak access token and extract user data.

    Args:
        access_token: JWT access token from Keycloak
        cfg: Keycloak configuration (if None, loads from env)
    """
    if cfg is None:
        cfg = KeycloakConfig.from_env()

    try:
        jwks = await get_keycloak_jwks(cfg)
        if not jwks:
            logger.error("Failed to fetch Keycloak JWKS for token validation")
            return None

        logger.debug(f"JWKS fetched successfully, keys count: {len(jwks.get('keys', []))}")

        jwt_options = {}
        decode_kwargs = {}

        if cfg.verify_audience:
            jwt_options["verify_aud"] = True
            audience = cfg.audience or cfg.client_id
            decode_kwargs["audience"] = audience
            logger.debug(f"Validating token audience against: {audience}")
        else:
            jwt_options["verify_aud"] = False

        if cfg.verify_issuer:
            jwt_options["verify_iss"] = True
            expected_issuer = f"{cfg.server_url}/realms/{cfg.realm}"
            decode_kwargs["issuer"] = expected_issuer
            logger.debug(f"Validating token issuer against: {expected_issuer}")
        else:
            jwt_options["verify_iss"] = False

        payload = jwt.decode(
            access_token,
            jwks,
            algorithms=["RS256"],
            options=jwt_options,
            **decode_kwargs,
        )

        logger.info(
            f"Token decoded successfully for user: {payload.get('preferred_username', 'unknown')}"
        )

        roles = payload.get("realm_access", {}).get("roles", [])
        is_admin = cfg.admin_role in roles

        cert_claims = _extract_certificate_claims(payload)

        # For government deployments where Keycloak acts as the X.509/PKI broker,
        # also honour PKI_ADMIN_DNS — a cert DN in that list grants admin regardless
        # of whether the Keycloak realm role is assigned.
        if not is_admin and cert_claims.get("cert_dn"):
            from app.auth.pki_auth import _is_pki_admin

            if _is_pki_admin(cert_claims["cert_dn"]):
                is_admin = True
                logger.info(
                    "Keycloak user promoted to admin via PKI cert DN: %s",
                    cert_claims["cert_dn"],
                )

        return KeycloakUserData(
            keycloak_id=payload["sub"],
            email=payload.get("email", ""),
            full_name=payload.get("name", ""),
            username=payload.get("preferred_username", ""),
            is_admin=is_admin,
            roles=roles,
            cert_dn=cert_claims["cert_dn"],
            cert_serial=cert_claims["cert_serial"],
            cert_issuer=cert_claims["cert_issuer"],
            cert_org=cert_claims["cert_org"],
            cert_ou=cert_claims["cert_ou"],
            cert_valid_from=cert_claims["cert_valid_from"],
            cert_valid_until=cert_claims["cert_valid_until"],
            cert_fingerprint=cert_claims["cert_fingerprint"],
        )
    except JWTError as e:
        logger.warning(f"Invalid Keycloak token (JWTError): {e}")
        return None
    except Exception as e:
        logger.error(f"Error validating Keycloak token: {type(e).__name__}: {e}")
        return None


def _create_keycloak_user(db, keycloak_data: KeycloakUserData):
    """Create a new user from Keycloak data."""
    from sqlalchemy.exc import IntegrityError

    from app.models.user import User

    keycloak_id = keycloak_data["keycloak_id"]
    email = keycloak_data["email"]

    if not email:
        email = f"{keycloak_data['username']}@keycloak.local"

    logger.info(f"Creating new user from Keycloak: {keycloak_id} ({email})")

    pki_not_before = None
    pki_not_after = None
    cert_valid_from = keycloak_data.get("cert_valid_from")
    if cert_valid_from:
        try:
            from datetime import datetime

            pki_not_before = datetime.fromisoformat(cert_valid_from)
        except ValueError:
            logger.warning(f"Invalid cert_valid_from format: {cert_valid_from}")
    cert_valid_until = keycloak_data.get("cert_valid_until")
    if cert_valid_until:
        try:
            from datetime import datetime

            pki_not_after = datetime.fromisoformat(cert_valid_until)
        except ValueError:
            logger.warning(f"Invalid cert_valid_until format: {cert_valid_until}")

    cert_fingerprint = keycloak_data.get("cert_fingerprint")
    user = User(
        email=email,
        full_name=keycloak_data["full_name"] or keycloak_data["username"] or email.split("@")[0],
        hashed_password=EXTERNAL_AUTH_NO_PASSWORD,
        auth_type=AUTH_TYPE_KEYCLOAK,
        keycloak_id=keycloak_id,
        pki_subject_dn=keycloak_data.get("cert_dn"),
        pki_serial_number=keycloak_data.get("cert_serial"),
        pki_issuer_dn=keycloak_data.get("cert_issuer"),
        pki_organization=keycloak_data.get("cert_org"),
        pki_organizational_unit=keycloak_data.get("cert_ou"),
        pki_not_before=pki_not_before,
        pki_not_after=pki_not_after,
        pki_fingerprint_sha256=cert_fingerprint.replace(":", "") if cert_fingerprint else None,
        role="admin" if keycloak_data["is_admin"] else "user",
        is_active=True,
        # External IdPs grant at most 'admin'; is_superuser mirrors super_admin.
        is_superuser=False,
    )
    db.add(user)

    try:
        db.commit()
        return user
    except IntegrityError:
        db.rollback()
        logger.info(f"User {keycloak_id} was created by concurrent request, fetching existing user")
        user = db.query(User).filter(User.keycloak_id == keycloak_id).first()
        if not user:
            user = db.query(User).filter(User.email == email).first()
        if not user:
            raise ValueError(f"Failed to create or find Keycloak user: {keycloak_id}") from None
        return user


def _update_keycloak_user(db, user, keycloak_data: KeycloakUserData):
    """Update an existing user's Keycloak data and certificate metadata."""
    keycloak_id = keycloak_data["keycloak_id"]
    email = keycloak_data["email"]

    logger.info(f"Updating existing user from Keycloak: {keycloak_id} ({email})")

    if email and email != user.email:
        logger.warning(
            f"SECURITY: User email changed during Keycloak login. "
            f"keycloak_id={keycloak_id}, old_email={user.email}, new_email={email}"
        )
        user.email = email
    if keycloak_data["full_name"]:
        user.full_name = keycloak_data["full_name"]
    user.keycloak_id = keycloak_id
    user.auth_type = AUTH_TYPE_KEYCLOAK

    # Update certificate metadata if present
    if keycloak_data.get("cert_dn"):
        user.pki_subject_dn = keycloak_data["cert_dn"]
    if keycloak_data.get("cert_serial"):
        user.pki_serial_number = keycloak_data["cert_serial"]
    if keycloak_data.get("cert_issuer"):
        user.pki_issuer_dn = keycloak_data["cert_issuer"]
    if keycloak_data.get("cert_org"):
        user.pki_organization = keycloak_data["cert_org"]
    if keycloak_data.get("cert_ou"):
        user.pki_organizational_unit = keycloak_data["cert_ou"]
    cert_valid_from = keycloak_data.get("cert_valid_from")
    if cert_valid_from:
        try:
            from datetime import datetime

            user.pki_not_before = datetime.fromisoformat(cert_valid_from)
        except ValueError:
            logger.warning(f"Invalid cert_valid_from format: {cert_valid_from}")
    cert_valid_until = keycloak_data.get("cert_valid_until")
    if cert_valid_until:
        try:
            from datetime import datetime

            user.pki_not_after = datetime.fromisoformat(cert_valid_until)
        except ValueError:
            logger.warning(f"Invalid cert_valid_until format: {cert_valid_until}")
    cert_fingerprint = keycloak_data.get("cert_fingerprint")
    if cert_fingerprint:
        user.pki_fingerprint_sha256 = cert_fingerprint.replace(":", "")

    if keycloak_data["is_admin"]:
        # External IdPs grant at most 'admin'; never demote a local super_admin.
        if user.role not in ("admin", "super_admin"):
            logger.info(f"Promoting Keycloak user {keycloak_id} to admin")
            user.role = "admin"
        user.is_superuser = user.role == "super_admin"
    elif user.role == "admin":
        logger.info(f"Demoting Keycloak user {keycloak_id} from admin")
        user.role = "user"
        user.is_superuser = False

    db.commit()
    return user


def _convert_local_user_to_keycloak(db, user, keycloak_data: KeycloakUserData):
    """Convert an existing local user to Keycloak authentication."""
    keycloak_id = keycloak_data["keycloak_id"]
    email = keycloak_data["email"]

    logger.info(f"Converting local user {user.email} to Keycloak auth: {keycloak_id}")

    user.auth_type = AUTH_TYPE_KEYCLOAK
    user.keycloak_id = keycloak_id
    user.hashed_password = EXTERNAL_AUTH_NO_PASSWORD

    if email and email != user.email:
        logger.warning(
            f"SECURITY: User email changed during Keycloak conversion. "
            f"keycloak_id={keycloak_id}, old_email={user.email}, new_email={email}"
        )
        user.email = email
    if keycloak_data["full_name"]:
        user.full_name = keycloak_data["full_name"]

    if keycloak_data["is_admin"]:
        # External IdPs grant at most 'admin'; never demote a local super_admin.
        if user.role not in ("admin", "super_admin"):
            logger.info(f"Promoting converted Keycloak user {keycloak_id} to admin")
            user.role = "admin"
        user.is_superuser = user.role == "super_admin"
    elif user.role == "admin":
        logger.info(
            f"Demoting converted Keycloak user {keycloak_id} from admin (no admin role in Keycloak)"
        )
        user.role = "user"
        user.is_superuser = False

    db.commit()
    return user


def sync_keycloak_user_to_db(db, keycloak_data: KeycloakUserData):
    """Create or update a user in the database from Keycloak data.

    Handles creating new users, updating existing Keycloak users,
    converting local users to Keycloak, and race conditions.
    """
    from app.auth.constants import AUTH_TYPE_LOCAL
    from app.models.user import User

    keycloak_id = keycloak_data["keycloak_id"]
    email = keycloak_data["email"]

    user = db.query(User).filter(User.keycloak_id == keycloak_id).first()
    if not user and email:
        user = db.query(User).filter(User.email == email).first()

    if not user:
        user = _create_keycloak_user(db, keycloak_data)
    elif user.auth_type == AUTH_TYPE_LOCAL:
        logger.warning(
            f"SECURITY: Converting local user {email} to Keycloak auth. "
            "User will now authenticate exclusively via Keycloak. "
            "Local password will be cleared."
        )
        user = _convert_local_user_to_keycloak(db, user, keycloak_data)
    else:
        user = _update_keycloak_user(db, user, keycloak_data)

    db.refresh(user)
    return user
