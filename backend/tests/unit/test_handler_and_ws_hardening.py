"""Handler correctness, WebSocket auth, and startup assertions (#284 A0.6/A0.7/A0.8).

A0.6 — `clear_video_cache` and `refresh_analytics` wrapped their ownership lookup in a
broad `except Exception`, so a legitimate 403/404 from `get_media_file_by_uuid` was
re-wrapped as a 500 (hiding the authz result) and the handler then referenced the
still-unassigned `file_id` in its own log line, crashing a second time with `NameError`.

A0.7 — the WebSocket local-JWT path skipped the `is_active` and token-revocation checks
that the HTTP path performs, so a revoked token or a deactivated account still opened a
socket and kept receiving that user's events.

A0.8 — `TESTING=true` makes `get_current_user` fabricate a user from the token UUID when
the DB lookup fails; outside tests that is an authentication bypass.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING
from typing import cast

import pytest

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.user import User

# ── A0.6: authorization must not be swallowed ────────────────────────────────────


@pytest.mark.parametrize("func_name", ["clear_video_cache", "refresh_analytics"])
def test_ownership_lookup_is_outside_the_try(func_name):
    """The lookup must precede the try, so its HTTPException propagates untouched."""
    from app.api.endpoints import files as files_module

    source = inspect.getsource(getattr(files_module, func_name))
    lookup_at = source.index("get_media_file_by_uuid(")
    try_at = source.index("\n    try:")

    assert lookup_at < try_at, (
        f"{func_name}: the ownership lookup is still inside the try block, so a 403/404 "
        "will be re-wrapped as a 500"
    )


@pytest.mark.parametrize("func_name", ["clear_video_cache", "refresh_analytics"])
def test_http_exceptions_are_re_raised(func_name):
    from app.api.endpoints import files as files_module

    source = inspect.getsource(getattr(files_module, func_name))
    assert "except HTTPException:" in source, f"{func_name} must re-raise HTTPException"


@pytest.mark.parametrize("func_name", ["clear_video_cache", "refresh_analytics"])
def test_file_id_is_assigned_before_the_handler_can_log_it(func_name):
    """The NameError half: file_id was logged in a handler that could run before it existed."""
    from app.api.endpoints import files as files_module

    source = inspect.getsource(getattr(files_module, func_name))
    assign_at = source.index("file_id = db_file.id")
    handler_at = source.index("\n    except Exception as e:")

    assert assign_at < handler_at


@pytest.mark.parametrize("func_name", ["clear_video_cache", "refresh_analytics"])
def test_internal_error_detail_does_not_leak_exception_text(func_name):
    """500 bodies should not echo str(e) back to the caller."""
    from app.api.endpoints import files as files_module

    source = inspect.getsource(getattr(files_module, func_name))
    assert "{str(e)}" not in source, f"{func_name} leaks raw exception text in its 500 detail"


# ── A0.7: WebSocket auth parity with HTTP ────────────────────────────────────────


def test_websocket_auth_checks_is_active_and_revocation():
    from app.api import websockets as ws_module

    source = inspect.getsource(ws_module._try_authenticate_token)

    assert "is_token_revoked" in source, "WS auth must check the revocation blacklist"
    assert "is_active" in source, "WS auth must reject deactivated accounts"


def test_websocket_auth_returns_none_for_inactive_user(monkeypatch):
    """A deactivated account must not open a socket even with a valid token."""
    from types import SimpleNamespace

    from app.api import websockets as ws_module

    inactive = SimpleNamespace(is_active=False, uuid="u-1")

    class _Query:
        def filter(self, *a, **k):
            return self

        def first(self):
            return inactive

    db = cast("Session", SimpleNamespace(query=lambda *a, **k: _Query()))

    monkeypatch.setattr(ws_module, "verify_token", lambda t: {"sub": "u-1", "jti": None})
    import app.auth.provider_registry as _registry

    monkeypatch.setattr(_registry, "has_verifiers", lambda: False)

    assert ws_module._try_authenticate_token("tok", db) is None


def test_websocket_auth_returns_none_for_revoked_token(monkeypatch):
    from types import SimpleNamespace

    from app.api import websockets as ws_module
    from app.core.config import settings

    active = SimpleNamespace(is_active=True, uuid="u-1")

    class _Query:
        def filter(self, *a, **k):
            return self

        def first(self):
            return active

    db = cast("Session", SimpleNamespace(query=lambda *a, **k: _Query()))

    monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", True)
    monkeypatch.setattr(ws_module, "verify_token", lambda t: {"sub": "u-1", "jti": "abcd1234"})
    import app.auth.provider_registry as _registry

    monkeypatch.setattr(_registry, "has_verifiers", lambda: False)
    monkeypatch.setattr(ws_module.token_service, "is_token_revoked", lambda jti, **_kw: True)

    assert ws_module._try_authenticate_token("tok", db) is None


def test_websocket_auth_accepts_active_user_with_valid_token(monkeypatch):
    from types import SimpleNamespace

    from app.api import websockets as ws_module
    from app.core.config import settings

    active = SimpleNamespace(is_active=True, uuid="u-1")

    class _Query:
        def filter(self, *a, **k):
            return self

        def first(self):
            return active

    db = cast("Session", SimpleNamespace(query=lambda *a, **k: _Query()))

    monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", True)
    monkeypatch.setattr(ws_module, "verify_token", lambda t: {"sub": "u-1", "jti": "abcd1234"})
    import app.auth.provider_registry as _registry

    monkeypatch.setattr(_registry, "has_verifiers", lambda: False)
    monkeypatch.setattr(ws_module.token_service, "is_token_revoked", lambda jti, **_kw: False)

    assert ws_module._try_authenticate_token("tok", db) is cast("User", active)


# ── A0.8: startup assertions ─────────────────────────────────────────────────────


def test_testing_shortcuts_are_inert_when_hardened(monkeypatch):
    """TESTING=true enables a mock-user auth fallback; is_hardened must disable it.

    Enforcement is at the USE site rather than a boot refusal, because the secrets-guard
    suite legitimately simulates production while conftest sets TESTING process-wide.
    """
    from pathlib import Path

    from app.api.endpoints import auth as auth_package

    # auth is a package, so scan every module in it rather than __init__ alone.
    source = "\n".join(
        p.read_text() for p in sorted(Path(auth_package.__file__).parent.glob("*.py"))
    )
    testing_gates = source.count('os.environ.get("TESTING", "False").lower() == "true"')
    guarded = source.count("and not settings.is_hardened")

    assert testing_gates > 0, "TESTING gate not found — did it move?"
    assert guarded >= testing_gates, (
        "every TESTING auth shortcut must also require `not settings.is_hardened`"
    )


def test_hardened_refuses_wildcard_cors_with_credentials(monkeypatch):
    from app.core.config import settings
    from app.main import _validate_production_secrets

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "a-real-and-sufficiently-long-secret")
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", "a-real-and-sufficiently-long-key")
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "redis-pass")
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setenv("TESTING", "False")
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["https://app.example.com", "*"])

    with pytest.raises(ValueError, match="CORS"):
        _validate_production_secrets()


def test_development_tolerates_testing_flag_and_wildcard(monkeypatch):
    """The test suite itself sets TESTING=true — dev must not refuse to boot."""
    from app.core.config import settings
    from app.main import _validate_production_secrets

    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["*"])
    monkeypatch.setenv("TESTING", "true")

    _validate_production_secrets()  # must not raise
