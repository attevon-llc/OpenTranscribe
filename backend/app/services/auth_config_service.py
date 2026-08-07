"""Service layer for authentication configuration management.

This module provides a comprehensive service for managing authentication
configuration settings stored in the database, with support for:
- Encrypted storage of sensitive values (passwords, secrets)
- Audit logging of all configuration changes
- Bulk updates by category
- Migration from environment variables to database
- Fallback to .env settings when database values not configured
"""

import json
import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.auth_config import AuthConfig
from app.models.auth_config import AuthConfigAudit
from app.schemas.auth_config import CATEGORY_SCHEMAS
from app.schemas.auth_config import CROSS_FIELD_KEYS
from app.schemas.auth_config import coded_default
from app.schemas.auth_config import validate_category_config
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key

logger = logging.getLogger(__name__)

#: Spellings accepted for a stored boolean. Anything else is a parse FAILURE, not
#: a false: ``keycloak_verify_issuer`` and friends are security controls, and
#: ``value.lower() in ("true", "1", "yes", "on")`` quietly turned a typo into
#: "validation off".
BOOL_TRUE_VALUES = frozenset({"true", "1", "yes", "on", "t", "y"})
BOOL_FALSE_VALUES = frozenset({"false", "0", "no", "off", "f", "n"})

#: Ceiling for a single audit-log page. ``?limit=10000000`` used to be honoured.
MAX_AUDIT_LOG_LIMIT = 500

#: Marker returned in place of a stored secret. It exists so the admin UI can show
#: "a value is set" without receiving the value — but it must NEVER be written back
#: as if it were one. The API used to return the literal ``***REDACTED***``, the
#: panel bound it into the password field, and clicking Save on any OTHER field in
#: the same tab re-encrypted the placeholder over the real LDAP bind password /
#: Keycloak client secret, with a success toast. Writes now reject it outright.
SENSITIVE_SET_SENTINEL = "__SECRET_IS_SET__"  # noqa: S105 # nosec B105

#: Values that mean "the user did not type a new secret" and must leave the stored
#: one untouched. Empty is the deliberate "leave blank to keep current" case;
#: the rest are placeholders older clients may echo back.
SENSITIVE_NO_CHANGE_VALUES = frozenset(
    {SENSITIVE_SET_SENTINEL, "***REDACTED***", "***ENCRYPTED***", ""}
)


class AuthConfigService:
    """Service for managing authentication configuration.

    Provides methods for getting, setting, and managing authentication
    configuration stored in the database with proper encryption for
    sensitive values and audit logging for compliance.
    """

    # Keys that contain sensitive data and should be encrypted
    SENSITIVE_KEYS = {
        "ldap_bind_password",
        "keycloak_client_secret",
    }

    # Mapping of config keys to their data types
    DATA_TYPE_MAPPING = {
        # Local auth settings
        "local_enabled": "bool",
        "allow_registration": "bool",
        "require_email_verification": "bool",
        # LDAP settings
        "ldap_enabled": "bool",
        "ldap_port": "int",
        "ldap_use_ssl": "bool",
        "ldap_use_tls": "bool",
        "ldap_timeout": "int",
        "ldap_recursive_groups": "bool",
        # Keycloak settings
        "keycloak_enabled": "bool",
        "keycloak_timeout": "int",
        "keycloak_verify_audience": "bool",
        "keycloak_use_pkce": "bool",
        "keycloak_verify_issuer": "bool",
        # PKI settings
        "pki_enabled": "bool",
        "pki_verify_revocation": "bool",
        "pki_ocsp_timeout_seconds": "int",
        "pki_crl_cache_seconds": "int",
        "pki_revocation_soft_fail": "bool",
        "pki_allow_password_fallback": "bool",
        "pki_support_cac": "bool",
        "pki_support_piv": "bool",
        "pki_mode": "string",
        # Password policy settings
        "password_policy_enabled": "bool",
        "password_min_length": "int",
        "password_require_uppercase": "bool",
        "password_require_lowercase": "bool",
        "password_require_digit": "bool",
        "password_require_special": "bool",
        "password_history_count": "int",
        "password_max_age_days": "int",
        # MFA settings
        "mfa_enabled": "bool",
        "mfa_required": "bool",
        "mfa_backup_code_count": "int",
        "mfa_token_expire_minutes": "int",
        # Session settings
        "jwt_access_token_expire_minutes": "int",
        "jwt_refresh_token_expire_days": "int",
        "session_idle_timeout_minutes": "int",
        "session_absolute_timeout_minutes": "int",
        "max_concurrent_sessions": "int",
        "concurrent_session_policy": "string",
        # Login banner settings
        "login_banner_enabled": "bool",
        # Account settings
        "account_lockout_threshold": "int",
        "account_lockout_duration_minutes": "int",
        "account_lockout_progressive": "bool",
        "account_lockout_max_duration_minutes": "int",
        "account_lockout_enabled": "bool",
        "rate_limit_auth_per_minute": "int",
        "rate_limit_enabled": "bool",
        # Local auth lockout (frontend naming)
        "max_login_attempts": "int",
        "lockout_duration_minutes": "int",
        # Frontend MFA naming
        "mfa_issuer": "string",
        # Frontend password naming
        "password_require_numbers": "bool",
    }

    #: Lazily-built reverse of ``ENV_TO_CONFIG_MAPPING`` (config key -> env var).
    _CONFIG_TO_ENV: dict[str, str] = {}

    # Environment variable to config key mapping
    ENV_TO_CONFIG_MAPPING = {
        # Local / identity source. ALLOW_OPEN_REGISTRATION was missing here, which
        # is why the admin UI's self-registration toggle did nothing: the endpoint
        # read the env var and the DB key had no env counterpart to migrate from.
        "ALLOW_OPEN_REGISTRATION": "allow_registration",
        "LOCAL_AUTH_ENABLED": "local_enabled",
        # LDAP
        "LDAP_ENABLED": "ldap_enabled",
        "LDAP_SERVER": "ldap_server",
        "LDAP_PORT": "ldap_port",
        "LDAP_USE_SSL": "ldap_use_ssl",
        "LDAP_USE_TLS": "ldap_use_tls",
        "LDAP_BIND_DN": "ldap_bind_dn",
        "LDAP_BIND_PASSWORD": "ldap_bind_password",
        "LDAP_SEARCH_BASE": "ldap_search_base",
        "LDAP_USERNAME_ATTR": "ldap_username_attr",
        "LDAP_EMAIL_ATTR": "ldap_email_attr",
        "LDAP_NAME_ATTR": "ldap_name_attr",
        "LDAP_TIMEOUT": "ldap_timeout",
        "LDAP_ADMIN_USERS": "ldap_admin_users",
        "LDAP_ADMIN_GROUPS": "ldap_admin_groups",
        "LDAP_USER_GROUPS": "ldap_user_groups",
        "LDAP_RECURSIVE_GROUPS": "ldap_recursive_groups",
        "LDAP_GROUP_ATTR": "ldap_group_attr",
        "LDAP_USER_SEARCH_FILTER": "ldap_user_search_filter",
        # Keycloak
        "KEYCLOAK_ENABLED": "keycloak_enabled",
        "KEYCLOAK_SERVER_URL": "keycloak_server_url",
        "KEYCLOAK_INTERNAL_URL": "keycloak_internal_url",
        # Generic OIDC discovery (issue #353). The OIDC_* spellings are aliases for
        # non-Keycloak deployments; KEYCLOAK_* wins when both are set, which is why
        # it is listed second here — the reverse map keeps the last writer.
        "OIDC_DISCOVERY_URL": "keycloak_discovery_url",
        "KEYCLOAK_DISCOVERY_URL": "keycloak_discovery_url",
        "OIDC_ISSUER": "keycloak_issuer",
        "KEYCLOAK_ISSUER": "keycloak_issuer",
        "KEYCLOAK_ROLES_CLAIM": "keycloak_roles_claim",
        "KEYCLOAK_SCOPES": "keycloak_scopes",
        "KEYCLOAK_REALM": "keycloak_realm",
        "KEYCLOAK_CLIENT_ID": "keycloak_client_id",
        "KEYCLOAK_CLIENT_SECRET": "keycloak_client_secret",
        "KEYCLOAK_CALLBACK_URL": "keycloak_callback_url",
        "KEYCLOAK_ADMIN_ROLE": "keycloak_admin_role",
        "KEYCLOAK_TIMEOUT": "keycloak_timeout",
        "KEYCLOAK_VERIFY_AUDIENCE": "keycloak_verify_audience",
        "KEYCLOAK_AUDIENCE": "keycloak_audience",
        "KEYCLOAK_USE_PKCE": "keycloak_use_pkce",
        "KEYCLOAK_VERIFY_ISSUER": "keycloak_verify_issuer",
        # PKI
        "PKI_ENABLED": "pki_enabled",
        "PKI_CA_CERT_PATH": "pki_ca_cert_path",
        "PKI_VERIFY_REVOCATION": "pki_verify_revocation",
        "PKI_CERT_HEADER": "pki_cert_header",
        "PKI_CERT_DN_HEADER": "pki_cert_dn_header",
        "PKI_ADMIN_DNS": "pki_admin_dns",
        "PKI_OCSP_TIMEOUT_SECONDS": "pki_ocsp_timeout_seconds",
        "PKI_CRL_CACHE_SECONDS": "pki_crl_cache_seconds",
        "PKI_REVOCATION_SOFT_FAIL": "pki_revocation_soft_fail",
        "PKI_TRUSTED_PROXIES": "pki_trusted_proxies",
        # Password policy
        "PASSWORD_POLICY_ENABLED": "password_policy_enabled",
        "PASSWORD_MIN_LENGTH": "password_min_length",
        "PASSWORD_REQUIRE_UPPERCASE": "password_require_uppercase",
        "PASSWORD_REQUIRE_LOWERCASE": "password_require_lowercase",
        "PASSWORD_REQUIRE_DIGIT": "password_require_digit",
        "PASSWORD_REQUIRE_SPECIAL": "password_require_special",
        "PASSWORD_HISTORY_COUNT": "password_history_count",
        "PASSWORD_MAX_AGE_DAYS": "password_max_age_days",
        # MFA
        "MFA_ENABLED": "mfa_enabled",
        "MFA_REQUIRED": "mfa_required",
        "MFA_ISSUER_NAME": "mfa_issuer_name",
        "MFA_BACKUP_CODE_COUNT": "mfa_backup_code_count",
        "MFA_TOKEN_EXPIRE_MINUTES": "mfa_token_expire_minutes",
        # Session
        "JWT_ACCESS_TOKEN_EXPIRE_MINUTES": "jwt_access_token_expire_minutes",
        "JWT_REFRESH_TOKEN_EXPIRE_DAYS": "jwt_refresh_token_expire_days",
        "SESSION_IDLE_TIMEOUT_MINUTES": "session_idle_timeout_minutes",
        "SESSION_ABSOLUTE_TIMEOUT_MINUTES": "session_absolute_timeout_minutes",
        "MAX_CONCURRENT_SESSIONS": "max_concurrent_sessions",
        "CONCURRENT_SESSION_POLICY": "concurrent_session_policy",
        # Login banner
        "LOGIN_BANNER_ENABLED": "login_banner_enabled",
        "LOGIN_BANNER_TEXT": "login_banner_text",
        "LOGIN_BANNER_CLASSIFICATION": "login_banner_classification",
        # Account lockout
        "ACCOUNT_LOCKOUT_THRESHOLD": "account_lockout_threshold",
        "ACCOUNT_LOCKOUT_DURATION_MINUTES": "account_lockout_duration_minutes",
        "ACCOUNT_LOCKOUT_PROGRESSIVE": "account_lockout_progressive",
        "ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES": "account_lockout_max_duration_minutes",
        "ACCOUNT_LOCKOUT_ENABLED": "account_lockout_enabled",
        # Rate limiting
        "RATE_LIMIT_AUTH_PER_MINUTE": "rate_limit_auth_per_minute",
        "RATE_LIMIT_ENABLED": "rate_limit_enabled",
    }

    #: Config keys grouped by category, derived from the per-category Pydantic
    #: models in ``app/schemas/auth_config.py``.
    #:
    #: It was a hand-maintained literal that drifted from those models and listed
    #: eight keys under TWO categories each (the password-policy and MFA keys the
    #: admin UI's Local tab also renders). ``auth_config.config_key`` is globally
    #: UNIQUE, so such a key kept whichever category wrote it first and then went
    #: missing from the other tab's ``GET /{category}``. One source of truth means
    #: the duplicate cannot come back: a key lives in exactly one model.
    CONFIG_CATEGORIES: dict[str, list[str]] = {
        category: list(model.model_fields) for category, model in CATEGORY_SCHEMAS.items()
    }

    @staticmethod
    def get_config(db: Session, key: str, decrypt: bool = True) -> str | None:
        """Get a single configuration value.

        Args:
            db: Database session
            key: Configuration key to retrieve
            decrypt: Whether to decrypt sensitive values (default True)

        Returns:
            Configuration value as string, or None if not found
        """
        config = db.query(AuthConfig).filter(AuthConfig.config_key == key).first()
        if not config:
            return None

        value: str | None = config.config_value  # type: ignore[assignment]
        if config.is_sensitive and decrypt and value:
            # Undecryptable means UNSET, never "hand back the ciphertext".
            #
            # This used to return the stored ciphertext on failure, and returned it
            # silently when decrypt_api_key merely returned falsy without raising
            # (issue #324). Callers use these values as real credentials — an LDAP
            # bind password, an OIDC client secret — so ciphertext is at best a
            # baffling auth failure and at worst an encrypted blob shipped to an
            # external IdP or rendered in the admin UI.
            #
            # Returning None makes the sole caller (`_get_effective`, which tests
            # `if db_value is not None`) fall through to the env value and then the
            # coded default: a known source instead of garbage.
            try:
                decrypted = decrypt_api_key(value)
            except Exception:
                logger.exception(
                    "Failed to decrypt sensitive auth config %s; treating it as unset. "
                    "Check ENCRYPTION_KEY — a rotated or lost key makes stored secrets "
                    "unrecoverable and they must be re-entered.",
                    key,
                )
                return None

            if not decrypted:
                logger.error(
                    "Decrypting sensitive auth config %s produced an empty value; "
                    "treating it as unset rather than returning the stored ciphertext.",
                    key,
                )
                return None

            value = decrypted

        return value

    @staticmethod
    def _convert_value(value: str | None, data_type: str, key: str | None = None) -> Any:
        """Convert a stored string value to its declared type.

        A value that does not parse falls back to *key*'s schema default, never to
        the zero value. ``value.lower() in ("true", "1", "yes", "on")`` read every
        malformed string as ``False``, so one bad character in
        ``keycloak_verify_issuer``, ``keycloak_verify_audience`` or
        ``ldap_use_ssl`` silently turned that security control OFF. Failing open
        because a string did not parse is the bug; the declared default is a known,
        safe source. Parse failures are logged so the bad row is fixable.

        Args:
            value: String value from the database.
            data_type: Target data type (string, bool, int, json).
            key: Configuration key, used to look up the schema default. Without it
                only the generic zero value is available.

        Returns:
            Converted value.
        """
        if value is None:
            generic = {"bool": False, "int": 0, "json": {}}.get(data_type)
            return coded_default(key, generic) if key else generic

        if data_type == "bool":
            lowered = value.strip().lower()
            if lowered in BOOL_TRUE_VALUES:
                return True
            if lowered in BOOL_FALSE_VALUES:
                return False
            fallback = coded_default(key, False) if key else False
            logger.error(
                "Auth config %s holds %r, which is not a boolean; using the coded default %r. "
                "A security control must not be disabled just because a value failed to parse.",
                key or "<unknown>",
                value,
                fallback,
            )
            return fallback
        elif data_type == "int":
            try:
                return int(value)
            except (ValueError, TypeError):
                fallback = coded_default(key, 0) if key else 0
                logger.error(
                    "Auth config %s holds %r, which is not an integer; using the coded default %r.",
                    key or "<unknown>",
                    value,
                    fallback,
                )
                return fallback
        elif data_type == "json":
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                logger.error(
                    "Auth config %s holds %r, which is not valid JSON; using an empty object.",
                    key or "<unknown>",
                    value,
                )
                return {}

        return value

    @staticmethod
    def get_config_by_category(db: Session, category: str, decrypt: bool = True) -> dict[str, Any]:
        """Get all configuration values for a category.

        Args:
            db: Database session
            category: Configuration category (ldap, keycloak, pki, etc.)
            decrypt: Whether to decrypt sensitive values

        Returns:
            Dictionary of configuration key-value pairs
        """
        configs = db.query(AuthConfig).filter(AuthConfig.category == category).all()
        result: dict[str, Any] = {}

        for config in configs:
            value = config.config_value  # type: ignore[assignment]

            # A sensitive value NEVER leaves this function in readable form when
            # the caller did not ask for it decrypted. The masking below used to
            # live inside the `and decrypt` branch, so the one caller that passes
            # decrypt=False (the admin GET /{category} endpoint) skipped it and
            # returned the raw CIPHERTEXT to the browser — the opposite of what
            # the endpoint's own comment claimed.
            if config.is_sensitive and not decrypt:
                result[config.config_key] = SENSITIVE_SET_SENTINEL if value else None
                continue

            if config.is_sensitive and decrypt and value:
                try:
                    decrypted = decrypt_api_key(value)  # type: ignore[call-overload]
                    if not decrypted:
                        logger.error(
                            "Decrypting sensitive auth config %s produced an empty value; "
                            "masking it. Check ENCRYPTION_KEY.",
                            config.config_key,
                        )
                    # Masked, not the ciphertext. This surface feeds the admin UI, so
                    # the placeholder is deliberate — but it must never be usable as a
                    # credential, and the failure must be visible (issue #324): both
                    # branches previously masked in silence.
                    value = decrypted or "***ENCRYPTED***"  # type: ignore[assignment]
                except Exception:
                    logger.exception(
                        "Failed to decrypt sensitive auth config %s; masking it. "
                        "Check ENCRYPTION_KEY — a rotated or lost key makes stored "
                        "secrets unrecoverable and they must be re-entered.",
                        config.config_key,
                    )
                    value = "***ENCRYPTED***"  # type: ignore[assignment]

            # Convert to appropriate type
            data_type = config.data_type or "string"
            result[config.config_key] = AuthConfigService._convert_value(  # type: ignore[index]
                value,  # type: ignore[arg-type]
                data_type,
                config.config_key,  # type: ignore[arg-type]
            )

        return result

    @staticmethod
    def set_config(
        db: Session,
        key: str,
        value: Any,
        is_sensitive: bool,
        category: str,
        user_id: int,
        request: Request | None = None,
        data_type: str | None = None,
        description: str | None = None,
    ) -> AuthConfig:
        """Set a configuration value with audit logging.

        Args:
            db: Database session
            key: Configuration key
            value: Value to set (will be converted to string)
            is_sensitive: Whether the value should be encrypted
            category: Configuration category
            user_id: ID of user making the change
            request: Optional FastAPI request for IP/user agent logging
            data_type: Optional data type (auto-detected if not provided)
            description: Optional description for the setting

        Returns:
            Updated or created AuthConfig object
        """
        # Auto-detect data type if not provided
        if data_type is None:
            data_type = AuthConfigService.DATA_TYPE_MAPPING.get(key, "string")

        # Convert value to string for storage
        if isinstance(value, bool):
            str_value = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            str_value = json.dumps(value)
        elif value is not None:
            str_value = str(value)
        else:
            str_value = None

        # Encrypt sensitive values
        encrypted_value = encrypt_api_key(str_value) if is_sensitive and str_value else str_value

        # Get existing config
        config: AuthConfig | None = (
            db.query(AuthConfig).filter(AuthConfig.config_key == key).first()
        )
        old_value = config.config_value if config else None

        if config:
            # Update existing
            config.config_value = encrypted_value  # type: ignore[assignment]
            config.data_type = data_type  # type: ignore[assignment]
            # Heal a stale category. `config_key` is globally UNIQUE and this branch
            # never rewrote `category`, so a key that used to be listed under two
            # categories stayed pinned to whichever tab wrote it first — and then
            # vanished from the other tab's GET, which filters by category.
            if config.category != category:
                logger.info(
                    "Auth config '%s' moving from category '%s' to '%s'",
                    key,
                    config.category,
                    category,
                )
                config.category = category  # type: ignore[assignment]
            config.updated_by = user_id  # type: ignore[assignment]
            config.updated_at = datetime.now(UTC)  # type: ignore[assignment]
            if description is not None:
                config.description = description  # type: ignore[assignment]
            change_type = "update"
        else:
            # Create new
            config = AuthConfig(
                config_key=key,
                config_value=encrypted_value,
                is_sensitive=is_sensitive,
                category=category,
                data_type=data_type,
                description=description,
                created_by=user_id,
                updated_by=user_id,
            )
            db.add(config)
            change_type = "create"

        # Create audit log
        audit = AuthConfigAudit(
            config_key=key,
            old_value="***REDACTED***" if is_sensitive else old_value,
            new_value="***REDACTED***" if is_sensitive else str_value,
            changed_by=user_id,
            change_type=change_type,
            ip_address=request.client.host if request and request.client else None,
            user_agent=(
                request.headers.get("user-agent", "")[:512] if request and request.headers else None
            ),
        )
        db.add(audit)

        db.commit()
        db.refresh(config)

        logger.info(
            f"Auth config '{key}' {change_type}d by user {user_id} "
            f"(category={category}, sensitive={is_sensitive})"
        )

        return config

    @staticmethod
    def delete_config(
        db: Session,
        key: str,
        user_id: int,
        request: Request | None = None,
    ) -> bool:
        """Delete a configuration value with audit logging.

        Args:
            db: Database session
            key: Configuration key to delete
            user_id: ID of user making the change
            request: Optional FastAPI request for IP/user agent logging

        Returns:
            True if deleted, False if not found
        """
        config = db.query(AuthConfig).filter(AuthConfig.config_key == key).first()
        if not config:
            return False

        old_value = config.config_value
        is_sensitive = config.is_sensitive

        # Create audit log
        audit = AuthConfigAudit(
            config_key=key,
            old_value="***REDACTED***" if is_sensitive else old_value,
            new_value=None,
            changed_by=user_id,
            change_type="delete",
            ip_address=request.client.host if request and request.client else None,
            user_agent=(
                request.headers.get("user-agent", "")[:512] if request and request.headers else None
            ),
        )
        db.add(audit)

        db.delete(config)
        db.commit()

        logger.info(f"Auth config '{key}' deleted by user {user_id}")
        return True

    @staticmethod
    def bulk_update_category(
        db: Session,
        category: str,
        config_dict: dict[str, Any],
        user_id: int,
        request: Request | None = None,
    ) -> dict[str, AuthConfig]:
        """Update multiple configuration values for a category.

        Every key is checked against the category's schema before anything is
        written: unknown keys, unparseable values, out-of-range numbers and
        rejected combinations all fail here rather than being stored verbatim.

        Args:
            db: Database session
            category: Configuration category
            config_dict: Dictionary of key-value pairs to update
            user_id: ID of user making the changes
            request: Optional FastAPI request for IP/user agent logging

        Returns:
            Dictionary of updated AuthConfig objects

        Raises:
            ValueError: The payload is invalid. The HTTP layer turns this into a
                400 naming the offending keys.
        """
        config_dict = validate_category_config(
            category,
            config_dict,
            current=AuthConfigService._cross_field_state(db, category, config_dict),
        )

        results: dict[str, AuthConfig] = {}

        for key, value in config_dict.items():
            is_sensitive = key in AuthConfigService.SENSITIVE_KEYS

            # Leave the stored secret alone unless the caller actually typed a new
            # one. Skipping only None/"" was not enough: the read path handed the
            # client a placeholder, the client submitted it back verbatim, and it
            # was encrypted over the real credential.
            if is_sensitive and (
                value is None or (isinstance(value, str) and value in SENSITIVE_NO_CHANGE_VALUES)
            ):
                continue

            config = AuthConfigService.set_config(
                db=db,
                key=key,
                value=value,
                is_sensitive=is_sensitive,
                category=category,
                user_id=user_id,
                request=request,
            )
            results[key] = config

        return results

    @staticmethod
    def _cross_field_state(
        db: Session | None, category: str, config_dict: dict[str, Any]
    ) -> dict[str, Any]:
        """Load the currently effective values a category's joint rules need.

        Only keys the payload does NOT carry are read; the payload wins for the
        rest. Without this the joint rules would only ever see one half of the
        pair and could be walked around by saving one field at a time.

        Args:
            db: Database session, or None when no DB is available.
            category: Configuration category being written.
            config_dict: The incoming payload.

        Returns:
            Currently effective values for the category's cross-field keys.
        """
        state: dict[str, Any] = {}
        for key in CROSS_FIELD_KEYS.get(category, ()):
            if key in config_dict:
                continue
            value = AuthConfigService.get_effective_config(db, key) if db is not None else None
            state[key] = coded_default(key) if value is None else value
        return state

    @staticmethod
    def get_effective_config(db: Session, key: str) -> Any:
        """Get effective config value with precedence: Database > .env > default.

        Args:
            db: Database session
            key: Configuration key

        Returns:
            Effective configuration value
        """
        # Try database first
        db_value = AuthConfigService.get_config(db, key)
        if db_value is not None:
            # Convert to appropriate type
            data_type = AuthConfigService.DATA_TYPE_MAPPING.get(key, "string")
            return AuthConfigService._convert_value(db_value, data_type, key)

        # Fall back to environment/settings
        return getattr(settings, AuthConfigService.env_var_for(key), None)

    @staticmethod
    def env_var_for(config_key: str) -> str:
        """Return the ``Settings`` attribute backing *config_key*.

        Most keys are just the upper-cased name, but a handful deliberately are
        not — ``allow_registration`` is ``ALLOW_OPEN_REGISTRATION``, for instance.
        ``ENV_TO_CONFIG_MAPPING`` recorded those pairs and nothing consulted it,
        so the mismatched keys silently resolved to ``None`` and could never be
        migrated out of the environment.
        """
        if not AuthConfigService._CONFIG_TO_ENV:
            AuthConfigService._CONFIG_TO_ENV.update(
                {v: k for k, v in AuthConfigService.ENV_TO_CONFIG_MAPPING.items()}
            )
        return AuthConfigService._CONFIG_TO_ENV.get(config_key, config_key.upper())

    @staticmethod
    def get_audit_log(
        db: Session,
        category: str | None = None,
        config_key: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuthConfigAudit]:
        """Get audit log entries for configuration changes.

        Args:
            db: Database session
            category: Optional category filter
            config_key: Optional specific key filter
            limit: Maximum number of entries to return (clamped to
                ``MAX_AUDIT_LOG_LIMIT``)
            offset: Number of entries to skip

        Returns:
            List of audit log entries

        Raises:
            ValueError: *category* is not a known category. It used to fall through
                ``CONFIG_CATEGORIES.get(category, [])`` to an EMPTY key list, which
                skipped the filter entirely — so ``/audit/anything`` returned the
                whole unfiltered audit log for every category at once.
        """
        query = db.query(AuthConfigAudit)

        if config_key:
            query = query.filter(AuthConfigAudit.config_key == config_key)
        elif category:
            category_keys = AuthConfigService.CONFIG_CATEGORIES.get(category)
            if category_keys is None:
                raise ValueError(
                    f"Unknown configuration category '{category}'. "
                    f"Must be one of: {', '.join(AuthConfigService.CONFIG_CATEGORIES)}"
                )
            query = query.filter(AuthConfigAudit.config_key.in_(category_keys))

        limit = max(1, min(limit, MAX_AUDIT_LOG_LIMIT))
        offset = max(0, offset)

        results: list[AuthConfigAudit] = (
            query.order_by(AuthConfigAudit.created_at.desc()).offset(offset).limit(limit).all()
        )
        return results

    @staticmethod
    def migrate_from_env(db: Session, user_id: int) -> int:
        """Migrate configuration from environment variables to database.

        This performs a one-time migration of settings from .env to the
        database. Only migrates values that don't already exist in the database.

        Args:
            db: Database session
            user_id: ID of user performing the migration

        Returns:
            Number of settings migrated
        """
        migrated = 0

        for category, keys in AuthConfigService.CONFIG_CATEGORIES.items():
            for key in keys:
                # Check if already exists in database
                existing = db.query(AuthConfig).filter(AuthConfig.config_key == key).first()
                if existing:
                    continue

                # Find corresponding env variable. Not simply key.upper(): a few
                # keys deliberately differ (allow_registration is
                # ALLOW_OPEN_REGISTRATION), and those silently resolved to None.
                env_key = AuthConfigService.env_var_for(key)
                env_value = getattr(settings, env_key, None)

                if env_value is not None:
                    is_sensitive = key in AuthConfigService.SENSITIVE_KEYS

                    AuthConfigService.set_config(
                        db=db,
                        key=key,
                        value=env_value,
                        is_sensitive=is_sensitive,
                        category=category,
                        user_id=user_id,
                        description=f"Migrated from environment variable {env_key}",
                    )
                    migrated += 1
                    logger.info(f"Migrated {key} from env to database")

        logger.info(f"Migration complete: {migrated} settings migrated from env")
        return migrated

    @staticmethod
    def get_config_status(db: Session) -> dict[str, bool]:
        """Get the enabled/disabled status of each authentication method.

        Args:
            db: Database session

        Returns:
            Dictionary with enabled status for each auth method
        """
        return {
            "ldap_enabled": bool(AuthConfigService.get_effective_config(db, "ldap_enabled")),
            "keycloak_enabled": bool(
                AuthConfigService.get_effective_config(db, "keycloak_enabled")
            ),
            "pki_enabled": bool(AuthConfigService.get_effective_config(db, "pki_enabled")),
            "mfa_enabled": bool(AuthConfigService.get_effective_config(db, "mfa_enabled")),
            "password_policy_enabled": bool(
                AuthConfigService.get_effective_config(db, "password_policy_enabled")
            ),
            "login_banner_enabled": bool(
                AuthConfigService.get_effective_config(db, "login_banner_enabled")
            ),
        }
