"""
Integration tests for authentication configuration with real database.

Tests verify:
- AuthConfigService round-trip (set/get) with real PostgreSQL database
- Encryption/decryption cycle for sensitive values
- Bulk category updates with real DB commits
- LdapConfig.from_db() reading values stored in database
- OIDCConfig.from_db() reading values stored in database
- Audit log creation with real DB
- Type conversion accuracy (bool, int, string)
- DB > .env precedence via get_effective_config()

Run with: pytest tests/test_auth_config_integration.py -v
"""

import uuid

import pytest

from app.models.auth_config import AuthConfig
from app.models.auth_config import AuthConfigAudit
from app.services.auth_config_service import AuthConfigService

# Run all tests in this module in the same xdist worker to avoid deadlocks
# on the shared auth_config table during parallel test execution.
pytestmark = pytest.mark.xdist_group("auth_config")


# ===== AuthConfigService Database Round-Trip Tests =====


class TestAuthConfigServiceRoundTrip:
    """Test set_config/get_config with real database transactions."""

    def test_set_and_get_string_value(self, db_session, admin_user):
        """Store a string value and retrieve it."""
        AuthConfigService.set_config(
            db=db_session,
            key="test_ldap_server",
            value="ldap.corp.example.com",
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
        )

        result = AuthConfigService.get_config(db_session, "test_ldap_server")
        assert result == "ldap.corp.example.com"

    def test_set_and_get_bool_value(self, db_session, admin_user):
        """Store a boolean and retrieve it as the stored string."""
        AuthConfigService.set_config(
            db=db_session,
            key="test_ldap_enabled",
            value=True,
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
            data_type="bool",
        )

        raw = AuthConfigService.get_config(db_session, "test_ldap_enabled")
        assert raw == "true"

    def test_set_and_get_int_value(self, db_session, admin_user):
        """Store an integer and retrieve it as the stored string."""
        AuthConfigService.set_config(
            db=db_session,
            key="test_ldap_port",
            value=636,
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
            data_type="int",
        )

        raw = AuthConfigService.get_config(db_session, "test_ldap_port")
        assert raw == "636"

    def test_update_existing_value(self, db_session, admin_user):
        """Updating an existing key replaces the value."""
        AuthConfigService.set_config(
            db=db_session,
            key="test_update_key",
            value="original",
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
        )

        AuthConfigService.set_config(
            db=db_session,
            key="test_update_key",
            value="updated",
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
        )

        result = AuthConfigService.get_config(db_session, "test_update_key")
        assert result == "updated"

    def test_get_missing_key_returns_none(self, db_session):
        """Getting a non-existent key returns None."""
        result = AuthConfigService.get_config(db_session, f"nonexistent_{uuid.uuid4().hex[:8]}")
        assert result is None

    def test_delete_config(self, db_session, admin_user):
        """Deleting a key removes it from the database."""
        AuthConfigService.set_config(
            db=db_session,
            key="test_delete_me",
            value="temporary",
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
        )
        assert AuthConfigService.get_config(db_session, "test_delete_me") == "temporary"

        deleted = AuthConfigService.delete_config(
            db_session, "test_delete_me", user_id=admin_user.id
        )
        assert deleted is True
        assert AuthConfigService.get_config(db_session, "test_delete_me") is None

    def test_delete_nonexistent_returns_false(self, db_session, admin_user):
        """Deleting a non-existent key returns False."""
        result = AuthConfigService.delete_config(
            db_session, f"no_such_key_{uuid.uuid4().hex[:8]}", user_id=admin_user.id
        )
        assert result is False


# ===== Sensitive Value Encryption Tests =====


class TestSensitiveValueEncryption:
    """Test encryption/decryption cycle for sensitive config values."""

    def test_sensitive_value_encrypted_at_rest(self, db_session, admin_user):
        """Sensitive values are stored encrypted in the database."""
        plain_password = f"secret_password_{uuid.uuid4().hex[:8]}"
        AuthConfigService.set_config(
            db=db_session,
            key="test_ldap_bind_password",
            value=plain_password,
            is_sensitive=True,
            category="ldap",
            user_id=admin_user.id,
        )

        # Read raw from DB — should NOT be plaintext
        row = (
            db_session.query(AuthConfig)
            .filter(AuthConfig.config_key == "test_ldap_bind_password")
            .first()
        )
        assert row is not None
        assert row.config_value != plain_password
        assert row.is_sensitive is True

    def test_sensitive_value_decrypted_on_read(self, db_session, admin_user):
        """Sensitive values are decrypted when read via get_config."""
        plain_password = f"my_secret_{uuid.uuid4().hex[:8]}"
        AuthConfigService.set_config(
            db=db_session,
            key="test_sensitive_read",
            value=plain_password,
            is_sensitive=True,
            category="ldap",
            user_id=admin_user.id,
        )

        result = AuthConfigService.get_config(db_session, "test_sensitive_read", decrypt=True)
        assert result == plain_password

    def test_sensitive_value_raw_when_no_decrypt(self, db_session, admin_user):
        """Sensitive values returned encrypted when decrypt=False."""
        plain_password = f"raw_secret_{uuid.uuid4().hex[:8]}"
        AuthConfigService.set_config(
            db=db_session,
            key="test_sensitive_raw",
            value=plain_password,
            is_sensitive=True,
            category="ldap",
            user_id=admin_user.id,
        )

        raw = AuthConfigService.get_config(db_session, "test_sensitive_raw", decrypt=False)
        assert raw != plain_password  # Should be encrypted

    def test_empty_sensitive_value_stored_as_none(self, db_session, admin_user):
        """Empty sensitive value is stored as None (not encrypted)."""
        AuthConfigService.set_config(
            db=db_session,
            key="test_empty_sensitive",
            value=None,
            is_sensitive=True,
            category="ldap",
            user_id=admin_user.id,
        )

        row = (
            db_session.query(AuthConfig)
            .filter(AuthConfig.config_key == "test_empty_sensitive")
            .first()
        )
        assert row is not None
        assert row.config_value is None


# ===== Bulk Update Tests =====


class TestBulkUpdateCategory:
    """Test bulk_update_category with real database."""

    def test_bulk_update_ldap_category(self, db_session, admin_user):
        """Bulk update stores all LDAP config values."""
        config = {
            "ldap_enabled": True,
            "ldap_server": "ldap.test.example.com",
            "ldap_port": 636,
            "ldap_use_ssl": True,
            "ldap_use_tls": False,
            "ldap_bind_dn": "cn=admin,dc=test,dc=com",
            "ldap_search_base": "dc=test,dc=com",
            "ldap_username_attr": "uid",
            "ldap_email_attr": "mail",
            "ldap_name_attr": "displayName",
            "ldap_user_search_filter": "(uid={username})",
            "ldap_timeout": 15,
        }

        results = AuthConfigService.bulk_update_category(
            db=db_session,
            category="ldap",
            config_dict=config,
            user_id=admin_user.id,
        )

        assert len(results) == len(config)

        # Verify each value round-trips correctly
        assert AuthConfigService.get_config(db_session, "ldap_server") == "ldap.test.example.com"
        assert AuthConfigService.get_config(db_session, "ldap_port") == "636"
        assert AuthConfigService.get_config(db_session, "ldap_enabled") == "true"
        assert AuthConfigService.get_config(db_session, "ldap_use_ssl") == "true"
        assert AuthConfigService.get_config(db_session, "ldap_use_tls") == "false"

    def test_bulk_update_skips_empty_sensitive(self, db_session, admin_user):
        """Empty sensitive values (passwords) are skipped in bulk update."""
        # First set a password
        AuthConfigService.set_config(
            db=db_session,
            key="ldap_bind_password",
            value="original_password",
            is_sensitive=True,
            category="ldap",
            user_id=admin_user.id,
        )

        # Bulk update with empty password — should NOT overwrite
        AuthConfigService.bulk_update_category(
            db=db_session,
            category="ldap",
            config_dict={"ldap_bind_password": "", "ldap_server": "new.server.com"},
            user_id=admin_user.id,
        )

        # Password should still be the original (decrypted)
        password = AuthConfigService.get_config(db_session, "ldap_bind_password", decrypt=True)
        assert password == "original_password"

        # Server should be updated
        assert AuthConfigService.get_config(db_session, "ldap_server") == "new.server.com"

    def test_bulk_update_oidc_category(self, db_session, admin_user):
        """Bulk update stores all OIDC config values."""
        config = {
            "oidc_enabled": True,
            "oidc_server_url": "https://keycloak.test.example.com",
            "oidc_realm": "testrealm",
            "oidc_client_id": "test-client",
            "oidc_client_secret": "test-secret-value",
            "oidc_callback_url": "http://localhost:5173/api/auth/keycloak/callback",
            "oidc_admin_role": "admin",
            "oidc_timeout": 30,
            "oidc_use_pkce": True,
            "oidc_verify_issuer": True,
            "oidc_verify_audience": False,
        }

        results = AuthConfigService.bulk_update_category(
            db=db_session,
            category="oidc",
            config_dict=config,
            user_id=admin_user.id,
        )

        assert "oidc_enabled" in results
        assert (
            AuthConfigService.get_config(db_session, "oidc_server_url")
            == "https://keycloak.test.example.com"
        )
        assert AuthConfigService.get_config(db_session, "oidc_realm") == "testrealm"

        # Client secret should be decrypted
        secret = AuthConfigService.get_config(db_session, "oidc_client_secret", decrypt=True)
        assert secret == "test-secret-value"


# ===== Audit Log Tests =====


class TestAuditLogIntegration:
    """Test audit log creation in real database."""

    def test_create_generates_audit_entry(self, db_session, admin_user):
        """Creating a config entry creates an audit log."""
        key = f"audit_test_{uuid.uuid4().hex[:8]}"
        AuthConfigService.set_config(
            db=db_session,
            key=key,
            value="test_value",
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
        )

        audits = db_session.query(AuthConfigAudit).filter(AuthConfigAudit.config_key == key).all()
        assert len(audits) == 1
        assert audits[0].change_type == "create"
        assert audits[0].new_value == "test_value"
        assert audits[0].changed_by == admin_user.id

    def test_update_generates_audit_entry(self, db_session, admin_user):
        """Updating a config entry creates an audit log with old and new values."""
        key = f"audit_update_{uuid.uuid4().hex[:8]}"
        AuthConfigService.set_config(
            db=db_session,
            key=key,
            value="original",
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
        )
        AuthConfigService.set_config(
            db=db_session,
            key=key,
            value="modified",
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
        )

        audits = (
            db_session.query(AuthConfigAudit)
            .filter(AuthConfigAudit.config_key == key)
            .order_by(AuthConfigAudit.created_at)
            .all()
        )
        assert len(audits) == 2
        assert audits[1].change_type == "update"
        assert audits[1].old_value == "original"
        assert audits[1].new_value == "modified"

    def test_sensitive_value_redacted_in_audit(self, db_session, admin_user):
        """Sensitive values are redacted (***REDACTED***) in audit log."""
        key = f"audit_sensitive_{uuid.uuid4().hex[:8]}"
        AuthConfigService.set_config(
            db=db_session,
            key=key,
            value="super_secret",
            is_sensitive=True,
            category="ldap",
            user_id=admin_user.id,
        )

        audit = db_session.query(AuthConfigAudit).filter(AuthConfigAudit.config_key == key).first()
        assert audit is not None
        assert audit.new_value == "***REDACTED***"

    def test_delete_generates_audit_entry(self, db_session, admin_user):
        """Deleting a config entry creates a delete audit log."""
        key = f"audit_delete_{uuid.uuid4().hex[:8]}"
        AuthConfigService.set_config(
            db=db_session,
            key=key,
            value="to_delete",
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
        )

        AuthConfigService.delete_config(db_session, key, user_id=admin_user.id)

        audits = (
            db_session.query(AuthConfigAudit)
            .filter(AuthConfigAudit.config_key == key)
            .order_by(AuthConfigAudit.created_at)
            .all()
        )
        assert len(audits) == 2
        assert audits[1].change_type == "delete"

    def test_get_audit_log_returns_entries(self, db_session, admin_user):
        """get_audit_log returns audit entries for a specific key."""
        key = f"audit_query_{uuid.uuid4().hex[:8]}"
        AuthConfigService.set_config(
            db=db_session,
            key=key,
            value="v1",
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
        )
        AuthConfigService.set_config(
            db=db_session,
            key=key,
            value="v2",
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
        )

        audits = AuthConfigService.get_audit_log(db_session, config_key=key)
        assert len(audits) >= 2


# ===== get_config_by_category Tests =====


class TestGetConfigByCategory:
    """Test retrieving config by category from real database."""

    def test_get_ldap_category(self, db_session, admin_user):
        """Retrieves all LDAP settings with proper type conversion."""
        # Store several LDAP values
        AuthConfigService.set_config(
            db=db_session,
            key="ldap_enabled",
            value=True,
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
            data_type="bool",
        )
        AuthConfigService.set_config(
            db=db_session,
            key="ldap_port",
            value=389,
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
            data_type="int",
        )
        AuthConfigService.set_config(
            db=db_session,
            key="ldap_server",
            value="ldap.example.com",
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
            data_type="string",
        )

        result = AuthConfigService.get_config_by_category(db_session, "ldap")

        assert result["ldap_enabled"] is True
        assert result["ldap_port"] == 389
        assert result["ldap_server"] == "ldap.example.com"


# ===== get_effective_config Precedence Tests =====


class TestEffectiveConfigPrecedence:
    """Test DB > .env precedence via get_effective_config."""

    def test_db_value_takes_precedence(self, db_session, admin_user):
        """Database value overrides environment variable."""
        AuthConfigService.set_config(
            db=db_session,
            key="ldap_port",
            value=636,
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
            data_type="int",
        )

        result = AuthConfigService.get_effective_config(db_session, "ldap_port")
        assert result == 636

    def test_bool_conversion_from_db(self, db_session, admin_user):
        """Boolean values from DB are properly converted."""
        AuthConfigService.set_config(
            db=db_session,
            key="ldap_enabled",
            value=True,
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
            data_type="bool",
        )

        result = AuthConfigService.get_effective_config(db_session, "ldap_enabled")
        assert result is True

    def test_string_passthrough_from_db(self, db_session, admin_user):
        """String values from DB are returned as-is."""
        AuthConfigService.set_config(
            db=db_session,
            key="ldap_server",
            value="my.ldap.host",
            is_sensitive=False,
            category="ldap",
            user_id=admin_user.id,
            data_type="string",
        )

        result = AuthConfigService.get_effective_config(db_session, "ldap_server")
        assert result == "my.ldap.host"


# ===== LdapConfig.from_db() Integration Tests =====


class TestLdapConfigFromDb:
    """Test LdapConfig.from_db() reading from real database."""

    def _store_ldap_config(self, db_session, admin_user, overrides=None):
        """Helper: store a complete LDAP configuration in the database."""
        defaults = {
            "ldap_enabled": ("true", "bool"),
            "ldap_server": ("ldap.integration-test.com", "string"),
            "ldap_port": ("636", "int"),
            "ldap_use_ssl": ("true", "bool"),
            "ldap_use_tls": ("false", "bool"),
            "ldap_bind_dn": ("cn=reader,dc=test,dc=com", "string"),
            "ldap_search_base": ("dc=test,dc=com", "string"),
            "ldap_username_attr": ("sAMAccountName", "string"),
            "ldap_email_attr": ("mail", "string"),
            "ldap_name_attr": ("displayName", "string"),
            "ldap_group_attr": ("memberOf", "string"),
            "ldap_user_search_filter": ("(sAMAccountName={username})", "string"),
            "ldap_admin_users": ("admin,superuser", "string"),
            "ldap_admin_groups": ("CN=Admins,DC=test,DC=com", "string"),
            "ldap_user_groups": ("", "string"),
            "ldap_recursive_groups": ("true", "bool"),
            "ldap_timeout": ("15", "int"),
        }
        if overrides:
            defaults.update(overrides)

        for key, (value, dtype) in defaults.items():
            is_sensitive = key in AuthConfigService.SENSITIVE_KEYS
            existing = db_session.query(AuthConfig).filter(AuthConfig.config_key == key).first()
            if existing:
                existing.config_value = value
                existing.data_type = dtype
            else:
                db_session.add(
                    AuthConfig(
                        config_key=key,
                        config_value=value,
                        category="ldap",
                        data_type=dtype,
                        is_sensitive=is_sensitive,
                    )
                )
        db_session.commit()

    def test_from_db_reads_all_fields(self, db_session, admin_user):
        """LdapConfig.from_db() populates all fields from database."""
        from app.auth.ldap_auth import LdapConfig

        self._store_ldap_config(db_session, admin_user)

        config = LdapConfig.from_db(db_session)

        assert config.enabled is True
        assert config.server == "ldap.integration-test.com"
        assert config.port == 636
        assert config.use_ssl is True
        assert config.use_tls is False
        assert config.bind_dn == "cn=reader,dc=test,dc=com"
        assert config.search_base == "dc=test,dc=com"
        assert config.username_attr == "sAMAccountName"
        assert config.email_attr == "mail"
        assert config.name_attr == "displayName"
        assert config.group_attr == "memberOf"
        assert config.user_search_filter == "(sAMAccountName={username})"
        assert config.admin_users == "admin,superuser"
        assert config.admin_groups == "CN=Admins,DC=test,DC=com"
        assert config.user_groups == ""
        assert config.recursive_groups is True
        assert config.timeout == 15

    def test_from_db_bool_conversion(self, db_session, admin_user):
        """LdapConfig.from_db() correctly converts boolean values."""
        from app.auth.ldap_auth import LdapConfig

        self._store_ldap_config(
            db_session,
            admin_user,
            overrides={
                "ldap_enabled": ("false", "bool"),
                "ldap_use_ssl": ("false", "bool"),
                "ldap_use_tls": ("true", "bool"),
                "ldap_recursive_groups": ("false", "bool"),
            },
        )

        config = LdapConfig.from_db(db_session)

        assert config.enabled is False
        assert config.use_ssl is False
        assert config.use_tls is True
        assert config.recursive_groups is False

    def test_from_db_int_conversion(self, db_session, admin_user):
        """LdapConfig.from_db() correctly converts integer values."""
        from app.auth.ldap_auth import LdapConfig

        self._store_ldap_config(
            db_session,
            admin_user,
            overrides={
                "ldap_port": ("389", "int"),
                "ldap_timeout": ("30", "int"),
            },
        )

        config = LdapConfig.from_db(db_session)

        assert config.port == 389
        assert config.timeout == 30

    def test_from_db_custom_search_filter(self, db_session, admin_user):
        """LdapConfig.from_db() reads custom user_search_filter."""
        from app.auth.ldap_auth import LdapConfig

        self._store_ldap_config(
            db_session,
            admin_user,
            overrides={
                "ldap_user_search_filter": ("(uid={username})", "string"),
                "ldap_username_attr": ("uid", "string"),
            },
        )

        config = LdapConfig.from_db(db_session)

        assert config.user_search_filter == "(uid={username})"
        assert config.username_attr == "uid"

    def test_from_db_is_frozen(self, db_session, admin_user):
        """LdapConfig is immutable (frozen dataclass)."""
        from app.auth.ldap_auth import LdapConfig

        self._store_ldap_config(db_session, admin_user)
        config = LdapConfig.from_db(db_session)

        with pytest.raises(AttributeError):
            config.server = "mutated.example.com"  # type: ignore[misc]  # Testing immutability


# ===== OIDCConfig.from_db() Integration Tests =====


class TestOIDCConfigFromDb:
    """Test OIDCConfig.from_db() reading from real database."""

    def _store_oidc_config(self, db_session, admin_user, overrides=None):
        """Helper: store a complete OIDC configuration in the database."""
        defaults = {
            "oidc_enabled": ("true", "bool"),
            "oidc_server_url": ("https://keycloak.integration-test.com", "string"),
            "oidc_internal_url": ("http://keycloak:8080", "string"),
            "oidc_realm": ("integration-test", "string"),
            "oidc_client_id": ("test-app", "string"),
            "oidc_callback_url": ("http://localhost:5173/api/auth/keycloak/callback", "string"),
            "oidc_admin_role": ("realm-admin", "string"),
            "oidc_timeout": ("25", "int"),
            "oidc_use_pkce": ("true", "bool"),
            "oidc_verify_issuer": ("true", "bool"),
            "oidc_verify_audience": ("false", "bool"),
            "oidc_audience": ("test-audience", "string"),
        }
        if overrides:
            defaults.update(overrides)

        for key, (value, dtype) in defaults.items():
            is_sensitive = key in AuthConfigService.SENSITIVE_KEYS
            existing = db_session.query(AuthConfig).filter(AuthConfig.config_key == key).first()
            if existing:
                existing.config_value = value
                existing.data_type = dtype
            else:
                db_session.add(
                    AuthConfig(
                        config_key=key,
                        config_value=value,
                        category="oidc",
                        data_type=dtype,
                        is_sensitive=is_sensitive,
                    )
                )
        db_session.commit()

    def test_from_db_reads_all_fields(self, db_session, admin_user):
        """OIDCConfig.from_db() populates all fields from database."""
        from app.auth.oidc import OIDCConfig

        self._store_oidc_config(db_session, admin_user)

        config = OIDCConfig.from_db(db_session)

        assert config.enabled is True
        assert config.server_url == "https://keycloak.integration-test.com"
        assert config.internal_url == "http://keycloak:8080"
        assert config.realm == "integration-test"
        assert config.client_id == "test-app"
        assert config.callback_url == "http://localhost:5173/api/auth/keycloak/callback"
        assert config.admin_role == "realm-admin"
        assert config.timeout == 25
        assert config.use_pkce is True
        assert config.verify_issuer is True
        assert config.verify_audience is False
        assert config.audience == "test-audience"

    def test_from_db_bool_conversion(self, db_session, admin_user):
        """OIDCConfig.from_db() correctly converts boolean values."""
        from app.auth.oidc import OIDCConfig

        self._store_oidc_config(
            db_session,
            admin_user,
            overrides={
                "oidc_enabled": ("false", "bool"),
                "oidc_use_pkce": ("false", "bool"),
                "oidc_verify_issuer": ("false", "bool"),
                "oidc_verify_audience": ("true", "bool"),
            },
        )

        config = OIDCConfig.from_db(db_session)

        assert config.enabled is False
        assert config.use_pkce is False
        assert config.verify_issuer is False
        assert config.verify_audience is True

    def test_from_db_int_conversion(self, db_session, admin_user):
        """OIDCConfig.from_db() correctly converts integer values."""
        from app.auth.oidc import OIDCConfig

        self._store_oidc_config(
            db_session,
            admin_user,
            overrides={"oidc_timeout": ("60", "int")},
        )

        config = OIDCConfig.from_db(db_session)
        assert config.timeout == 60

    def test_from_db_is_frozen(self, db_session, admin_user):
        """OIDCConfig is immutable (frozen dataclass)."""
        from app.auth.oidc import OIDCConfig

        self._store_oidc_config(db_session, admin_user)
        config = OIDCConfig.from_db(db_session)

        with pytest.raises(AttributeError):
            config.server_url = "https://mutated.example.com"  # type: ignore[misc]  # Testing immutability


# ===== Config Status Tests =====


class TestConfigStatusIntegration:
    """Test get_config_status with real database."""

    def test_config_status_reflects_db_state(self, db_session, admin_user):
        """get_config_status returns correct booleans from database state."""
        # Enable LDAP, disable OIDC and PKI
        for key, value in [
            ("ldap_enabled", "true"),
            ("oidc_enabled", "false"),
            ("pki_enabled", "false"),
            ("mfa_enabled", "true"),
        ]:
            existing = db_session.query(AuthConfig).filter(AuthConfig.config_key == key).first()
            if existing:
                existing.config_value = value
            else:
                db_session.add(
                    AuthConfig(
                        config_key=key,
                        config_value=value,
                        category=key.split("_")[0],
                        data_type="bool",
                        is_sensitive=False,
                    )
                )
        db_session.commit()

        status = AuthConfigService.get_config_status(db_session)

        assert status["ldap_enabled"] is True
        assert status["oidc_enabled"] is False
        assert status["pki_enabled"] is False
        assert status["mfa_enabled"] is True
