"""TOTP replay protection and registration gating (#284 A0.13 / A0.11).

A0.13 — `verify_totp` never tracked used codes, so a valid code stayed valid for the
whole 30s step plus the drift window (RFC 6238 §5.2 requires single use). A code
observed in transit could be replayed inside that envelope.

A0.11 — `/register` was the only auth route with no rate limiter, while creating
accounts that are immediately active and GPU-capable.
"""

from __future__ import annotations

import inspect

import pytest

from app.auth.mfa import MFAService


class _FakeRedis:
    """Records SET NX calls the way Redis would."""

    def __init__(self):
        self.keys: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    import app.core.redis as redis_module

    monkeypatch.setattr(redis_module, "get_redis", lambda: fake)
    return fake


def test_totp_code_is_single_use(fake_redis):
    """The core A0.13 fix: the same code must not verify twice."""
    assert MFAService._consume_totp_code(42, "123456") is True
    assert MFAService._consume_totp_code(42, "123456") is False


def test_totp_codes_are_scoped_per_user(fake_redis):
    """One user consuming a code must not lock another user out of the same digits."""
    assert MFAService._consume_totp_code(1, "123456") is True
    assert MFAService._consume_totp_code(2, "123456") is True


def test_distinct_codes_for_same_user_both_succeed(fake_redis):
    assert MFAService._consume_totp_code(7, "111111") is True
    assert MFAService._consume_totp_code(7, "222222") is True


def test_ttl_covers_the_full_acceptance_window(fake_redis, monkeypatch):
    """The claim must outlive the drift window, or a code becomes replayable inside it."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "TOTP_VALID_WINDOW", 1)
    captured = {}

    def capture(key, value, nx=False, ex=None):
        captured["ex"] = ex
        return True

    fake_redis.set = capture
    MFAService._consume_totp_code(1, "123456")

    assert captured["ex"] >= MFAService.TOTP_INTERVAL * 3


def test_redis_failure_fails_open_by_default(monkeypatch):
    """Self-host must keep working if Redis is down."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "MFA_REQUIRE_REDIS", False)
    import app.core.redis as redis_module

    def boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_module, "get_redis", boom)
    assert MFAService._consume_totp_code(1, "123456") is True


def test_redis_failure_fails_closed_when_required(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "MFA_REQUIRE_REDIS", True)
    import app.core.redis as redis_module

    def boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_module, "get_redis", boom)
    assert MFAService._consume_totp_code(1, "123456") is False


def test_verify_totp_without_user_id_does_not_consume(fake_redis):
    """Back-compat: the consume step only engages when a user is supplied."""
    assert MFAService.verify_totp("BADSECRET", "000000") is False
    assert fake_redis.keys == {}


def _auth_package_source() -> str:
    """Concatenated source of every module in the ``auth`` endpoint package."""
    from pathlib import Path

    from app.api.endpoints import auth as auth_package

    return "\n".join(p.read_text() for p in sorted(Path(auth_package.__file__).parent.glob("*.py")))


def test_every_auth_call_site_passes_user_id():
    """A call site that forgets user_id silently loses replay protection."""
    source = _auth_package_source()
    calls = source.count("MFAService.verify_totp(")
    with_user = source.count("user_id=")

    assert calls > 0
    assert with_user >= calls, "every verify_totp call must pass user_id"


# ── A0.11 registration ───────────────────────────────────────────────────────────


def test_register_is_rate_limited():
    source = _auth_package_source()
    register_at = source.index('@router.post("/register"')
    window = source[register_at : register_at + 400]

    assert "@limiter.limit" in window, (
        "/register must carry a rate limiter like every other auth route"
    )


def test_open_registration_can_be_disabled():
    """The gate is DB-backed with the env var as fallback.

    It used to read ``settings.ALLOW_OPEN_REGISTRATION`` directly, which meant the
    admin UI's self-registration toggle wrote a DB key nothing consumed — an LDAP
    deployment reported users could still self-register with the switch off (#354).
    The endpoint now goes through ``get_auth_settings(db).allow_registration``,
    which resolves DB > env > default.
    """
    from app.api.endpoints import auth as auth_module
    from app.core.auth_settings import DynamicAuthSettings
    from app.core.config import settings

    assert hasattr(settings, "ALLOW_OPEN_REGISTRATION")
    source = inspect.getsource(auth_module.register)
    assert "allow_registration" in source
    assert "settings.ALLOW_OPEN_REGISTRATION" not in source, (
        "the endpoint must not bypass the DB-backed setting"
    )
    # ...and the env var must still be the fallback the property reads.
    assert "ALLOW_OPEN_REGISTRATION" in inspect.getsource(
        DynamicAuthSettings.allow_registration.fget
    )


def test_allow_registration_env_var_is_mapped_for_migration():
    """``ENV_TO_CONFIG_MAPPING`` is what lets an env value seed the DB key.

    Its absence is the reason the admin toggle could never take effect.
    """
    from app.services.auth_config_service import AuthConfigService

    assert AuthConfigService.ENV_TO_CONFIG_MAPPING["ALLOW_OPEN_REGISTRATION"] == (
        "allow_registration"
    )
    assert AuthConfigService.env_var_for("allow_registration") == "ALLOW_OPEN_REGISTRATION"


def test_open_registration_defaults_on_for_self_host():
    """Self-host expects to create the first account through the UI."""
    from app.core.config import Settings

    assert Settings(_env_file=None).ALLOW_OPEN_REGISTRATION is True
