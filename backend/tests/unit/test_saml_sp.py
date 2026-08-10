"""The python3-saml settings/request bridge — no live IdP or DB needed.

``build_settings`` and ``saml_request_data`` are pure translation: SAMLConfig (or a
FastAPI Request) in, a python3-saml-shaped dict out. Actual signature verification
is python3-saml's own, exercised against a real IdP in the E2E round (task #20) —
these tests pin that the translation itself is correct, which is what everything
downstream of it depends on.
"""

from __future__ import annotations

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from app.auth.saml.config import SAMLConfig
from app.auth.saml.sp import build_settings
from app.auth.saml.sp import saml_request_data


def _cfg(**overrides) -> SAMLConfig:
    base: dict[str, object] = {
        "enabled": True,
        "sp_entity_id": "https://sp.example.com",
        "sp_acs_url": "https://sp.example.com/api/auth/saml/acs",
        "sp_sls_url": "https://sp.example.com/api/auth/saml/sls",
        "idp_entity_id": "https://idp.example.com",
        "idp_sso_url": "https://idp.example.com/sso",
        "idp_slo_url": "https://idp.example.com/slo",
        "idp_x509_cert": "MIIC...fake...cert",
    }
    base.update(overrides)
    return SAMLConfig(**base)  # type: ignore[arg-type]


class TestBuildSettings:
    def test_strict_mode_is_always_on(self):
        """python3-saml documents strict=False as a debugging aid only."""
        assert build_settings(_cfg())["strict"] is True

    def test_sp_and_idp_identity_round_trip(self):
        cfg = _cfg()
        settings = build_settings(cfg)
        assert settings["sp"]["entityId"] == cfg.sp_entity_id
        assert settings["sp"]["assertionConsumerService"]["url"] == cfg.sp_acs_url
        assert settings["sp"]["singleLogoutService"]["url"] == cfg.sp_sls_url
        assert settings["idp"]["entityId"] == cfg.idp_entity_id
        assert settings["idp"]["singleSignOnService"]["url"] == cfg.idp_sso_url
        assert settings["idp"]["singleLogoutService"]["url"] == cfg.idp_slo_url
        assert settings["idp"]["x509cert"] == cfg.idp_x509_cert

    def test_security_posture_defaults_to_wanting_signatures(self):
        settings = build_settings(_cfg())
        assert settings["security"]["wantAssertionsSigned"] is True
        assert settings["security"]["wantMessagesSigned"] is True
        assert settings["security"]["authnRequestsSigned"] is False

    def test_security_posture_follows_config(self):
        cfg = _cfg(
            want_assertions_signed=False,
            want_messages_signed=False,
            sign_authn_requests=True,
            sp_x509_cert="MIIC...sp...cert",
            sp_private_key="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",  # gitleaks:allow - fake fixture value, not a real key
        )
        settings = build_settings(cfg)
        assert settings["security"]["wantAssertionsSigned"] is False
        assert settings["security"]["wantMessagesSigned"] is False
        assert settings["security"]["authnRequestsSigned"] is True
        assert settings["sp"]["x509cert"] == cfg.sp_x509_cert
        assert settings["sp"]["privateKey"] == cfg.sp_private_key

    def test_settings_validate_against_the_real_toolkit(self):
        """Not a stub check — feeds the built dict through python3-saml's own
        OneLogin_Saml2_Settings validation so a shape mistake fails here, not at
        the first real login."""
        from onelogin.saml2.settings import OneLogin_Saml2_Settings

        settings = OneLogin_Saml2_Settings(settings=build_settings(_cfg()), sp_validation_only=True)
        errors = settings.check_sp_settings(build_settings(_cfg()))
        assert errors == [], errors


class TestSamlRequestData:
    async def _request(self, *, method="GET", headers=None, query="", body=b"") -> Request:
        scope = {
            "type": "http",
            "method": method,
            "path": "/api/auth/saml/acs",
            "query_string": query.encode(),
            "headers": Headers(headers or {}).raw,
            "scheme": "https",
        }

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(scope, receive=receive)

    @pytest.mark.asyncio
    async def test_https_and_host_come_from_forwarded_headers(self):
        request = await self._request(
            headers={
                "x-forwarded-proto": "https",
                "x-forwarded-host": "app.example.com",
                "host": "backend-internal:8080",
            }
        )
        data = await saml_request_data(request)
        assert data["https"] == "on"
        assert data["http_host"] == "app.example.com"

    @pytest.mark.asyncio
    async def test_falls_back_to_the_host_header_without_forwarding(self):
        request = await self._request(headers={"host": "localhost:5173"})
        data = await saml_request_data(request)
        assert data["http_host"] == "localhost"
        assert data["server_port"] == "5173"

    @pytest.mark.asyncio
    async def test_get_data_carries_query_params(self):
        request = await self._request(query="RelayState=%2Fdashboard")
        data = await saml_request_data(request)
        assert data["get_data"]["RelayState"] == "/dashboard"

    @pytest.mark.asyncio
    async def test_post_form_becomes_post_data(self):
        body = b"SAMLResponse=abc123&RelayState=%2F"
        request = await self._request(
            method="POST",
            headers={
                "host": "localhost:5173",
                "content-type": "application/x-www-form-urlencoded",
            },
            body=body,
        )
        data = await saml_request_data(request)
        assert data["post_data"]["SAMLResponse"] == "abc123"
        assert data["post_data"]["RelayState"] == "/"

    @pytest.mark.asyncio
    async def test_a_get_request_has_no_post_data(self):
        request = await self._request(headers={"host": "localhost:5173"})
        data = await saml_request_data(request)
        assert data["post_data"] == {}
