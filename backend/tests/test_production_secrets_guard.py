"""Tests for the production startup secret validation guard.

Regression coverage for the placeholder-key bypass: the guard rejected known
weak defaults but not the .env.example placeholder
(``CHANGE_ME_auto_generated_on_install``), so a hand-copied .env could boot
production with publicly-known JWT/encryption keys.
"""

import pytest

from app.core.config import settings
from app.main import _validate_production_secrets
from tests.helpers import does_not_raise

STRONG_JWT = "a" * 128
STRONG_ENCRYPTION = "opentranscribe_" + "b" * 64
PLACEHOLDER = "CHANGE_ME_auto_generated_on_install"


@pytest.fixture
def production_settings(monkeypatch):
    """Baseline settings that pass production validation.

    ``FIPS_MODE`` is published explicitly, like every other flag here, rather than
    inherited from the process. ``run-integration-tests.sh`` runs part of the suite with
    ``FIPS_MODE=true``, and under that profile ``_validate_production_secrets`` also
    validates key-material entropy — which ``STRONG_JWT``/``STRONG_ENCRYPTION`` (a repeated
    character padded to length) correctly fail. This file exercises the ``is_hardened``
    branch; the FIPS branch has its own red/green coverage in ``test_fips_140_3.py``.
    """
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "FIPS_MODE", False)
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", STRONG_JWT)
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", STRONG_ENCRYPTION)
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "strong-redis-password")
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "OIDC_ENABLED", False)
    monkeypatch.setattr(settings, "PKI_ENABLED", False)
    monkeypatch.setattr(settings, "MINIO_PUBLIC_URL", "")
    return monkeypatch


def test_strong_secrets_pass(production_settings):
    """The baseline passes. Written as an explicit non-raise, not a bare call.

    ``pytest.raises`` is the only assertion the other tests here need, so this one had no
    assertion at all and read as an empty test. Failing on the exception with its message
    attached says what "must not raise" actually means.
    """
    try:
        _validate_production_secrets()
    except ValueError as exc:  # pragma: no cover - only on a real regression
        pytest.fail(f"baseline production settings must pass validation, got: {exc}")


def test_wildcard_cors_rejected_in_production(production_settings):
    """A wildcard CORS origin must refuse to boot production.

    ``allow_credentials=True`` plus ``*`` would let any site read authenticated responses
    (issue #284 A0.8), so ``_validate_production_secrets`` raises rather than starting. This
    control had NO coverage: the only test naming it asserted
    ``if hasattr(settings, "CORS_ORIGINS"): assert "*" not in settings.CORS_ORIGINS`` against
    the *testing* config, which never exercised the production path and passed vacuously if
    the attribute were ever renamed (issue #431).
    """
    production_settings.setattr(settings, "CORS_ORIGINS", ["*"])
    with pytest.raises(ValueError, match="CORS"):
        _validate_production_secrets()


def test_explicit_cors_origins_pass_in_production(production_settings):
    """The control must not fire on a legitimate explicit origin list."""
    production_settings.setattr(settings, "CORS_ORIGINS", ["https://app.example.com"])
    try:
        _validate_production_secrets()
    except ValueError as exc:  # pragma: no cover - only on a real regression
        pytest.fail(f"explicit CORS origins must be accepted, got: {exc}")


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
    with does_not_raise("placeholders are allowed outside production, so validation must pass"):
        _validate_production_secrets()  # must not raise
