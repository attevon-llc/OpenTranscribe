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
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key

logger = logging.getLogger(__name__)


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

    # Environment variable to config key mapping
    ENV_TO_CONFIG_MAPPING = {
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

    # Config keys grouped by category
    CONFIG_CATEGORIES = {
        "local": [
            "local_enabled",
            "allow_registration",
            "require_email_verification",
            "password_min_length",
            "password_require_uppercase",
            "password_require_lowercase",
            "password_require_numbers",
            "password_require_special",
            "password_max_age_days",
            "password_history_count",
            "mfa_enabled",
            "mfa_required",
            "mfa_issuer",
            "max_login_attempts",
            "lockout_duration_minutes",
        ],
        "ldap": [
            "ldap_enabled",
            "ldap_server",
            "ldap_port",
            "ldap_use_ssl",
            "ldap_use_tls",
            "ldap_bind_dn",
            "ldap_bind_password",
            "ldap_search_base",
            "ldap_username_attr",
            "ldap_email_attr",
            "ldap_name_attr",
            "ldap_user_search_filter",
            "ldap_timeout",
            "ldap_admin_users",
            "ldap_admin_groups",
            "ldap_user_groups",
            "ldap_recursive_groups",
            "ldap_group_attr",
        ],
        "keycloak": [
            "keycloak_enabled",
            "keycloak_server_url",
            "keycloak_internal_url",
            "keycloak_realm",
            "keycloak_client_id",
            "keycloak_client_secret",
            "keycloak_callback_url",
            "keycloak_admin_role",
            "keycloak_timeout",
            "keycloak_verify_audience",
            "keycloak_audience",
            "keycloak_use_pkce",
            "keycloak_verify_issuer",
        ],
        "pki": [
            "pki_enabled",
            "pki_ca_cert_path",
            "pki_verify_revocation",
            "pki_cert_header",
            "pki_cert_dn_header",
            "pki_admin_dns",
            "pki_ocsp_timeout_seconds",
            "pki_crl_cache_seconds",
            "pki_revocation_soft_fail",
            "pki_trusted_proxies",
            "pki_mode",
            "pki_allow_password_fallback",
        ],
        "password_policy": [
            "password_policy_enabled",
            "password_min_length",
            "password_require_uppercase",
            "password_require_lowercase",
            "password_require_digit",
            "password_require_special",
            "password_history_count",
            "password_max_age_days",
        ],
        "mfa": [
            "mfa_enabled",
            "mfa_required",
            "mfa_issuer_name",
            "mfa_backup_code_count",
            "mfa_token_expire_minutes",
        ],
        "session": [
            "jwt_access_token_expire_minutes",
            "jwt_refresh_token_expire_days",
            "session_idle_timeout_minutes",
            "session_absolute_timeout_minutes",
            "max_concurrent_sessions",
            "concurrent_session_policy",
        ],
        "banner": [
            "login_banner_enabled",
            "login_banner_text",
            "login_banner_classification",
        ],
        "lockout": [
            "account_lockout_threshold",
            "account_lockout_duration_minutes",
            "account_lockout_progressive",
            "account_lockout_max_duration_minutes",
            "account_lockout_enabled",
            "rate_limit_auth_per_minute",
            "rate_limit_enabled",
        ],
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
            try:
                decrypted = decrypt_api_key(value)
                if decrypted:
                    value = decrypted
            except Exception as e:
                logger.warning(f"Failed to decrypt config value for {key}: {e}")
                # Return encrypted value if decryption fails

        return value

    @staticmethod
    def _convert_value(value: str | None, data_type: str) -> Any:
        """Convert string value to appropriate type.

        Args:
            value: String value from database
            data_type: Target data type (string, bool, int, json)

        Returns:
            Converted value
        """
        if value is None:
            defaults = {"bool": False, "int": 0, "json": {}}
            return defaults.get(data_type)

        if data_type == "bool":
            return value.lower() in ("true", "1", "yes", "on")
        elif data_type == "int":
            try:
                return int(value)
            except (ValueError, TypeError):
                return 0
        elif data_type == "json":
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
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
            if config.is_sensitive and decrypt and value:
                try:
                    decrypted = decrypt_api_key(value)  # type: ignore[call-overload]
                    value = decrypted or "***ENCRYPTED***"  # type: ignore[assignment]
                except Exception:
                    value = "***ENCRYPTED***"  # type: ignore[assignment]

            # Convert to appropriate type
            data_type = config.data_type or "string"
            result[config.config_key] = AuthConfigService._convert_value(value, data_type)  # type: ignore[index,arg-type]

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

        Args:
            db: Database session
            category: Configuration category
            config_dict: Dictionary of key-value pairs to update
            user_id: ID of user making the changes
            request: Optional FastAPI request for IP/user agent logging

        Returns:
            Dictionary of updated AuthConfig objects
        """
        results: dict[str, AuthConfig] = {}

        for key, value in config_dict.items():
            is_sensitive = key in AuthConfigService.SENSITIVE_KEYS

            # Skip empty sensitive values (don't overwrite with empty)
            if is_sensitive and (value is None or value == ""):
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
            return AuthConfigService._convert_value(db_value, data_type)

        # Fall back to environment/settings
        env_key = key.upper()
        return getattr(settings, env_key, None)

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
            limit: Maximum number of entries to return
            offset: Number of entries to skip

        Returns:
            List of audit log entries
        """
        query = db.query(AuthConfigAudit)

        if config_key:
            query = query.filter(AuthConfigAudit.config_key == config_key)
        elif category:
            # Get all keys for this category
            category_keys = AuthConfigService.CONFIG_CATEGORIES.get(category, [])
            if category_keys:
                query = query.filter(AuthConfigAudit.config_key.in_(category_keys))

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

                # Find corresponding env variable
                env_key = key.upper()
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
