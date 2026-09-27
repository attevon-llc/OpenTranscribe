"""``GET /api/auth/saml/metadata`` — disabled and misconfigured SAML (issue #998).

The metadata handler never checked ``cfg.enabled`` and handed python3-saml whatever
config it had. With SAML off that is an empty config, python3-saml raised
``OneLogin_Saml2_Error: Invalid dict settings ...``, nothing caught it, and every
anonymous hit was a 500 with a traceback in the logs.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api.endpoints.auth import saml as saml_module
from app.auth.saml.config import SAMLConfig
from app.db.base import get_db
from app.main import app

METADATA = "/api/auth/saml/metadata"


def _valid_cfg(**overrides) -> SAMLConfig:
    base: dict[str, object] = {
        "enabled": True,
        "sp_entity_id": "https://sp.example.com",
        "sp_acs_url": "https://sp.example.com/api/auth/saml/acs",
        "sp_sls_url": "https://sp.example.com/api/auth/saml/sls",
        "idp_entity_id": "https://idp.example.com",
        "idp_sso_url": "https://idp.example.com/sso",
        "idp_slo_url": "https://idp.example.com/slo",
        "idp_x509_cert": "MIICfakecert",
    }
    base.update(overrides)
    return SAMLConfig(**base)  # type: ignore[arg-type]


@pytest.fixture
def raw_client(db_session):
    """A client that reports a server error as a 500, as a real caller sees it,
    instead of re-raising it into the test."""

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False, follow_redirects=False) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _use_config(monkeypatch, cfg: SAMLConfig) -> None:
    monkeypatch.setattr(SAMLConfig, "from_db", classmethod(lambda cls, db: cfg))


def test_metadata_is_404_when_saml_is_disabled(raw_client, monkeypatch):
    _use_config(monkeypatch, SAMLConfig(enabled=False))
    response = raw_client.get(METADATA)
    assert response.status_code == 404, response.text


def test_metadata_is_404_with_the_real_default_config(raw_client):
    """No monkeypatching: a deployment that never configured SAML."""
    response = raw_client.get(METADATA)
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("path", [METADATA, "/api/auth/saml/login"])
def test_an_invalid_enabled_config_is_503_not_500(raw_client, monkeypatch, path):
    """Enabled, but python3-saml rejects the settings (here: no IdP SSO URL)."""
    _use_config(monkeypatch, _valid_cfg(idp_sso_url=""))
    response = raw_client.get(path)
    assert response.status_code == 503, response.text
    assert "OneLogin" not in response.text


def test_a_valid_config_still_serves_metadata(raw_client, monkeypatch):
    _use_config(monkeypatch, _valid_cfg())
    response = raw_client.get(METADATA)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/xml")
    assert "https://sp.example.com/api/auth/saml/acs" in response.text


def test_invalid_generated_metadata_is_503(raw_client, monkeypatch):
    class _Settings:
        def get_sp_metadata(self) -> str:
            return "<md/>"

        def validate_metadata(self, metadata: str) -> list[str]:
            return ["invalid_xml"]

    class _Auth:
        def get_settings(self) -> _Settings:
            return _Settings()

    _use_config(monkeypatch, _valid_cfg())
    monkeypatch.setattr(saml_module, "build_auth", lambda request_data, cfg: _Auth())
    response = raw_client.get(METADATA)
    assert response.status_code == 503, response.text


def test_metadata_loads_its_config_off_the_event_loop(raw_client, monkeypatch):
    """Same property as tests/api/test_sso_handlers_off_event_loop.py (#997)."""
    ran_on_loop: list[bool] = []

    def _from_db(cls, db):
        try:
            asyncio.get_running_loop()
            ran_on_loop.append(True)
        except RuntimeError:
            ran_on_loop.append(False)
        return SAMLConfig(enabled=False)

    monkeypatch.setattr(SAMLConfig, "from_db", classmethod(_from_db))
    response = raw_client.get(METADATA)
    assert response.status_code == 404, response.text
    assert ran_on_loop == [False]
