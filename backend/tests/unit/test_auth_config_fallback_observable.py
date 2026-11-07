"""An auth-config read that falls back to ``.env`` is logged and counted every time.

The fallback itself is deliberate: a database hiccup must not turn every login into a 500.
But the ``.env`` value used instead may be weaker than what an admin set in the UI, so each
fallback writes a WARNING naming the key and increments
``security_state_degraded_total{control="auth_config", fallback="env"}``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.api.endpoints.auth import lifecycle_gates
from app.auth import session_ceiling
from app.core import auth_settings as auth_settings_module
from app.core import metrics as metrics_module
from app.core import process_auth_settings
from app.core.config import settings
from app.services.auth_config_service import AuthConfigService

pytestmark = pytest.mark.unit


class _Counter:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def labels(self, **kwargs: str) -> _Counter:
        self.calls.append(kwargs)
        return self

    def inc(self) -> None:
        pass


class _BrokenSession(Session):
    """A session whose every query fails, like a database that just went away."""

    def query(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database unavailable")


@pytest.fixture
def counter(monkeypatch) -> _Counter:
    fake = _Counter()
    monkeypatch.setattr(metrics_module, "security_state_degraded_total", fake)
    return fake


def _fallback_warnings(caplog, key: str) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and f"auth config '{key}'" in r.getMessage()
    ]


def test_request_scoped_read_falls_back_loudly(monkeypatch, caplog, counter):
    def _boom(db, key):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(AuthConfigService, "get_effective_config", staticmethod(_boom))
    loader = auth_settings_module.DynamicAuthSettings(db=_BrokenSession())

    with caplog.at_level(logging.WARNING):
        minimum = loader.password_min_length
        lockout = loader.account_lockout_threshold

    assert minimum == settings.PASSWORD_MIN_LENGTH
    assert lockout == settings.ACCOUNT_LOCKOUT_THRESHOLD
    assert counter.calls == [{"control": "auth_config", "fallback": "env"}] * 2
    assert _fallback_warnings(caplog, "password_min_length")
    assert _fallback_warnings(caplog, "account_lockout_threshold")


def test_a_successful_read_is_not_counted(monkeypatch, counter):
    monkeypatch.setattr(AuthConfigService, "get_effective_config", staticmethod(lambda db, key: 99))
    loader = auth_settings_module.DynamicAuthSettings(db=_BrokenSession())

    assert loader.password_min_length == 99
    assert counter.calls == []


def test_process_level_read_falls_back_loudly(monkeypatch, caplog, counter):
    monkeypatch.setenv("TESTING", "false")

    def _no_session():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.db.base.SessionLocal", _no_session)

    with caplog.at_level(logging.WARNING):
        value = process_auth_settings._read_effective_from_own_session("password_min_length")

    assert value is None
    assert counter.calls == [{"control": "auth_config", "fallback": "env"}]
    assert _fallback_warnings(caplog, "password_min_length")


def test_session_lifetime_falls_back_loudly(monkeypatch, caplog, counter):
    def _boom(db):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.core.auth_settings.get_auth_settings", _boom)

    with caplog.at_level(logging.WARNING):
        lifetimes = session_ceiling._session_lifetime_minutes(_BrokenSession())

    assert lifetimes == (
        settings.SESSION_IDLE_TIMEOUT_MINUTES,
        settings.SESSION_ABSOLUTE_TIMEOUT_MINUTES,
        settings.MAX_CONCURRENT_SESSIONS,
    )
    assert counter.calls == [{"control": "auth_config", "fallback": "env"}]
    assert _fallback_warnings(caplog, "session_lifetime")


def test_banner_read_falls_back_loudly(monkeypatch, caplog, counter):
    monkeypatch.setattr(lifecycle_gates, "_last_banner_read", None)

    with caplog.at_level(logging.WARNING):
        enabled, changed_at = lifecycle_gates._banner_requirement(_BrokenSession())

    assert (enabled, changed_at) == (settings.LOGIN_BANNER_ENABLED, None)
    assert counter.calls == [{"control": "auth_config", "fallback": "env"}]
    assert _fallback_warnings(caplog, "login_banner")
