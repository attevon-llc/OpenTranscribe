"""Dynamic authentication settings loader with database fallback.

This module provides a dynamic settings loader that retrieves authentication
configuration from the database with automatic fallback to environment
variables when database values are not configured.

This enables the super admin UI to update authentication settings without
requiring application restarts or .env file modifications.

Two ways in, one resolution rule
--------------------------------
``get_auth_settings(db)`` is the precise form and the one to prefer: the caller
already holds a request session, so the value it returns is the one committed a
moment ago.

``get_process_auth_settings()`` exists for the enforcement points that cannot be
handed a session — ``auth/password_policy.py`` is reached from a Pydantic
validator, ``auth/lockout.py`` from module-level functions whose callers pass
only an identifier. Rewriting either signature would mean changing every call
site in ``login.py`` / ``pki.py`` / ``admin.py`` / ``users.py``. Both forms are
the same :class:`DynamicAuthSettings` and therefore the same DB > .env > coded
default rule; they differ only in where the session comes from and, for the
process-level one, in accepting up to ``AUTH_CONFIG_CACHE_SECONDS`` of staleness
in *other* processes — the same trade ``core/settings_cache.py`` already makes
for ``SystemSettings``.
"""

import logging
import math
import threading
import time
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

    @property
    def require_account_approval(self) -> bool:
        """Whether a newly provisioned account starts ``pending`` admin approval.

        Read at account-creation time only (``app/auth/approval.py``), by
        self-registration and by every external-IdP JIT path. Turning it OFF does
        not retroactively approve anyone — it stops *new* accounts being held —
        but the enforcement gate stands down with it, so an operator who changes
        their mind releases the queue rather than having to click through it. A
        **rejected** account stays refused either way: that was a deliberate
        per-account decision, not a policy setting.
        """
        return self.get_bool("require_account_approval", settings.REQUIRE_ACCOUNT_APPROVAL)

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

    # OIDC Settings Properties
    @property
    def oidc_enabled(self) -> bool:
        """Check if OIDC authentication is enabled."""
        return self.get_bool("oidc_enabled", False)

    @property
    def oidc_server_url(self) -> str:
        """Get the identity provider's public base URL."""
        return self.get_str("oidc_server_url", "")

    @property
    def oidc_realm(self) -> str:
        """Realm name, used only by the no-discovery URL fallback."""
        return self.get_str("oidc_realm", "opentranscribe")

    @property
    def oidc_client_id(self) -> str:
        """Get the OIDC client ID."""
        return self.get_str("oidc_client_id", "")

    @property
    def oidc_client_secret(self) -> str:
        """Get the OIDC client secret."""
        return self.get_str("oidc_client_secret", "")

    @property
    def oidc_use_pkce(self) -> bool:
        """Check if PKCE should be used."""
        return self.get_bool("oidc_use_pkce", True)

    @property
    def oidc_discovery_url(self) -> str:
        """OIDC ``.well-known/openid-configuration`` URL.

        Set this for any provider that does not serve the realm URL shape. Empty
        keeps the legacy realm-derived construction (issue #353).
        """
        return self.get_str("oidc_discovery_url", "")

    @property
    def oidc_issuer(self) -> str:
        """Expected ``iss`` claim. Empty means "use the discovered/realm issuer"."""
        return self.get_str("oidc_issuer", "")

    @property
    def oidc_roles_claim(self) -> str:
        """Dotted path to the claim carrying role/group names.

        Realm-shaped providers: ``realm_access.roles``. Authentik: ``groups``.
        Entra: ``roles``.
        """
        return self.get_str("oidc_roles_claim", "realm_access.roles")

    @property
    def oidc_scopes(self) -> str:
        """Space-separated scopes requested at authorization time."""
        return self.get_str("oidc_scopes", "openid email profile")

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

    @property
    def pki_mode(self) -> str:
        """How a client certificate reaches this application.

        ``header`` (default) — a reverse proxy terminates mTLS and forwards the
        certificate and/or its DN in ``PKI_CERT_HEADER`` / ``PKI_CERT_DN_HEADER``.
        Only a configured ``PKI_TRUSTED_PROXIES`` peer may assert them.

        ``mutual_tls`` — the same transport, but a bare DN assertion is refused:
        the full certificate must be forwarded so this application parses and
        validates it itself. Stricter, and the mode to use when the proxy is not
        the only thing standing between clients and the backend.

        The earlier ``direct``/``broker``/``hybrid`` vocabulary described a
        delegation choice no code ever made, and did not even overlap with the
        values the admin UI sent — so every save of the PKI tab was rejected.
        """
        return self.get_str("pki_mode", "header")

    @property
    def pki_allow_password_fallback(self) -> bool:
        """Deployment-level ceiling over the per-user ``allow_local_fallback`` flag.

        The effective permission is ``user.allow_local_fallback AND this``. The
        per-user flag stays the precise control (it is super_admin-settable, per
        account); this is the switch that turns password fallback off for the
        whole deployment without visiting every PKI account.

        Defaults True — i.e. no additional restriction — because the per-user flag
        already defaults to False. Defaulting this to False would silently revoke
        fallback from accounts a super_admin had deliberately granted it to.
        """
        return self.get_bool("pki_allow_password_fallback", True)

    @property
    def pki_trusted_proxies(self) -> str:
        """Comma-separated IPs/CIDRs permitted to assert PKI headers.

        **Empty refuses every header-sourced assertion** — same fail-closed rule
        as ``proxy_trusted_proxies``, implemented once in ``auth/header_trust.py``.
        Passed into ``pki_auth.pki_authenticate`` per login so a Settings UI save
        takes effect immediately, mirroring ``ProxyConfig.from_db``.
        """
        return self.get_str("pki_trusted_proxies", settings.PKI_TRUSTED_PROXIES)

    @property
    def pki_cert_header(self) -> str:
        """Header carrying the URL-encoded PEM client certificate."""
        return self.get_str("pki_cert_header", settings.PKI_CERT_HEADER)

    @property
    def pki_cert_dn_header(self) -> str:
        """Header carrying the certificate's Distinguished Name."""
        return self.get_str("pki_cert_dn_header", settings.PKI_CERT_DN_HEADER)

    # Trusted-header (reverse-proxy) Settings Properties
    #
    # Read by ``auth/proxy/config.py:ProxyConfig.from_db`` (per login) and by the
    # per-request consistency check in ``api/endpoints/auth/dependencies.py``, which
    # goes through the process-level layer because it holds no spare query budget.
    @property
    def proxy_enabled(self) -> bool:
        """Whether an authenticating reverse proxy may assert identities here."""
        return self.get_bool("proxy_enabled", settings.PROXY_ENABLED)

    @property
    def proxy_trusted_proxies(self) -> str:
        """Comma-separated IPs/CIDRs permitted to assert identity headers.

        **Empty refuses every header-sourced assertion.** Not "trust everyone", not
        "warn and continue" — the same fail-closed rule ``PKI_TRUSTED_PROXIES`` has,
        implemented once in ``auth/header_trust.py``.
        """
        return self.get_str("proxy_trusted_proxies", settings.PROXY_TRUSTED_PROXIES)

    @property
    def proxy_email_header(self) -> str:
        """Header carrying the authenticated user's email address."""
        return self.get_str("proxy_email_header", settings.PROXY_EMAIL_HEADER)

    @property
    def proxy_name_header(self) -> str:
        """Header carrying a display name for a just-in-time created account."""
        return self.get_str("proxy_name_header", settings.PROXY_NAME_HEADER)

    @property
    def proxy_groups_header(self) -> str:
        """Header carrying group names. Empty means the proxy manages no groups."""
        return self.get_str("proxy_groups_header", settings.PROXY_GROUPS_HEADER)

    @property
    def proxy_groups_separator(self) -> str:
        """Separator inside the groups header — ``,`` unless the values are DNs."""
        return self.get_str("proxy_groups_separator", settings.PROXY_GROUPS_SEPARATOR)

    @property
    def proxy_role_header(self) -> str:
        """Header naming ``user``/``admin``. Empty = header-driven privilege is OFF.

        Off by default deliberately: this is privilege granted over HTTP, and
        ``super_admin`` is unreachable through it under any configuration.
        """
        return self.get_str("proxy_role_header", settings.PROXY_ROLE_HEADER)

    @property
    def proxy_shared_secret(self) -> str:
        """Optional secret the proxy must present, compared in constant time."""
        return self.get_str("proxy_shared_secret", settings.PROXY_SHARED_SECRET)

    @property
    def proxy_allowed_domains(self) -> str:
        """Email-domain admission list. Empty admits every domain."""
        return self.get_str("proxy_allowed_domains", settings.PROXY_ALLOWED_DOMAINS)

    @property
    def proxy_jit_provisioning(self) -> bool:
        """Whether an unknown asserted identity may be created on the fly."""
        return self.get_bool("proxy_jit_provisioning", settings.PROXY_JIT_PROVISIONING)

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

    @property
    def mfa_backup_code_count(self) -> int:
        """How many one-time backup codes an enrolment hands out."""
        return self.get_int("mfa_backup_code_count", 10)

    @property
    def mfa_token_expire_minutes(self) -> int:
        """Lifetime of the MFA half-token minted between password and second factor."""
        return self.get_int("mfa_token_expire_minutes", 5)

    # Session Settings Properties
    #
    # A session IS a ``refresh_token`` row (``app/models/refresh_token.py``). The
    # three settings below are enforced against those rows —
    # ``token_service.verify_refresh_token`` for the two timeouts,
    # ``api/endpoints/auth/login.py`` for the concurrency limit — so changing them
    # in the admin UI takes effect on the next refresh with no restart.
    #
    # ``jwt_access_token_expire_minutes`` / ``jwt_refresh_token_expire_days`` are
    # the exception and are marked ``requires_restart``: ``auth/cookies.py``
    # computes ``ACCESS_MAX_AGE`` / ``REFRESH_MAX_AGE`` from them **at import
    # time**, so a live change would leave cookie lifetimes disagreeing with token
    # lifetimes. They are read here for display only.
    @property
    def jwt_access_token_expire_minutes(self) -> int:
        """Access-token lifetime in minutes. Restart-required — see above."""
        return self.get_int("jwt_access_token_expire_minutes", 60)

    @property
    def jwt_refresh_token_expire_days(self) -> int:
        """Refresh-token lifetime in days. Restart-required — see above."""
        return self.get_int("jwt_refresh_token_expire_days", 7)

    @property
    def session_idle_timeout_minutes(self) -> int:
        """Minutes a session may go without refreshing before it is refused.

        Checked when a refresh token is presented, so the granularity is one
        access-token lifetime rather than one request — a deliberate choice, since
        per-request tracking would be reset continuously by polling and WebSocket
        keepalives and the control would never fire. 0 (reachable only via ``.env``)
        disables it.
        """
        return self.get_int("session_idle_timeout_minutes", 15)

    @property
    def session_absolute_timeout_minutes(self) -> int:
        """Maximum total lifetime of a session, regardless of activity.

        Stamped into ``refresh_token.absolute_expires_at`` when the session is
        established and carried forward through every rotation. This had no
        reader at all before: rotation issued a fresh expiry each time, so a
        continuously-active client never had to re-authenticate.
        """
        return self.get_int("session_absolute_timeout_minutes", 480)

    @property
    def max_concurrent_sessions(self) -> int:
        """Maximum simultaneous sessions per user. 0 = unlimited."""
        return self.get_int("max_concurrent_sessions", 5)

    @property
    def concurrent_session_policy(self) -> str:
        """What to do at the limit: ``terminate_oldest`` or ``reject``.

        These two strings are the ones the login path compares against; any other
        value falls through both branches and silently enforces nothing.
        """
        return self.get_str("concurrent_session_policy", "terminate_oldest")

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
    def password_require_uppercase(self) -> bool:
        """Whether a password must contain an upper-case letter."""
        return self.get_bool("password_require_uppercase", True)

    @property
    def password_require_lowercase(self) -> bool:
        """Whether a password must contain a lower-case letter."""
        return self.get_bool("password_require_lowercase", True)

    @property
    def password_require_digit(self) -> bool:
        """Whether a password must contain a digit."""
        return self.get_bool("password_require_digit", True)

    @property
    def password_require_special(self) -> bool:
        """Whether a password must contain a special character."""
        return self.get_bool("password_require_special", True)

    @property
    def password_history_count(self) -> int:
        """Get number of passwords to remember for reuse prevention."""
        return self.get_int("password_history_count", 24)

    @property
    def password_max_age_days(self) -> int:
        """Days before a password is treated as expired. 0 = never expires."""
        return self.get_int("password_max_age_days", 60)

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

    @property
    def account_lockout_progressive(self) -> bool:
        """Whether repeat lockouts double (then quadruple) the base duration."""
        return self.get_bool("account_lockout_progressive", True)

    @property
    def account_lockout_max_duration_minutes(self) -> int:
        """Ceiling on any single lockout, and the basis for the record's TTL."""
        return self.get_int("account_lockout_max_duration_minutes", 1440)


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


#: How long a process-level read stays cached. Matches ``SETTINGS_CACHE_TTL``'s
#: default and accepts the same cross-process staleness: a write in the API
#: process is instant there (``AuthConfigService.set_config`` primes this cache)
#: and reaches a Celery worker within the TTL.
AUTH_CONFIG_CACHE_SECONDS = 30.0

_process_cache: dict[str, Any] = {}
_process_cache_expiry: float = 0.0
_process_cache_lock = threading.Lock()


def _process_cache_ttl() -> float:
    """Seconds a process-level cache generation lives.

    Infinite under ``TESTING``: the unit suite runs inside savepoint-rolled-back
    transactions, so the only writer is an explicit prime from the test's own
    session (see :func:`prime_process_auth_settings`). Letting a generation age
    out mid-test would silently discard that prime and the test would start
    reading ``.env`` again. Isolation between tests comes from the autouse
    ``_clear_process_auth_cache`` fixture instead.
    """
    import os

    if os.environ.get("TESTING", "").lower() == "true":
        return math.inf
    return AUTH_CONFIG_CACHE_SECONDS


class _ProcessAuthSettings(DynamicAuthSettings):
    """Layered auth settings for call sites that hold no request session.

    Same DB > .env > coded-default rule as :class:`DynamicAuthSettings`; the
    session is opened here, per cache generation, instead of being passed in.
    Every failure mode falls back to the ``.env`` value rather than raising: a
    database hiccup must not be able to turn a login into a 500.
    """

    def __init__(self) -> None:
        # The base-class per-instance cache is bypassed — the module-level one is
        # shared by every caller and is what ``set_config`` primes.
        super().__init__(db=None, enable_cache=False)

    def get(self, key: str, default: Any = None) -> Any:
        """Return the effective value of *key*, cached process-wide.

        Args:
            key: Configuration key (lowercase, e.g. ``password_min_length``).
            default: Returned when neither the database nor ``.env`` has a value.

        Returns:
            The effective configuration value.
        """
        cached = _cache_lookup(key)
        if cached is not None:
            return cached

        db_value = _read_effective_from_own_session(key)
        if db_value is not None:
            publish_process_auth_setting(key, db_value)
            return db_value

        return super().get(key, default)


def _cache_lookup(key: str) -> Any:
    """Read *key* from the current cache generation, expiring a stale one."""
    global _process_cache_expiry

    with _process_cache_lock:
        if time.monotonic() >= _process_cache_expiry:
            _process_cache.clear()
            _process_cache_expiry = time.monotonic() + _process_cache_ttl()
            return None
        return _process_cache.get(key)


def publish_process_auth_setting(key: str, value: Any) -> None:
    """Publish an already-resolved effective value to the whole process.

    The single writer for the process-level cache: the database read path, the
    post-commit prime, and any caller that has resolved the effective value some
    other way all land here, so there is one place that decides what "current
    generation" means.

    ``None`` removes the key rather than caching it, because ``None`` is how the
    resolver spells "no value here — fall through to ``.env``".

    Args:
        key: Configuration key (lowercase).
        value: The effective value, already type-converted, or ``None`` to drop it.
    """
    global _process_cache_expiry

    with _process_cache_lock:
        if time.monotonic() >= _process_cache_expiry:
            _process_cache.clear()
            _process_cache_expiry = time.monotonic() + _process_cache_ttl()
        if value is None:
            _process_cache.pop(key, None)
        else:
            _process_cache[key] = value


def _read_effective_from_own_session(key: str) -> Any:
    """Resolve *key* against the database using a short-lived session.

    Returns ``None`` — meaning "fall back to ``.env``" — under ``TESTING`` and on
    any database error. Under ``TESTING`` a fresh session sits outside the
    suite's savepoint and would never see the row the test just wrote, so the
    honest answer is "no database value here".
    """
    import os

    if os.environ.get("TESTING", "").lower() == "true":
        return None

    try:
        from app.db.base import SessionLocal
        from app.services.auth_config_service import AuthConfigService

        db = SessionLocal()
        try:
            return AuthConfigService.get_effective_config(db, key)
        finally:
            db.close()
    except Exception:
        logger.warning(
            "Could not resolve auth config '%s' from the database; using the .env value.",
            key,
            exc_info=True,
        )
        return None


_process_auth_settings: _ProcessAuthSettings | None = None


def get_process_auth_settings() -> _ProcessAuthSettings:
    """Get the process-wide auth settings used where no session is available.

    Returns:
        The shared :class:`_ProcessAuthSettings` singleton.
    """
    global _process_auth_settings
    if _process_auth_settings is None:
        _process_auth_settings = _ProcessAuthSettings()
    return _process_auth_settings


def prime_process_auth_settings(db: Session, key: str) -> None:
    """Publish a freshly written value into the process-wide cache.

    Called by :meth:`AuthConfigService.set_config` right after the commit, so the
    process that accepted the admin's change enforces it on the very next
    request rather than up to a TTL later. It is also what makes the change
    visible to savepoint-isolated tests, whose rows no other session can see.

    Args:
        db: The session that wrote (and committed) the value.
        key: Configuration key that changed.
    """
    try:
        from app.services.auth_config_service import AuthConfigService

        value = AuthConfigService.get_effective_config(db, key)
    except Exception:
        logger.warning("Could not prime auth config '%s'; dropping the cache.", key, exc_info=True)
        clear_process_auth_settings_cache()
        return

    publish_process_auth_setting(key, value)


def clear_process_auth_settings_cache() -> None:
    """Drop every process-level value so the next read re-resolves it."""
    global _process_cache_expiry

    with _process_cache_lock:
        _process_cache.clear()
        _process_cache_expiry = 0.0
