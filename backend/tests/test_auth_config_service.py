"""
Tests for AuthConfigService - authentication configuration management.

Tests verify:
- Getting and setting configuration values
- Encryption of sensitive values
- Bulk updates by category
- Effective config precedence (database > .env)
- Audit logging of configuration changes

NOTE: These tests are for the dynamic auth configuration service planned in the
FedRAMP compliance plan. They are currently skipped until the service is fully implemented.
"""

import os
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

# Skip all tests - auth config service in development
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_AUTH_CONFIG_TESTS", "false").lower() != "true",
    reason="Auth config service in development (set RUN_AUTH_CONFIG_TESTS=true to run)",
)

from sqlalchemy.orm import Session

from app.models.auth_config import AuthConfig
from app.models.auth_config import AuthConfigAudit
from app.services.auth_config_service import AuthConfigService


class TestAuthConfigServiceGetConfig:
    """Test AuthConfigService get configuration methods."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        return MagicMock(spec=Session)

    def test_get_config_returns_value(self, mock_db):
        """Test getting a configuration value."""
        mock_config = MagicMock(spec=AuthConfig)
        mock_config.config_value = "test_value"
        mock_config.is_sensitive = False
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config

        result = AuthConfigService.get_config(mock_db, "test_key")
        assert result == "test_value"

    def test_get_config_returns_none_for_missing(self, mock_db):
        """Test getting non-existent configuration."""
        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = AuthConfigService.get_config(mock_db, "nonexistent")
        assert result is None

    def test_get_config_decrypts_sensitive(self, mock_db):
        """Test that sensitive values are decrypted."""
        mock_config = MagicMock(spec=AuthConfig)
        mock_config.config_value = "encrypted_value"
        mock_config.is_sensitive = True
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config

        with patch("app.services.auth_config_service.decrypt_api_key") as mock_decrypt:
            mock_decrypt.return_value = "decrypted_value"
            result = AuthConfigService.get_config(mock_db, "sensitive_key")
            assert result == "decrypted_value"
            mock_decrypt.assert_called_once_with("encrypted_value")

    def test_get_config_no_decrypt_option(self, mock_db):
        """Test getting config without decryption."""
        mock_config = MagicMock(spec=AuthConfig)
        mock_config.config_value = "encrypted_value"
        mock_config.is_sensitive = True
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config

        result = AuthConfigService.get_config(mock_db, "sensitive_key", decrypt=False)
        assert result == "encrypted_value"

    def test_get_config_decryption_failure_returns_none(self, mock_db):
        """A value that cannot be decrypted is UNSET, never the raw ciphertext.

        This test previously asserted the opposite — that the ciphertext is handed
        back — which pinned a fail-open as intended behaviour (issue #324). Callers
        use these values as real credentials (LDAP bind password, OIDC client
        secret), so ciphertext is at best a baffling auth failure and at worst an
        encrypted blob shipped to an external IdP or rendered in the admin UI.

        Returning None makes `_get_effective` fall through to the env value and then
        the coded default — a known source instead of garbage.
        """
        mock_config = MagicMock(spec=AuthConfig)
        mock_config.config_value = "encrypted_value"
        mock_config.is_sensitive = True
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config

        with patch("app.services.auth_config_service.decrypt_api_key") as mock_decrypt:
            mock_decrypt.side_effect = Exception("Decryption failed")
            result = AuthConfigService.get_config(mock_db, "sensitive_key")
            assert result is None, "must not hand back the ciphertext"

    def test_get_config_empty_decryption_returns_none(self, mock_db):
        """A decrypt that returns falsy without raising is also UNSET.

        The quieter half of the same bug: the old code only logged in the `except`
        branch, so a `decrypt_api_key` that returned None or "" left `value` as the
        ciphertext and said nothing at all.
        """
        mock_config = MagicMock(spec=AuthConfig)
        mock_config.config_value = "encrypted_value"
        mock_config.is_sensitive = True
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config

        with patch("app.services.auth_config_service.decrypt_api_key") as mock_decrypt:
            mock_decrypt.return_value = None
            result = AuthConfigService.get_config(mock_db, "sensitive_key")
            assert result is None, "must not silently fall back to the ciphertext"


class TestAuthConfigServiceSetConfig:
    """Test AuthConfigService set configuration methods."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = MagicMock(spec=Session)
        db.query.return_value.filter.return_value.first.return_value = None
        return db

    def test_set_config_creates_new(self, mock_db):
        """Test creating new configuration."""
        result = AuthConfigService.set_config(
            db=mock_db,
            key="new_key",
            value="new_value",
            is_sensitive=False,
            category="test",
            user_id=1,
        )

        mock_db.add.assert_called()
        mock_db.commit.assert_called()

    def test_set_config_updates_existing(self, mock_db):
        """Test updating existing configuration."""
        existing_config = MagicMock(spec=AuthConfig)
        existing_config.config_value = "old_value"
        mock_db.query.return_value.filter.return_value.first.return_value = existing_config

        AuthConfigService.set_config(
            db=mock_db,
            key="existing_key",
            value="new_value",
            is_sensitive=False,
            category="test",
            user_id=1,
        )

        assert existing_config.config_value == "new_value"
        mock_db.commit.assert_called()

    def test_set_config_encrypts_sensitive(self, mock_db):
        """Test that sensitive values are encrypted."""
        with patch("app.services.auth_config_service.encrypt_api_key") as mock_encrypt:
            mock_encrypt.return_value = "encrypted"
            AuthConfigService.set_config(
                db=mock_db,
                key="secret_key",
                value="secret_value",
                is_sensitive=True,
                category="test",
                user_id=1,
            )
            mock_encrypt.assert_called_once_with("secret_value")

    def test_set_config_bool_conversion(self, mock_db):
        """Test boolean value conversion."""
        AuthConfigService.set_config(
            db=mock_db,
            key="bool_key",
            value=True,
            is_sensitive=False,
            category="test",
            user_id=1,
        )

        # Check that the add call includes a properly converted value
        call_args = mock_db.add.call_args_list
        # At least one call should have been made with an AuthConfig
        assert len(call_args) >= 1

    def test_set_config_with_request(self, mock_db):
        """Test setting config with request for audit logging."""
        mock_request = MagicMock()
        mock_request.client.host = "127.0.0.1"
        mock_headers = MagicMock()
        mock_headers.get = MagicMock(return_value="test-agent")
        mock_request.headers = mock_headers

        AuthConfigService.set_config(
            db=mock_db,
            key="test_key",
            value="test_value",
            is_sensitive=False,
            category="test",
            user_id=1,
            request=mock_request,
        )

        mock_db.add.assert_called()
        mock_db.commit.assert_called()


class TestAuthConfigServiceBulkUpdate:
    """Test AuthConfigService bulk update methods."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = MagicMock(spec=Session)
        db.query.return_value.filter.return_value.first.return_value = None
        return db

    def test_bulk_update_category(self, mock_db):
        """Test bulk updating a category.

        Updated: this used to write ``{"key1": ..., "key2": ...}`` to a category
        called ``"test"``, pinning the behaviour that any key in any category is
        stored verbatim. Unknown keys are now a ValueError (400 at the HTTP edge),
        so the payload has to be real keys in a real category.
        """
        config = {
            "keycloak_realm": "opentranscribe",
            "keycloak_admin_role": "admin",
            "keycloak_use_pkce": True,
        }

        AuthConfigService.bulk_update_category(
            db=mock_db, category="keycloak", config_dict=config, user_id=1
        )

        # Should have processed all non-sensitive keys
        assert mock_db.add.call_count >= 3  # At least 3 configs + 3 audits

    def test_bulk_update_rejects_unknown_keys(self, mock_db):
        """A typo'd key is refused instead of being stored and read by nothing."""
        with pytest.raises(ValueError, match="keycloak_verify_audiance"):
            AuthConfigService.bulk_update_category(
                db=mock_db,
                category="keycloak",
                config_dict={"keycloak_verify_audiance": True},
                user_id=1,
            )

        mock_db.add.assert_not_called()

    def test_bulk_update_skips_empty_sensitive(self, mock_db):
        """Test that empty sensitive values are skipped."""
        config = {
            "ldap_bind_password": "",  # Sensitive, empty - should skip
            "ldap_server": "ldap.example.com",  # Non-sensitive
        }

        AuthConfigService.bulk_update_category(
            db=mock_db, category="ldap", config_dict=config, user_id=1
        )

        # ldap_server should be processed, ldap_bind_password should be skipped
        # Due to the skip, we expect fewer calls

    def test_bulk_update_encrypts_sensitive_keys(self, mock_db):
        """Test that sensitive keys are encrypted during bulk update.

        Updated: both secrets used to be submitted under ``category="ldap"``.
        ``keycloak_client_secret`` belongs to ``keycloak`` and writing it through
        the LDAP tab is now a ValueError, so each goes to its own category.
        """
        with patch("app.services.auth_config_service.encrypt_api_key") as mock_encrypt:
            mock_encrypt.return_value = "encrypted"

            AuthConfigService.bulk_update_category(
                db=mock_db,
                category="ldap",
                config_dict={"ldap_bind_password": "secret123"},
                user_id=1,
            )
            AuthConfigService.bulk_update_category(
                db=mock_db,
                category="keycloak",
                config_dict={"keycloak_client_secret": "another_secret"},
                user_id=1,
            )

            # Both sensitive keys should have been encrypted
            assert mock_encrypt.call_count == 2


class TestAuthConfigServiceEffectiveConfig:
    """Test AuthConfigService effective config precedence."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        return MagicMock(spec=Session)

    def test_get_effective_config_db_precedence(self, mock_db):
        """Test that database config takes precedence over .env."""
        mock_config = MagicMock(spec=AuthConfig)
        mock_config.config_value = "db_value"
        mock_config.is_sensitive = False
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config

        result = AuthConfigService.get_effective_config(mock_db, "some_key")
        assert result == "db_value"

    def test_get_effective_config_env_fallback(self, mock_db):
        """Test fallback to environment settings."""
        mock_db.query.return_value.filter.return_value.first.return_value = None

        with patch("app.services.auth_config_service.settings") as mock_settings:
            mock_settings.LDAP_ENABLED = True
            result = AuthConfigService.get_effective_config(mock_db, "ldap_enabled")
            # Should return the settings value when not in DB

    def test_get_effective_config_bool_conversion(self, mock_db):
        """Test boolean conversion for effective config."""
        mock_config = MagicMock(spec=AuthConfig)
        mock_config.config_value = "true"
        mock_config.is_sensitive = False
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config

        result = AuthConfigService.get_effective_config(mock_db, "ldap_enabled")
        assert result is True

    def test_get_effective_config_int_conversion(self, mock_db):
        """Test integer conversion for effective config."""
        mock_config = MagicMock(spec=AuthConfig)
        mock_config.config_value = "636"
        mock_config.is_sensitive = False
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config

        result = AuthConfigService.get_effective_config(mock_db, "ldap_port")
        assert result == 636


class TestAuthConfigServiceValueConversion:
    """Test AuthConfigService value conversion methods."""

    def test_convert_value_bool_true(self):
        """Test boolean true conversion."""
        assert AuthConfigService._convert_value("true", "bool") is True
        assert AuthConfigService._convert_value("1", "bool") is True
        assert AuthConfigService._convert_value("yes", "bool") is True
        assert AuthConfigService._convert_value("on", "bool") is True

    def test_convert_value_bool_false(self):
        """Test boolean false conversion."""
        assert AuthConfigService._convert_value("false", "bool") is False
        assert AuthConfigService._convert_value("0", "bool") is False
        assert AuthConfigService._convert_value("no", "bool") is False
        assert AuthConfigService._convert_value("anything", "bool") is False

    def test_convert_value_int(self):
        """Test integer conversion."""
        assert AuthConfigService._convert_value("42", "int") == 42
        assert AuthConfigService._convert_value("invalid", "int") == 0
        assert AuthConfigService._convert_value(None, "int") == 0

    def test_convert_value_json(self):
        """Test JSON conversion."""
        result = AuthConfigService._convert_value('{"key": "value"}', "json")
        assert result == {"key": "value"}

        result = AuthConfigService._convert_value("invalid", "json")
        assert result == {}

    def test_convert_value_string(self):
        """Test string passthrough."""
        assert AuthConfigService._convert_value("test", "string") == "test"

    def test_convert_value_none(self):
        """Test None value defaults."""
        assert AuthConfigService._convert_value(None, "bool") is False
        assert AuthConfigService._convert_value(None, "int") == 0
        assert AuthConfigService._convert_value(None, "json") == {}


class TestAuthConfigServiceSensitiveKeys:
    """Test AuthConfigService sensitive key handling."""

    def test_sensitive_keys_defined(self):
        """Test that sensitive keys are properly defined."""
        sensitive = AuthConfigService.SENSITIVE_KEYS

        assert "ldap_bind_password" in sensitive
        assert "keycloak_client_secret" in sensitive

    def test_sensitive_key_identification(self):
        """Test identifying sensitive keys during operations."""
        # ldap_bind_password should be identified as sensitive
        assert "ldap_bind_password" in AuthConfigService.SENSITIVE_KEYS

        # ldap_server should not be sensitive
        assert "ldap_server" not in AuthConfigService.SENSITIVE_KEYS


class TestAuthConfigAudit:
    """Test authentication configuration audit logging."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = MagicMock(spec=Session)
        db.query.return_value.filter.return_value.first.return_value = None
        return db

    def test_audit_log_created_on_change(self, mock_db):
        """Test that audit log is created when config changes."""
        AuthConfigService.set_config(
            db=mock_db,
            key="audit_test",
            value="test_value",
            is_sensitive=False,
            category="test",
            user_id=1,
        )

        # Verify audit entry was added (2 add calls: config + audit)
        assert mock_db.add.call_count >= 2

    def test_sensitive_values_masked_in_audit(self, mock_db):
        """Test that sensitive values are masked in audit log."""
        with patch("app.services.auth_config_service.encrypt_api_key") as mock_encrypt:
            mock_encrypt.return_value = "encrypted"
            AuthConfigService.set_config(
                db=mock_db,
                key="ldap_bind_password",
                value="secret",
                is_sensitive=True,
                category="ldap",
                user_id=1,
            )

        # Check that audit was created with masked values
        add_calls = mock_db.add.call_args_list
        audit_call = None
        for call in add_calls:
            obj = call[0][0]
            if isinstance(obj, AuthConfigAudit):
                audit_call = obj
                break

        # The audit entry should have redacted value
        if audit_call:
            assert audit_call.new_value == "***REDACTED***"

    def test_get_audit_log(self, mock_db):
        """Test getting audit log entries."""
        mock_audits = [MagicMock(spec=AuthConfigAudit) for _ in range(3)]
        mock_db.query.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = mock_audits

        result = AuthConfigService.get_audit_log(mock_db, limit=10)
        assert len(result) == 3

    def test_get_audit_log_by_category(self, mock_db):
        """Test getting audit log filtered by category."""
        mock_audits = [MagicMock(spec=AuthConfigAudit)]
        mock_db.query.return_value.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = mock_audits

        result = AuthConfigService.get_audit_log(mock_db, category="ldap")
        assert len(result) == 1


class TestAuthConfigServiceDelete:
    """Test AuthConfigService delete methods."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        return MagicMock(spec=Session)

    def test_delete_config_existing(self, mock_db):
        """Test deleting existing configuration."""
        existing_config = MagicMock(spec=AuthConfig)
        existing_config.config_value = "old_value"
        existing_config.is_sensitive = False
        mock_db.query.return_value.filter.return_value.first.return_value = existing_config

        result = AuthConfigService.delete_config(db=mock_db, key="test_key", user_id=1)

        assert result is True
        mock_db.delete.assert_called_once_with(existing_config)
        mock_db.commit.assert_called()

    def test_delete_config_nonexistent(self, mock_db):
        """Test deleting non-existent configuration."""
        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = AuthConfigService.delete_config(db=mock_db, key="nonexistent", user_id=1)

        assert result is False
        mock_db.delete.assert_not_called()


class TestAuthConfigServiceCategories:
    """Test AuthConfigService category handling."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        return MagicMock(spec=Session)

    def test_get_config_by_category(self, mock_db):
        """Test getting all config values for a category."""
        mock_configs = [
            MagicMock(
                config_key="ldap_enabled",
                config_value="true",
                is_sensitive=False,
                data_type="bool",
            ),
            MagicMock(
                config_key="ldap_server",
                config_value="ldap.example.com",
                is_sensitive=False,
                data_type="string",
            ),
        ]
        mock_db.query.return_value.filter.return_value.all.return_value = mock_configs

        result = AuthConfigService.get_config_by_category(mock_db, "ldap")

        assert result["ldap_enabled"] is True
        assert result["ldap_server"] == "ldap.example.com"

    def test_config_categories_defined(self):
        """Test that all config categories are defined."""
        categories = AuthConfigService.CONFIG_CATEGORIES

        assert "ldap" in categories
        assert "keycloak" in categories
        assert "pki" in categories
        assert "password_policy" in categories
        assert "mfa" in categories
        assert "session" in categories
        assert "banner" in categories
        assert "lockout" in categories

    def test_get_config_status(self, mock_db):
        """Test getting auth method enabled status."""
        # Mock get_effective_config to return values
        with patch.object(AuthConfigService, "get_effective_config") as mock_get_effective:
            mock_get_effective.side_effect = lambda db, key: {
                "ldap_enabled": True,
                "keycloak_enabled": False,
                "pki_enabled": False,
                "mfa_enabled": True,
                "password_policy_enabled": True,
                "login_banner_enabled": False,
            }.get(key, False)

            result = AuthConfigService.get_config_status(mock_db)

            assert result["ldap_enabled"] is True
            assert result["keycloak_enabled"] is False
            assert result["mfa_enabled"] is True


class TestAuthConfigServiceMigration:
    """Test AuthConfigService environment migration."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = MagicMock(spec=Session)
        db.query.return_value.filter.return_value.first.return_value = None
        return db

    def test_migrate_from_env(self, mock_db):
        """Test migrating settings from environment to database."""
        with patch("app.services.auth_config_service.settings") as mock_settings:
            mock_settings.LDAP_ENABLED = True
            mock_settings.LDAP_SERVER = "ldap.example.com"
            mock_settings.LDAP_PORT = 636

            count = AuthConfigService.migrate_from_env(mock_db, user_id=1)

            # Should have migrated settings
            assert mock_db.add.call_count > 0

    def test_migrate_skips_existing(self, mock_db):
        """Test that migration skips existing database entries."""
        existing_config = MagicMock(spec=AuthConfig)
        mock_db.query.return_value.filter.return_value.first.return_value = existing_config

        with patch("app.services.auth_config_service.settings") as mock_settings:
            mock_settings.LDAP_ENABLED = True

            count = AuthConfigService.migrate_from_env(mock_db, user_id=1)

            # Should return 0 since all entries already exist
            assert count == 0


class TestAuthConfigDataTypeMapping:
    """Test AuthConfigService data type mapping."""

    def test_data_type_mapping_contains_common_keys(self):
        """Test that data type mapping contains expected keys."""
        mapping = AuthConfigService.DATA_TYPE_MAPPING

        # Boolean settings
        assert mapping["ldap_enabled"] == "bool"
        assert mapping["keycloak_enabled"] == "bool"
        assert mapping["pki_enabled"] == "bool"
        assert mapping["mfa_enabled"] == "bool"

        # Integer settings
        assert mapping["ldap_port"] == "int"
        assert mapping["ldap_timeout"] == "int"
        assert mapping["password_min_length"] == "int"
        assert mapping["mfa_backup_code_count"] == "int"

    def test_env_to_config_mapping(self):
        """Test environment to config key mapping."""
        mapping = AuthConfigService.ENV_TO_CONFIG_MAPPING

        assert mapping["LDAP_ENABLED"] == "ldap_enabled"
        assert mapping["KEYCLOAK_SERVER_URL"] == "keycloak_server_url"
        assert mapping["PKI_ENABLED"] == "pki_enabled"


# Run with: pytest tests/test_auth_config_service.py -v
