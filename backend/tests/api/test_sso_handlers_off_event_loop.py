"""SAML/OIDC handlers must not run synchronous DB/Redis work on the event loop (#997).

These handlers are ``async def`` (SAML must ``await request.form()``; OIDC awaits its
outbound HTTP calls), so FastAPI does NOT move them to the threadpool the way it does a
plain ``def`` handler. Every synchronous call they make — ``SAMLConfig.from_db`` alone
is ~19 queries — therefore blocked the one event loop serving every other request.

The probe: a synchronous stand-in that records whether it was called with an asyncio
loop running in its thread. On the event loop there is one; in a threadpool worker
there is not.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.endpoints.auth import oidc as oidc_module
from app.api.endpoints.auth import saml as saml_module
from app.auth.cookies import OIDC_STATE_COOKIE


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _Probe:
    """Records, per named call, whether it ran on the event loop."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, name: str, result=None, exc: Exception | None = None):
        def _fn(*args, **kwargs):
            self.calls.append((name, _on_event_loop()))
            if exc is not None:
                raise exc
            return result

        return _fn

    def reached(self) -> set[str]:
        return {name for name, _ in self.calls}

    def ran_on_loop(self) -> list[str]:
        return [name for name, on_loop in self.calls if on_loop]


@pytest.fixture
def probe() -> _Probe:
    return _Probe()


def _patch_config(monkeypatch, config_cls, probe: _Probe, cfg) -> None:
    fn = probe("from_db", cfg)
    monkeypatch.setattr(config_cls, "from_db", classmethod(lambda cls, db: fn(db)))


class _FakeSamlAuth:
    def process_response(self) -> None:
        return None

    def get_errors(self) -> list:
        return []

    def is_authenticated(self) -> bool:
        return True

    def get_last_error_reason(self) -> str:
        return ""


class TestSaml:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/auth/saml/login"),
            ("post", "/api/auth/saml/acs"),
            ("get", "/api/auth/saml/sls"),
        ],
    )
    def test_config_load_is_off_the_event_loop(self, client, monkeypatch, probe, method, path):
        _patch_config(monkeypatch, saml_module.SAMLConfig, probe, SimpleNamespace(enabled=False))
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code == 400
        assert probe.reached() == {"from_db"}
        assert probe.ran_on_loop() == []

    def test_acs_user_sync_is_off_the_event_loop(self, client, monkeypatch, probe):
        _patch_config(monkeypatch, saml_module.SAMLConfig, probe, SimpleNamespace(enabled=True))
        monkeypatch.setattr(saml_module, "build_auth", lambda request_data, cfg: _FakeSamlAuth())
        monkeypatch.setattr(
            saml_module,
            "extract_saml_user_data",
            lambda auth, cfg: {"saml_subject": "subject-1", "email": "sso@example.com"},
        )
        monkeypatch.setattr(
            saml_module,
            "sync_saml_user_to_db",
            probe("sync_user", exc=HTTPException(status_code=401, detail="refused")),
        )
        response = client.post("/api/auth/saml/acs", data={"SAMLResponse": "x"})
        assert response.status_code == 401
        assert probe.reached() == {"from_db", "sync_user"}
        assert probe.ran_on_loop() == []


class TestOidcCallback:
    def test_config_state_and_audit_are_off_the_event_loop(self, client, monkeypatch, probe):
        _patch_config(monkeypatch, oidc_module.OIDCConfig, probe, SimpleNamespace(enabled=True))
        monkeypatch.setattr(oidc_module._oidc_state_store, "get_state", probe("get_state"))
        monkeypatch.setattr(oidc_module.audit_logger, "log_login_failure", probe("audit"))
        response = client.get("/api/auth/oidc/callback", params={"code": "c", "state": "s"})
        assert response.status_code == 400
        assert probe.reached() == {"from_db", "get_state", "audit"}
        assert probe.ran_on_loop() == []

    def test_user_sync_is_off_the_event_loop(self, client, monkeypatch, probe):
        """Drives the real state store and binding cookie up to the user sync."""
        _patch_config(monkeypatch, oidc_module.OIDCConfig, probe, SimpleNamespace(enabled=True))
        state = "state-off-loop"
        oidc_module._oidc_state_store.store_state(
            state=state,
            data={"binding": oidc_module._hash_binding("binding-secret")},
            expires_seconds=60,
        )
        client.cookies.set(OIDC_STATE_COOKIE, "binding-secret")

        async def _exchange(code, verifier, cfg):
            return SimpleNamespace(access_token="a", id_token="i", refresh_token=None)

        async def _validate(access_token, cfg, id_token):
            return {"oidc_subject": "s", "email": "sso@example.com"}

        monkeypatch.setattr(oidc_module, "exchange_code_for_tokens", _exchange)
        monkeypatch.setattr(oidc_module, "validate_oidc_token", _validate)
        monkeypatch.setattr(
            oidc_module,
            "sync_oidc_user_to_db",
            probe("sync_user", exc=HTTPException(status_code=401, detail="refused")),
        )
        response = client.get("/api/auth/oidc/callback", params={"code": "c", "state": state})
        assert response.status_code == 401
        assert probe.reached() == {"from_db", "sync_user"}
        assert probe.ran_on_loop() == []
