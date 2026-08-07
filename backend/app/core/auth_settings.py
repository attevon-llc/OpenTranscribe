"""Dynamic authentication settings loader with database fallback.

This module provides a dynamic settings loader that retrieves authentication
configuration from the database with automatic fallback to environment
variables when database values are not configured.

This enables the super admin UI to update authentication settings without
requiring application restarts or .env file modifications.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)


class DynamicAuthSettings:
    """Dynamic auth settings loader with fallback to .env.

    Provides a unified interface for accessing authentication configuration
    that checks the database first and falls back to environment variables.

    This enables runtime configuration changes via the super admin UI
    while maintaining backward compatibility with .env-based configuration.

    Attributes:
        _db: Optional database session for fetching config from database
        _cache: In-memory cache for configuration values
        _cache_enabled: Whether caching is enabled
    """

    def __init__(self, db: Session | None = None, enable_cache: bool = True):
        """Initialize the dynamic settings loader.

        Args:
            db: Optional database session for fetching config from database
            enable_cache: Whether to cache values in memory (default True)
        """
        self._db = db
        self._cache: dict[str, Any] = {}
        self._cache_enabled = enable_cache

    def get(self, key: str, default: Any = None) -> Any:
        """Get config value with precedence: cache > database > .env > default.

        Args:
            key: Configuration key (lowercase, e.g., 'ldap_enabled')
            default: Default value if not found anywhere

        Returns:
            Configuration value from the highest precedence source
        """
        # Check cache first
        if self._cache_enabled and key in self._cache:
            return self._cache[key]

        # Try database
        if self._db:
            try:
                from app.services.auth_config_service import AuthConfigService

                db_value = AuthConfigService.get_effective_config(self._db, key)
                if db_value is not None:
                    if self._cache_enabled:
                        self._cache[key] = db_value
                    return db_value
            except Exception as e:
                logger.warning(f"Failed to get config '{key}' from database: {e}")

        # Fall back to environment/settings
        env_key = key.upper()
        env_value = getattr(settings, env_key, None)
        if env_value is not None:
            if self._cache_enabled:
                self._cache[key] = env_value
            return env_value

        return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get config value as boolean.

        Args:
            key: Configuration key
            default: Default boolean value

        Returns:
            Configuration value as boolean
        """
        value = self.get(key, default)

        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)

    def get_int(self, key: str, default: int = 0) -> int:
        """Get config value as integer.

        Args:
            key: Configuration key
            default: Default integer value

        Returns:
            Configuration value as integer
        """
        value = self.get(key, default)

        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def get_str(self, key: str, default: str = "") -> str:
        """Get config value as string.

        Args:
            key: Configuration key
            default: Default string value

        Returns:
            Configuration value as string
        """
        value = self.get(key, default)
        return str(value) if value is not None else default

    def clear_cache(self) -> None:
        """Clear the settings cache.

        Call this after making configuration changes to ensure
        fresh values are loaded on next access.
        """
        self._cache.clear()

    def refresh(self, key: str | None = None) -> None:
        """Refresh cached value(s) from database.

        Args:
            key: Specific key to refresh, or None to clear entire cache
        """
        if key:
            self._cache.pop(key, None)
        else:
            self._cache.clear()

    # Local (password) Settings Properties
    @property
    def local_enabled(self) -> bool:
        """Whether local password accounts may authenticate.

        The deployment-level identity-source switch. Turn it off when an external
        IdP owns identity and no one should be able to sign in with a password
        stored here. It deliberately does **not** disable the username/password
        form outright — LDAP shares that form — and it never applies to an active
        ``super_admin``, which is the documented break-glass account.
        """
        return self.get_bool("local_enabled", settings.LOCAL_AUTH_ENABLED)

    @property
    def allow_registration(self) -> bool:
        """Whether anyone may create their own account via ``POST /auth/register``.

        This toggle already existed in the admin UI and was wired to nothing: the
        endpoint read the ``ALLOW_OPEN_REGISTRATION`` env var instead, and that env
        var was missing from ``ENV_TO_CONFIG_MAPPING``, so flipping the switch did
        nothing at all. Reported by a deployment running LDAP where users could
        still self-register.
        """
        return self.get_bool("allow_registration", settings.ALLOW_OPEN_REGISTRATION)

    # LDAP Settings Properties
    @property
    def ldap_enabled(self) -> bool:
        """Check if LDAP authentication is enabled."""
        return self.get_bool("ldap_enabled", False)

    @property
    def ldap_server(self) -> str:
        """Get LDAP server address."""
        return self.get_str("ldap_server", "")

    @property
    def ldap_port(self) -> int:
        """Get LDAP server port."""
        return self.get_int("ldap_port", 636)

    @property
    def ldap_use_ssl(self) -> bool:
        """Check if LDAP should use SSL."""
        return self.get_bool("ldap_use_ssl", True)

    @property
    def ldap_use_tls(self) -> bool:
        """Check if LDAP should use StartTLS."""
        return self.get_bool("ldap_use_tls", False)

    @property
    def ldap_bind_dn(self) -> str:
        """Get LDAP bind DN."""
        return self.get_str("ldap_bind_dn", "")

    @property
    def ldap_bind_password(self) -> str:
        """Get LDAP bind password."""
        return self.get_str("ldap_bind_password", "")

    @property
    def ldap_search_base(self) -> str:
        """Get LDAP search base DN."""
        return self.get_str("ldap_search_base", "")

    @property
    def ldap_timeout(self) -> int:
        """Get LDAP connection timeout in seconds."""
        return self.get_int("ldap_timeout", 10)

    # Keycloak Settings Properties
    @property
    def keycloak_enabled(self) -> bool:
        """Check if Keycloak authentication is enabled."""
        return self.get_bool("keycloak_enabled", False)

    @property
    def keycloak_server_url(self) -> str:
        """Get Keycloak server URL."""
        return self.get_str("keycloak_server_url", "")

    @property
    def keycloak_realm(self) -> str:
        """Get Keycloak realm name."""
        return self.get_str("keycloak_realm", "opentranscribe")

    @property
    def keycloak_client_id(self) -> str:
        """Get Keycloak client ID."""
        return self.get_str("keycloak_client_id", "")

    @property
    def keycloak_client_secret(self) -> str:
        """Get Keycloak client secret."""
        return self.get_str("keycloak_client_secret", "")

    @property
    def keycloak_use_pkce(self) -> bool:
        """Check if PKCE should be used for Keycloak."""
        return self.get_bool("keycloak_use_pkce", True)

    @property
    def keycloak_discovery_url(self) -> str:
        """OIDC ``.well-known/openid-configuration`` URL.

        Set this for any provider that is not Keycloak. Empty keeps the legacy
        realm-derived URL construction, which only Keycloak serves (issue #353).
        """
        return self.get_str("keycloak_discovery_url", "")

    @property
    def keycloak_issuer(self) -> str:
        """Expected ``iss`` claim. Empty means "use the discovered/realm issuer"."""
        return self.get_str("keycloak_issuer", "")

    @property
    def keycloak_roles_claim(self) -> str:
        """Dotted path to the claim carrying role/group names.

        Keycloak: ``realm_access.roles``. Authentik: ``groups``. Entra: ``roles``.
        """
        return self.get_str("keycloak_roles_claim", "realm_access.roles")

    @property
    def keycloak_scopes(self) -> str:
        """Space-separated scopes requested at authorization time."""
        return self.get_str("keycloak_scopes", "openid email profile")

    # PKI Settings Properties
    @property
    def pki_enabled(self) -> bool:
        """Check if PKI authentication is enabled."""
        return self.get_bool("pki_enabled", False)

    @property
    def pki_verify_revocation(self) -> bool:
        """Check if certificate revocation should be verified."""
        return self.get_bool("pki_verify_revocation", False)

    @property
    def pki_admin_dns(self) -> str:
        """Semicolon-separated certificate DNs granted admin on PKI login."""
        return self.get_str("pki_admin_dns", "")

    # MFA Settings Properties
    @property
    def mfa_enabled(self) -> bool:
        """Check if MFA is enabled."""
        return self.get_bool("mfa_enabled", False)

    @property
    def mfa_required(self) -> bool:
        """Check if MFA is required for all users."""
        return self.get_bool("mfa_required", False)

    @property
    def mfa_issuer_name(self) -> str:
        """Get MFA issuer name for authenticator apps."""
        return self.get_str("mfa_issuer_name", "OpenTranscribe")

    # Session Settings Properties
    @property
    def jwt_access_token_expire_minutes(self) -> int:
        """Get JWT access token expiration in minutes."""
        return self.get_int("jwt_access_token_expire_minutes", 60)

    @property
    def session_idle_timeout_minutes(self) -> int:
        """Get session idle timeout in minutes."""
        return self.get_int("session_idle_timeout_minutes", 15)

    @property
    def max_concurrent_sessions(self) -> int:
        """Get maximum concurrent sessions per user."""
        return self.get_int("max_concurrent_sessions", 5)

    # Password Policy Properties
    @property
    def password_policy_enabled(self) -> bool:
        """Check if password policy is enabled."""
        return self.get_bool("password_policy_enabled", True)

    @property
    def password_min_length(self) -> int:
        """Get minimum password length."""
        return self.get_int("password_min_length", 12)

    @property
    def password_history_count(self) -> int:
        """Get number of passwords to remember for reuse prevention."""
        return self.get_int("password_history_count", 24)

    # Login Banner Properties
    @property
    def login_banner_enabled(self) -> bool:
        """Check if login banner is enabled."""
        return self.get_bool("login_banner_enabled", False)

    @property
    def login_banner_text(self) -> str:
        """Get login banner text."""
        return self.get_str("login_banner_text", "")

    @property
    def login_banner_classification(self) -> str:
        """Get login banner classification level."""
        return self.get_str("login_banner_classification", "UNCLASSIFIED")

    # Account Lockout Properties
    @property
    def account_lockout_enabled(self) -> bool:
        """Check if account lockout is enabled."""
        return self.get_bool("account_lockout_enabled", True)

    @property
    def account_lockout_threshold(self) -> int:
        """Get number of failed attempts before lockout."""
        return self.get_int("account_lockout_threshold", 5)

    @property
    def account_lockout_duration_minutes(self) -> int:
        """Get initial lockout duration in minutes."""
        return self.get_int("account_lockout_duration_minutes", 15)


def get_auth_settings(db: Session) -> DynamicAuthSettings:
    """Get dynamic auth settings instance with database session.

    Factory function to create a DynamicAuthSettings instance with
    the provided database session.

    Args:
        db: Database session for fetching config

    Returns:
        Configured DynamicAuthSettings instance
    """
    return DynamicAuthSettings(db)


# Global instance for cases where database is not available
# Uses only environment variables
_static_auth_settings: DynamicAuthSettings | None = None


def get_static_auth_settings() -> DynamicAuthSettings:
    """Get static auth settings instance without database.

    Returns a singleton instance that only uses environment variables.
    Useful for startup scenarios where database is not yet available.

    Returns:
        DynamicAuthSettings instance using only .env values
    """
    global _static_auth_settings
    if _static_auth_settings is None:
        _static_auth_settings = DynamicAuthSettings(db=None, enable_cache=True)
    return _static_auth_settings


def clear_static_auth_settings_cache() -> None:
    """Clear the static auth settings cache.

    Call this when configuration changes to ensure fresh values.
    """
    global _static_auth_settings
    if _static_auth_settings is not None:
        _static_auth_settings.clear_cache()
