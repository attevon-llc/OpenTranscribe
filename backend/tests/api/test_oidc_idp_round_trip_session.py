"""The OIDC handlers hold no database transaction while they wait for the provider.

``oidc_login`` (discovery) and ``oidc_callback`` (code exchange, ID-token validation)
read their config from the database and then awaited the identity provider with that
read transaction still open, i.e. a pooled connection idle in transaction for as long as
the provider took. The request's real session is observed at the moment of each
provider call.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import status
from starlette.testclient import TestClient

import app.api.endpoints.auth.oidc as oidc_module
from app.auth.oidc import OIDCConfig
from app.db.base import SessionLocal
from app.db.base import get_db
from app.main import app


@pytest.fixture
def observed_session():
    """Serve every request a real session and keep a handle on it."""
    sessions = []

    def _get_db():
        db = SessionLocal()
        sessions.append(db)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    yield sessions
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def oidc_enabled(monkeypatch):
    real = OIDCConfig.from_db

    def _enabled(db):
        # The real read (it opens the transaction under test), with OIDC switched on.
        return dataclasses.replace(real(db), enabled=True)

    # A plain function on the class: ``OIDCConfig.from_db(db)`` calls it unbound.
    monkeypatch.setattr(OIDCConfig, "from_db", _enabled)


def test_login_holds_no_transaction_during_discovery(observed_session, oidc_enabled, monkeypatch):
    seen: list[bool] = []

    async def _authorization_url(state, cfg):
        seen.append(observed_session[0].in_transaction())
        return "https://idp.example.com/authorize", "verifier"

    monkeypatch.setattr(oidc_module, "get_authorization_url", _authorization_url)
    monkeypatch.setattr(oidc_module._oidc_state_store, "store_state", lambda **_k: True)

    response = TestClient(app).get("/api/auth/oidc/login")

    assert response.status_code == status.HTTP_200_OK, response.text
    assert seen == [False]


def test_callback_holds_no_transaction_during_the_code_exchange(
    observed_session, oidc_enabled, monkeypatch
):
    seen: list[bool] = []

    async def _exchange(code, code_verifier, cfg):
        seen.append(observed_session[0].in_transaction())
        return None

    monkeypatch.setattr(oidc_module, "exchange_code_for_tokens", _exchange)
    monkeypatch.setattr(
        oidc_module._oidc_state_store,
        "get_state",
        lambda state: {"binding": oidc_module._hash_binding("binding"), "code_verifier": "v"},
    )
    monkeypatch.setattr(oidc_module, "get_oidc_state_binding", lambda request: "binding")
    monkeypatch.setattr(oidc_module, "_record_refusal", lambda *a, **k: None)

    response = TestClient(app).get("/api/auth/oidc/callback?code=c&state=s")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.text
    assert seen == [False]
