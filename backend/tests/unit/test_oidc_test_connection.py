"""P1.2 — Test Connection surfaces the claims a provider actually advertises.

``_roles_claim_advertised`` cross-references the admin's configured roles claim
against the discovery document's OPTIONAL ``claims_supported`` field. This is the
only signal available before a real login has happened, and the whole point is
that "wrong claim path" must stop being a silent failure discovered in
production — see ``plans/auth-provider-compatibility.md`` P1.
"""

import asyncio

import pytest

from app.api.endpoints.auth_config import _roles_claim_advertised
from app.api.endpoints.auth_config import _test_oidc_connection


class TestRolesClaimAdvertised:
    def test_yes_when_top_level_segment_is_listed(self):
        assert (
            _roles_claim_advertised("realm_access.roles", ["sub", "email", "realm_access"]) == "yes"
        )

    def test_yes_for_a_flat_claim_not_a_dotted_path(self):
        assert _roles_claim_advertised("groups", ["sub", "groups"]) == "yes"

    def test_no_when_the_top_level_segment_is_absent(self):
        assert _roles_claim_advertised("groups", ["sub", "email", "realm_access"]) == "no"

    def test_unknown_when_the_provider_sent_no_claims_supported(self):
        """Authentik's shape — silence is not the same as "not advertised"."""
        assert _roles_claim_advertised("groups", None) == "unknown"

    def test_unknown_when_claims_supported_is_an_empty_list(self):
        assert _roles_claim_advertised("groups", []) == "unknown"

    def test_unknown_when_no_roles_claim_is_configured_yet(self):
        assert _roles_claim_advertised("", ["sub", "groups"]) == "unknown"

    def test_only_the_first_path_segment_is_checked(self):
        """The rest of a dotted path is a JSON path INTO the claim, not a claim name."""
        assert _roles_claim_advertised("realm_access.roles", ["sub", "realm_access"]) == "yes"


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeAsyncClient:
    #: Set per-test before use.
    document: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, **kwargs):
        return _FakeResponse(_FakeAsyncClient.document)


@pytest.fixture
def fake_discovery_http(monkeypatch):
    import httpx

    from app.utils import url_validation

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(url_validation, "assert_safe_outbound_url", lambda *a, **k: None)
    yield _FakeAsyncClient


class TestOIDCTestConnectionSurfacesClaims:
    """The `/oidc/test` endpoint, exercised at the function level."""

    def test_advertised_claims_and_yes_verdict_reach_details(self, fake_discovery_http):
        fake_discovery_http.document = {
            "issuer": "https://auth.example.com/application/o/opentranscribe/",
            "authorization_endpoint": "https://auth.example.com/application/o/authorize/",
            "token_endpoint": "https://auth.example.com/application/o/token/",
            "claims_supported": ["sub", "email", "groups"],
        }
        config = {
            "oidc_discovery_url": "https://auth.example.com/.well-known/openid-configuration",
            "oidc_roles_claim": "groups",
        }
        result = asyncio.run(_test_oidc_connection(config))
        assert result.success is True
        assert result.details is not None
        assert result.details["claims_supported"] == ["sub", "email", "groups"]
        assert result.details["configured_roles_claim"] == "groups"
        assert result.details["roles_claim_advertised"] == "yes"

    def test_misconfigured_roles_claim_reaches_details_as_no(self, fake_discovery_http):
        fake_discovery_http.document = {
            "issuer": "https://kc.example.com/realms/opentranscribe",
            "authorization_endpoint": "https://kc.example.com/realms/opentranscribe/auth",
            "token_endpoint": "https://kc.example.com/realms/opentranscribe/token",
            "claims_supported": ["sub", "email", "groups"],
        }
        config = {
            "oidc_discovery_url": "https://kc.example.com/.well-known/openid-configuration",
            # Keycloak default — wrong for a provider that only advertises "groups".
            "oidc_roles_claim": "realm_access.roles",
        }
        result = asyncio.run(_test_oidc_connection(config))
        assert result.success is True
        assert result.details is not None
        assert result.details["roles_claim_advertised"] == "no"

    def test_provider_silent_on_claims_supported_reaches_details_as_unknown(
        self, fake_discovery_http
    ):
        """Authentik omits claims_supported — this must read as "unknown", not "no"."""
        fake_discovery_http.document = {
            "issuer": "https://auth.example.com/application/o/opentranscribe/",
            "authorization_endpoint": "https://auth.example.com/application/o/authorize/",
            "token_endpoint": "https://auth.example.com/application/o/token/",
        }
        config = {
            "oidc_discovery_url": "https://auth.example.com/.well-known/openid-configuration",
            "oidc_roles_claim": "groups",
        }
        result = asyncio.run(_test_oidc_connection(config))
        assert result.success is True
        assert result.details is not None
        assert result.details["claims_supported"] is None
        assert result.details["roles_claim_advertised"] == "unknown"
