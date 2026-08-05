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


def test_every_auth_call_site_passes_user_id():
    """A call site that forgets user_id silently loses replay protection."""
    from app.api.endpoints import auth as auth_module

    source = inspect.getsource(auth_module)
    calls = source.count("MFAService.verify_totp(")
    with_user = source.count("user_id=")

    assert calls > 0
    assert with_user >= calls, "every verify_totp call must pass user_id"


# ── A0.11 registration ───────────────────────────────────────────────────────────


def test_register_is_rate_limited():
    from app.api.endpoints import auth as auth_module

    source = inspect.getsource(auth_module)
    register_at = source.index('@router.post("/register"')
    window = source[register_at : register_at + 400]

    assert "@limiter.limit" in window, (
        "/register must carry a rate limiter like every other auth route"
    )


def test_open_registration_can_be_disabled():
    from app.api.endpoints import auth as auth_module
    from app.core.config import settings

    assert hasattr(settings, "ALLOW_OPEN_REGISTRATION")
    assert "ALLOW_OPEN_REGISTRATION" in inspect.getsource(auth_module.register)


def test_open_registration_defaults_on_for_self_host():
    """Self-host expects to create the first account through the UI."""
    from app.core.config import Settings

    assert Settings(_env_file=None).ALLOW_OPEN_REGISTRATION is True
