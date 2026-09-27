"""Issue #1029 — a hardened deployment must not default CORS_ORIGINS to the Vite dev origins.

``CORS_ORIGINS`` defaulted to ``http://localhost:5173`` and ``http://127.0.0.1:5173`` in every
environment, and ``main.py`` hands it to ``CORSMiddleware`` with ``allow_credentials=True``. A
production deployment that never set it therefore granted credentialed cross-origin reads to
any page served on those ports on a user's machine (a hostile dev server, a compromised npm
package) — while its own origin was not on the list at all. Same-origin needs no CORS, and the
WebSocket origin gate already admits same-origin handshakes without it.
"""

from __future__ import annotations

import logging

import pytest

from app.core.config import Settings
from app.core.config import settings

pytestmark = pytest.mark.unit

#: Spelled out rather than imported, so this file also runs (and fails) against the old code.
VITE_DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


@pytest.fixture(autouse=True)
def _no_cors_env(monkeypatch):
    """The value under test is the DEFAULT, so no ambient CORS_ORIGINS may leak in."""
    monkeypatch.delenv("CORS_ORIGINS", raising=False)


@pytest.mark.parametrize("environment", ["production", "prod", "staging", "", "Productoin"])
def test_hardened_environment_without_cors_origins_defaults_to_empty(environment: str):
    """RED before the fix: this returned the two Vite dev origins."""
    assert Settings(ENVIRONMENT=environment).CORS_ORIGINS == []


@pytest.mark.parametrize("environment", ["development", "dev", "testing", "test", "local"])
def test_relaxed_environment_keeps_the_vite_dev_default(environment: str):
    """The dev stack serves the SPA from Vite on :5173 and still needs these."""
    assert Settings(ENVIRONMENT=environment).CORS_ORIGINS == VITE_DEV_ORIGINS


def test_explicit_origins_are_honoured_when_hardened(monkeypatch):
    """A separate-origin frontend is still configurable — the env value wins."""
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com, https://admin.example.com")
    assert Settings(ENVIRONMENT="production").CORS_ORIGINS == [
        "https://app.example.com",
        "https://admin.example.com",
    ]


def test_json_list_form_is_accepted(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", '["https://app.example.com"]')
    assert Settings(ENVIRONMENT="production").CORS_ORIGINS == ["https://app.example.com"]


def test_explicit_dev_origins_are_honoured_when_hardened(monkeypatch):
    """Opting back in to the dev origins is a choice the operator can still make."""
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173")
    assert Settings(ENVIRONMENT="production").CORS_ORIGINS == ["http://localhost:5173"]


def test_explicit_empty_list_is_honoured_when_relaxed(monkeypatch):
    """`[]` is a value, not "unset" — it must not be replaced by the dev default."""
    monkeypatch.setenv("CORS_ORIGINS", "[]")
    assert Settings(ENVIRONMENT="development").CORS_ORIGINS == []


def test_blank_env_value_means_unset(monkeypatch):
    """`.env.example`'s `VAR=` convention: blank falls through to the environment default."""
    monkeypatch.setenv("CORS_ORIGINS", "")
    assert Settings(ENVIRONMENT="production").CORS_ORIGINS == []
    assert Settings(ENVIRONMENT="development").CORS_ORIGINS == VITE_DEV_ORIGINS


def test_startup_logs_the_effective_origin_list(monkeypatch, caplog):
    """The default now depends on ENVIRONMENT, so the resolved list is stated at boot."""
    from app.main import _validate_production_secrets

    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["https://app.example.com"])
    with caplog.at_level(logging.INFO, logger="app.main"):
        _validate_production_secrets()
    lines = [r.getMessage() for r in caplog.records if "CORS allowed origins" in r.getMessage()]
    assert lines == [
        "CORS allowed origins: ['https://app.example.com'] (same-origin requests need no entry)"
    ]


def test_startup_log_names_an_empty_list_explicitly(monkeypatch, caplog):
    from app.main import _validate_production_secrets

    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "CORS_ORIGINS", [])
    with caplog.at_level(logging.INFO, logger="app.main"):
        _validate_production_secrets()
    lines = [r.getMessage() for r in caplog.records if "CORS allowed origins" in r.getMessage()]
    assert lines == ["CORS allowed origins: none (same-origin requests need no entry)"]
