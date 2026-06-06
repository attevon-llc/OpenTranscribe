"""Tests for the production startup secret validation guard.

Regression coverage for the placeholder-key bypass: the guard rejected known
weak defaults but not the .env.example placeholder
(``CHANGE_ME_auto_generated_on_install``), so a hand-copied .env could boot
production with publicly-known JWT/encryption keys.
"""

import pytest

from app.core.config import settings
from app.main import _validate_production_secrets

STRONG_JWT = "a" * 128
STRONG_ENCRYPTION = "opentranscribe_" + "b" * 64
PLACEHOLDER = "CHANGE_ME_auto_generated_on_install"


@pytest.fixture
def production_settings(monkeypatch):
    """Baseline settings that pass production validation."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", STRONG_JWT)
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", STRONG_ENCRYPTION)
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "strong-redis-password")
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "KEYCLOAK_ENABLED", False)
    monkeypatch.setattr(settings, "PKI_ENABLED", False)
    monkeypatch.setattr(settings, "MINIO_PUBLIC_URL", "")
    return monkeypatch


def test_strong_secrets_pass(production_settings):
    _validate_production_secrets()  # must not raise


def test_env_example_placeholder_jwt_rejected(production_settings):
    production_settings.setattr(settings, "JWT_SECRET_KEY", PLACEHOLDER)
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        _validate_production_secrets()


def test_env_example_placeholder_encryption_key_rejected(production_settings):
    production_settings.setattr(settings, "ENCRYPTION_KEY", PLACEHOLDER)
    with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
        _validate_production_secrets()


@pytest.mark.parametrize(
    "weak_jwt",
    ["this_should_be_changed_in_production", "changeme", "secret", "your-secret-key", "CHANGEME"],
)
def test_known_weak_jwt_defaults_rejected(production_settings, weak_jwt):
    production_settings.setattr(settings, "JWT_SECRET_KEY", weak_jwt)
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        _validate_production_secrets()


def test_legacy_default_encryption_key_rejected(production_settings):
    production_settings.setattr(
        settings, "ENCRYPTION_KEY", "this_should_be_changed_in_production_for_api_key_encryption"
    )
    with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
        _validate_production_secrets()


def test_missing_redis_password_rejected(production_settings):
    production_settings.setattr(settings, "REDIS_PASSWORD", "")
    with pytest.raises(ValueError, match="REDIS_PASSWORD"):
        _validate_production_secrets()


def test_debug_in_production_rejected(production_settings):
    production_settings.setattr(settings, "DEBUG", True)
    with pytest.raises(ValueError, match="DEBUG"):
        _validate_production_secrets()


def test_placeholders_allowed_in_development(production_settings):
    """The guard only enforces in production - dev/test stay bootable."""
    production_settings.setattr(settings, "ENVIRONMENT", "development")
    production_settings.setattr(settings, "JWT_SECRET_KEY", PLACEHOLDER)
    production_settings.setattr(settings, "ENCRYPTION_KEY", PLACEHOLDER)
    production_settings.setattr(settings, "REDIS_PASSWORD", "")
    _validate_production_secrets()  # must not raise
