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
from app.core.process_auth_settings import prime_process_auth_settings
from app.models.auth_config import AuthConfig
from app.models.auth_config import AuthConfigAudit
from app.schemas.auth_config_validation import CROSS_FIELD_KEYS
from app.schemas.auth_config_validation import coded_default
from app.schemas.auth_config_validation import validate_category_config
from app.services.auth_config_keys import CONFIG_CATEGORIES
from app.services.auth_config_keys import DATA_TYPE_MAPPING
from app.services.auth_config_keys import ENV_TO_CONFIG_MAPPING
from app.services.auth_config_keys import RESTART_REQUIRED_KEYS
from app.services.auth_config_keys import RETIRED_KEYS
from app.services.auth_config_keys import SENSITIVE_KEYS
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key

logger = logging.getLogger(__name__)

#: Spellings accepted for a stored boolean. Anything else is a parse FAILURE, not
#: a false: ``oidc_verify_issuer`` and friends are security controls, and
#: a bare truthy-tuple membership test quietly turned a typo into
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
#: OIDC client secret, with a success toast. Writes now reject it outright.
SENSITIVE_SET_SENTINEL = "__SECRET_IS_SET__"  # noqa: S105 # nosec B105

#: Values that mean "the user did not type a new secret" and must leave the stored
#: one untouched. Empty is the deliberate "leave blank to keep current" case;
#: the rest are placeholders older clients may echo back.
SENSITIVE_NO_CHANGE_VALUES = frozenset(
    {SENSITIVE_SET_SENTINEL, "***REDACTED***", "***ENCRYPTED***", ""}
)

#: The no-change values that are markers, never a real secret. ``""`` is left out:
#: storing an empty secret is how an unset env value migrates.
SENSITIVE_PLACEHOLDER_VALUES = SENSITIVE_NO_CHANGE_VALUES - {""}


class PlaceholderSecretError(ValueError):
    """A write tried to store a placeholder marker as a secret value."""


class AuthConfigService:
    """Service for managing authentication configuration.

    Provides methods for getting, setting, and managing authentication
    configuration stored in the database with proper encryption for
    sensitive values and audit logging for compliance.
    """

    #: Lazily-built reverse of ``ENV_TO_CONFIG_MAPPING`` (config key -> env var).
    _CONFIG_TO_ENV: dict[str, str] = {}

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
        the zero value. A bare truthy-tuple membership test read every
        malformed string as ``False``, so one bad character in
        ``oidc_verify_issuer``, ``oidc_verify_audience`` or
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
            category: Configuration category (ldap, oidc, pki, etc.)
            decrypt: Whether to decrypt sensitive values

        Returns:
            Dictionary of configuration key-value pairs
        """
        configs = db.query(AuthConfig).filter(AuthConfig.category == category).all()
        result: dict[str, Any] = {}

        for config in configs:
            if config.config_key in RETIRED_KEYS:
                # A row an older panel wrote for a key that no longer exists.
                # Serving it would re-render a control that changes nothing.
                continue

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

        Raises:
            PlaceholderSecretError: *value* is one of the read-side placeholders for
                a sensitive key. ``bulk_update_category`` skips those before calling
                here; this guards every other write path, which would otherwise
                encrypt the marker over the real credential.
        """
        if (
            (is_sensitive or key in SENSITIVE_KEYS)
            and isinstance(value, str)
            and value in SENSITIVE_PLACEHOLDER_VALUES
        ):
            raise PlaceholderSecretError(
                f"{key}: refusing to store a placeholder as the secret value"
            )

        # Auto-detect data type if not provided
        if data_type is None:
            data_type = DATA_TYPE_MAPPING.get(key, "string")

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

        requires_restart = key in RESTART_REQUIRED_KEYS

        # Get existing config
        config: AuthConfig | None = (
            db.query(AuthConfig).filter(AuthConfig.config_key == key).first()
        )
        old_value = config.config_value if config else None

        if config:
            # Update existing
            config.config_value = encrypted_value  # type: ignore[assignment]
            config.data_type = data_type  # type: ignore[assignment]
            # Re-derived on every write, not just on create: rows written before
            # the flag was populated would otherwise stay False forever.
            config.requires_restart = requires_restart  # type: ignore[assignment]
            # Same for the sensitivity flag: a row created before its key joined
            # SENSITIVE_KEYS kept False while its new value was encrypted, so reads
            # returned the ciphertext as if it were the credential.
            config.is_sensitive = is_sensitive
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
                requires_restart=requires_restart,
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

        # Publish the new value to the enforcement points that hold no session —
        # the password policy and the lockout counter read through the
        # process-wide layer, and without this they would keep enforcing the
        # previous value for up to a cache generation after a successful save.
        prime_process_auth_settings(db, key)

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

        # The row is gone, so the effective value reverts to .env / the coded
        # default. Re-resolve rather than leaving the deleted value cached.
        prime_process_auth_settings(db, key)

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
            is_sensitive = key in SENSITIVE_KEYS

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
            data_type = DATA_TYPE_MAPPING.get(key, "string")
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
                {v: k for k, v in ENV_TO_CONFIG_MAPPING.items()}
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
            category_keys = CONFIG_CATEGORIES.get(category)
            if category_keys is None:
                raise ValueError(
                    f"Unknown configuration category '{category}'. "
                    f"Must be one of: {', '.join(CONFIG_CATEGORIES)}"
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

        for category, keys in CONFIG_CATEGORIES.items():
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
                    is_sensitive = key in SENSITIVE_KEYS

                    try:
                        AuthConfigService.set_config(
                            db=db,
                            key=key,
                            value=env_value,
                            is_sensitive=is_sensitive,
                            category=category,
                            user_id=user_id,
                            description=f"Migrated from environment variable {env_key}",
                        )
                    except PlaceholderSecretError:
                        # Skipped like the category update skips it, so one stale
                        # env value does not fail the keys after it.
                        logger.warning(
                            "Not migrating %s: %s holds a placeholder, not a secret",
                            key,
                            env_key,
                        )
                        continue
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
            "oidc_enabled": bool(AuthConfigService.get_effective_config(db, "oidc_enabled")),
            "pki_enabled": bool(AuthConfigService.get_effective_config(db, "pki_enabled")),
            "proxy_enabled": bool(AuthConfigService.get_effective_config(db, "proxy_enabled")),
            "mfa_enabled": bool(AuthConfigService.get_effective_config(db, "mfa_enabled")),
            "password_policy_enabled": bool(
                AuthConfigService.get_effective_config(db, "password_policy_enabled")
            ),
            "login_banner_enabled": bool(
                AuthConfigService.get_effective_config(db, "login_banner_enabled")
            ),
        }
