"""Unit tests for OIDC discovery and generic-provider support (issue #353).

Covers:
- Discovery document parsing into our endpoint names
- Public/internal split: the browser-facing authorization endpoint stays public while
  token/JWKS/userinfo move to the compose-network host
- Realm fallback is byte-identical when no discovery URL is configured
- Dotted-path roles claim (``groups`` for Authentik, ``realm_access.roles`` for Keycloak)
- ID-token-preferred validation
- TTL caching (one HTTP fetch serves two calls)
- A discovery failure degrades to the realm URLs instead of raising

httpx is faked (no network, no event-loop plugin needed — the house style in
tests/unit/test_db_metrics.py drives coroutines with ``asyncio.run``).
"""

import asyncio

import pytest

from app.auth import oidc_discovery
from app.auth.keycloak_auth import KeycloakConfig
from app.auth.keycloak_auth import _claim_by_path
from app.auth.keycloak_auth import _get_keycloak_urls
from app.auth.keycloak_auth import _normalize_roles
from app.auth.keycloak_auth import get_authorization_url
from app.auth.keycloak_auth import resolve_endpoints
from app.auth.keycloak_auth import validate_token

AUTHENTIK_DISCOVERY = (
    "https://auth.example.com/application/o/opentranscribe/.well-known/openid-configuration"
)

AUTHENTIK_DOCUMENT = {
    "issuer": "https://auth.example.com/application/o/opentranscribe/",
    "authorization_endpoint": "https://auth.example.com/application/o/authorize/",
    "token_endpoint": "https://auth.example.com/application/o/token/",
    "userinfo_endpoint": "https://auth.example.com/application/o/userinfo/",
    "end_session_endpoint": "https://auth.example.com/application/o/opentranscribe/end-session/",
    "jwks_uri": "https://auth.example.com/application/o/opentranscribe/jwks/",
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]

    def json(self):
        return self._payload


class FakeAsyncClient:
    """Stands in for httpx.AsyncClient, recording every GET it serves."""

    #: url -> payload (or Exception instance to raise)
    routes: dict = {}
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, **kwargs):
        type(self).calls.append(url)
        payload = type(self).routes.get(url)
        if isinstance(payload, Exception):
            raise payload
        if payload is None:
            return FakeResponse({}, status_code=404)
        return FakeResponse(payload)


@pytest.fixture
def fake_http(monkeypatch):
    """Route oidc_discovery's HTTP calls to FakeAsyncClient and skip the SSRF DNS check."""
    FakeAsyncClient.routes = {}
    FakeAsyncClient.calls = []
    oidc_discovery.clear_discovery_caches()
    monkeypatch.setattr(oidc_discovery.httpx, "AsyncClient", FakeAsyncClient)
    # assert_safe_outbound_url resolves DNS; example.com hosts must not be looked up.
    monkeypatch.setattr(oidc_discovery, "assert_safe_outbound_url", lambda *a, **k: None)
    yield FakeAsyncClient
    oidc_discovery.clear_discovery_caches()


def _cfg(**kwargs) -> KeycloakConfig:
    base = {
        "enabled": True,
        "server_url": "https://keycloak.example.com",
        "realm": "opentranscribe",
        "client_id": "opentranscribe-app",
        "client_secret": "secret",
        "callback_url": "https://app.example.com/login",
        "timeout": 5,
    }
    base.update(kwargs)
    return KeycloakConfig(**base)


# ---------------------------------------------------------------------------
# Discovery document fetching / parsing
# ---------------------------------------------------------------------------


class TestFetchDiscoveryDocument:
    def test_parses_document(self, fake_http):
        fake_http.routes[AUTHENTIK_DISCOVERY] = AUTHENTIK_DOCUMENT
        doc = asyncio.run(oidc_discovery.fetch_discovery_document(AUTHENTIK_DISCOVERY))
        assert doc is not None
        assert doc["token_endpoint"] == "https://auth.example.com/application/o/token/"

    def test_missing_core_endpoints_rejected(self, fake_http):
        fake_http.routes[AUTHENTIK_DISCOVERY] = {"issuer": "https://auth.example.com/"}
        assert asyncio.run(oidc_discovery.fetch_discovery_document(AUTHENTIK_DISCOVERY)) is None

    def test_http_failure_returns_none_without_raising(self, fake_http):
        import httpx

        fake_http.routes[AUTHENTIK_DISCOVERY] = httpx.ConnectError("no route to host")
        assert asyncio.run(oidc_discovery.fetch_discovery_document(AUTHENTIK_DISCOVERY)) is None

    def test_ssrf_rejection_returns_none(self, fake_http, monkeypatch):
        from fastapi import HTTPException

        def _refuse(*args, **kwargs):
            raise HTTPException(status_code=400, detail="nope")

        monkeypatch.setattr(oidc_discovery, "assert_safe_outbound_url", _refuse)
        fake_http.routes[AUTHENTIK_DISCOVERY] = AUTHENTIK_DOCUMENT
        assert asyncio.run(oidc_discovery.fetch_discovery_document(AUTHENTIK_DISCOVERY)) is None
        assert fake_http.calls == []

    def test_empty_url_short_circuits(self, fake_http):
        assert asyncio.run(oidc_discovery.fetch_discovery_document("")) is None
        assert fake_http.calls == []


class TestDiscoveryCaching:
    def test_two_calls_one_fetch(self, fake_http):
        fake_http.routes[AUTHENTIK_DISCOVERY] = AUTHENTIK_DOCUMENT

        async def scenario():
            first = await oidc_discovery.fetch_discovery_document(AUTHENTIK_DISCOVERY)
            second = await oidc_discovery.fetch_discovery_document(AUTHENTIK_DISCOVERY)
            return first, second

        first, second = asyncio.run(scenario())
        assert first == second
        assert fake_http.calls.count(AUTHENTIK_DISCOVERY) == 1

    def test_jwks_cached(self, fake_http):
        jwks_uri = AUTHENTIK_DOCUMENT["jwks_uri"]
        fake_http.routes[jwks_uri] = {"keys": [{"kid": "abc", "kty": "RSA"}]}

        async def scenario():
            await oidc_discovery.fetch_jwks(jwks_uri)
            await oidc_discovery.fetch_jwks(jwks_uri)

        asyncio.run(scenario())
        assert fake_http.calls.count(jwks_uri) == 1

    def test_failure_is_not_cached(self, fake_http):
        import httpx

        fake_http.routes[AUTHENTIK_DISCOVERY] = httpx.ConnectError("down")

        async def scenario():
            await oidc_discovery.fetch_discovery_document(AUTHENTIK_DISCOVERY)
            fake_http.routes[AUTHENTIK_DISCOVERY] = AUTHENTIK_DOCUMENT
            return await oidc_discovery.fetch_discovery_document(AUTHENTIK_DISCOVERY)

        assert asyncio.run(scenario()) == AUTHENTIK_DOCUMENT

    def test_empty_jwks_rejected(self, fake_http):
        jwks_uri = AUTHENTIK_DOCUMENT["jwks_uri"]
        fake_http.routes[jwks_uri] = {"keys": []}
        assert asyncio.run(oidc_discovery.fetch_jwks(jwks_uri)) is None


# ---------------------------------------------------------------------------
# Internal-URL substitution
# ---------------------------------------------------------------------------


class TestToInternal:
    def test_swaps_scheme_and_host(self):
        assert (
            oidc_discovery.to_internal(
                "https://auth.example.com/application/o/token/",
                "https://auth.example.com",
                "http://authentik:9000",
            )
            == "http://authentik:9000/application/o/token/"
        )

    def test_no_internal_base_is_identity(self):
        url = "https://auth.example.com/application/o/token/"
        assert oidc_discovery.to_internal(url, "https://auth.example.com", "") == url

    def test_foreign_host_left_alone(self):
        """The internal base only fronts the configured IdP."""
        url = "https://other-idp.example.net/token"
        assert (
            oidc_discovery.to_internal(url, "https://auth.example.com", "http://authentik:9000")
            == url
        )

    def test_relative_internal_base_ignored(self):
        url = "https://auth.example.com/token"
        assert oidc_discovery.to_internal(url, "https://auth.example.com", "authentik:9000") == url

    def test_query_string_preserved(self):
        assert (
            oidc_discovery.to_internal(
                "https://auth.example.com/o/token/?x=1",
                "https://auth.example.com",
                "http://authentik:9000",
            )
            == "http://authentik:9000/o/token/?x=1"
        )


# ---------------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------------


class TestResolveEndpoints:
    def test_discovery_endpoints_used(self, fake_http):
        fake_http.routes[AUTHENTIK_DISCOVERY] = AUTHENTIK_DOCUMENT
        cfg = _cfg(discovery_url=AUTHENTIK_DISCOVERY, server_url="https://auth.example.com")
        endpoints = asyncio.run(resolve_endpoints(cfg))
        assert endpoints["authorization"] == "https://auth.example.com/application/o/authorize/"
        assert endpoints["token"] == "https://auth.example.com/application/o/token/"
        assert endpoints["certs"] == AUTHENTIK_DOCUMENT["jwks_uri"]
        assert endpoints["logout"] == AUTHENTIK_DOCUMENT["end_session_endpoint"]
        assert endpoints["issuer"] == AUTHENTIK_DOCUMENT["issuer"]
        assert "/realms/" not in endpoints["authorization"]

    def test_internal_split_keeps_authorization_public(self, fake_http):
        fake_http.routes[AUTHENTIK_DISCOVERY] = AUTHENTIK_DOCUMENT
        cfg = _cfg(
            discovery_url=AUTHENTIK_DISCOVERY,
            server_url="https://auth.example.com",
            internal_url="http://authentik:9000",
        )
        endpoints = asyncio.run(resolve_endpoints(cfg, internal=True))
        # Browser-facing: must stay on the public host.
        assert endpoints["authorization"].startswith("https://auth.example.com")
        # Back-channel: must resolve on the compose network.
        assert endpoints["token"].startswith("http://authentik:9000")
        assert endpoints["certs"].startswith("http://authentik:9000")
        assert endpoints["userinfo"].startswith("http://authentik:9000")
        # The issuer is an identity claim, never rewritten.
        assert endpoints["issuer"] == AUTHENTIK_DOCUMENT["issuer"]

    def test_configured_issuer_overrides_document(self, fake_http):
        fake_http.routes[AUTHENTIK_DISCOVERY] = AUTHENTIK_DOCUMENT
        cfg = _cfg(discovery_url=AUTHENTIK_DISCOVERY, issuer="https://issuer.override/")
        assert asyncio.run(resolve_endpoints(cfg))["issuer"] == "https://issuer.override/"

    def test_discovery_failure_falls_back_to_realm(self, fake_http):
        import httpx

        fake_http.routes[AUTHENTIK_DISCOVERY] = httpx.ConnectError("down")
        cfg = _cfg(discovery_url=AUTHENTIK_DISCOVERY, realm="myrealm")
        endpoints = asyncio.run(resolve_endpoints(cfg))
        assert endpoints == _get_keycloak_urls(cfg)

    def test_missing_end_session_endpoint_is_empty(self, fake_http):
        document = {k: v for k, v in AUTHENTIK_DOCUMENT.items() if k != "end_session_endpoint"}
        fake_http.routes[AUTHENTIK_DISCOVERY] = document
        cfg = _cfg(discovery_url=AUTHENTIK_DISCOVERY)
        assert asyncio.run(resolve_endpoints(cfg))["logout"] == ""


class TestRealmFallbackUnchanged:
    """No discovery URL → byte-identical URLs to the pre-#353 builder."""

    def test_public_urls(self, fake_http):
        cfg = _cfg(server_url="https://keycloak.example.com", realm="myrealm")
        endpoints = asyncio.run(resolve_endpoints(cfg))
        assert endpoints["authorization"] == (
            "https://keycloak.example.com/realms/myrealm/protocol/openid-connect/auth"
        )
        assert endpoints["token"] == (
            "https://keycloak.example.com/realms/myrealm/protocol/openid-connect/token"
        )
        assert endpoints["userinfo"] == (
            "https://keycloak.example.com/realms/myrealm/protocol/openid-connect/userinfo"
        )
        assert endpoints["logout"] == (
            "https://keycloak.example.com/realms/myrealm/protocol/openid-connect/logout"
        )
        assert endpoints["certs"] == (
            "https://keycloak.example.com/realms/myrealm/protocol/openid-connect/certs"
        )
        assert endpoints["issuer"] == "https://keycloak.example.com/realms/myrealm"
        assert fake_http.calls == []

    def test_internal_urls(self, fake_http):
        cfg = _cfg(
            server_url="https://keycloak.example.com",
            internal_url="http://keycloak:8080",
            realm="myrealm",
        )
        endpoints = asyncio.run(resolve_endpoints(cfg, internal=True))
        assert endpoints["token"] == (
            "http://keycloak:8080/realms/myrealm/protocol/openid-connect/token"
        )
        assert endpoints["certs"] == (
            "http://keycloak:8080/realms/myrealm/protocol/openid-connect/certs"
        )
        assert endpoints["authorization"] == (
            "https://keycloak.example.com/realms/myrealm/protocol/openid-connect/auth"
        )
        # Issuer verification has always used the public server URL, internal or not.
        assert endpoints["issuer"] == "https://keycloak.example.com/realms/myrealm"


class TestAuthorizationUrl:
    def test_default_scopes_and_pkce(self, fake_http):
        cfg = _cfg(realm="myrealm", use_pkce=True)
        url, verifier = asyncio.run(get_authorization_url("state123", cfg=cfg))
        assert url.startswith(
            "https://keycloak.example.com/realms/myrealm/protocol/openid-connect/auth?"
        )
        assert "scope=openid+email+profile" in url
        assert "code_challenge_method=S256" in url
        assert verifier is not None

    def test_custom_scopes(self, fake_http):
        cfg = _cfg(use_pkce=False, scopes="openid profile groups")
        url, verifier = asyncio.run(get_authorization_url("state123", cfg=cfg))
        assert "scope=openid+profile+groups" in url
        assert verifier is None

    def test_discovery_authorization_endpoint(self, fake_http):
        fake_http.routes[AUTHENTIK_DISCOVERY] = AUTHENTIK_DOCUMENT
        cfg = _cfg(discovery_url=AUTHENTIK_DISCOVERY, use_pkce=False)
        url, _ = asyncio.run(get_authorization_url("state123", cfg=cfg))
        assert url.startswith("https://auth.example.com/application/o/authorize/?")


# ---------------------------------------------------------------------------
# Roles claim resolution
# ---------------------------------------------------------------------------


class TestSecurityDefaults:
    def test_audience_validation_defaults_on(self):
        """A token-validation control must not default to the off position.

        ``core/config.py:KEYCLOAK_VERIFY_AUDIENCE`` is the authority and says True;
        KeycloakConfig used to disagree in three places.
        """
        assert KeycloakConfig().verify_audience is True
        assert KeycloakConfig().verify_issuer is True
        assert KeycloakConfig().use_pkce is True


class TestClaimByPath:
    def test_dotted_path(self):
        assert _claim_by_path({"realm_access": {"roles": ["admin"]}}, "realm_access.roles") == [
            "admin"
        ]

    def test_flat_groups_claim(self):
        assert _claim_by_path({"groups": ["ot-admins"]}, "groups") == ["ot-admins"]

    def test_missing_path_returns_none(self):
        assert _claim_by_path({"groups": ["x"]}, "realm_access.roles") is None

    def test_non_dict_intermediate_returns_none(self):
        assert _claim_by_path({"realm_access": "nope"}, "realm_access.roles") is None

    def test_normalize_list_and_string(self):
        assert _normalize_roles(["a", "b"]) == ["a", "b"]
        assert _normalize_roles("a") == ["a"]
        assert _normalize_roles(None) is None
        assert _normalize_roles(42) is None


# ---------------------------------------------------------------------------
# Token validation: ID token preferred, roles claim honoured
# ---------------------------------------------------------------------------


class _StubJWT:
    """Decodes our fake "tokens", which are just dict keys into a table."""

    def __init__(self, table: dict):
        self.table = table
        self.decoded: list = []

    def decode(self, token, key, algorithms=None, options=None, **kwargs):
        from jose import JWTError

        self.decoded.append(token)
        if token not in self.table:
            raise JWTError(f"unknown token {token}")
        return self.table[token]


@pytest.fixture
def stub_jwt(monkeypatch, fake_http):
    """Replace jose's jwt in keycloak_auth and stub the JWKS fetch."""
    from app.auth import keycloak_auth

    def _install(table: dict):
        stub = _StubJWT(table)
        monkeypatch.setattr(keycloak_auth, "jwt", stub)
        return stub

    fake_http.routes[
        "https://keycloak.example.com/realms/opentranscribe/protocol/openid-connect/certs"
    ] = {"keys": [{"kid": "k1", "kty": "RSA"}]}
    return _install


class TestValidateTokenPrefersIdToken:
    def _payload(self, **extra):
        payload = {
            "sub": "user-1",
            "email": "user@example.com",
            "name": "A User",
            "preferred_username": "auser",
        }
        payload.update(extra)
        return payload

    def test_id_token_is_validated_first(self, stub_jwt):
        stub = stub_jwt(
            {
                "ID": self._payload(realm_access={"roles": ["admin"]}),
                "ACCESS": self._payload(realm_access={"roles": []}),
            }
        )
        cfg = _cfg(verify_issuer=False, admin_role="admin")
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token="ID"))
        assert stub.decoded == ["ID"]
        assert data is not None
        assert data["is_admin"] is True

    def test_falls_back_to_access_token(self, stub_jwt):
        """An opaque/unverifiable ID token must not break a working Keycloak login."""
        stub = stub_jwt({"ACCESS": self._payload(realm_access={"roles": ["admin"]})})
        cfg = _cfg(verify_issuer=False, admin_role="admin")
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token="OPAQUE"))
        assert stub.decoded == ["OPAQUE", "ACCESS"]
        assert data is not None and data["is_admin"] is True

    def test_no_id_token_behaves_as_before(self, stub_jwt):
        stub = stub_jwt({"ACCESS": self._payload(realm_access={"roles": ["user"]})})
        cfg = _cfg(verify_issuer=False, admin_role="admin")
        data = asyncio.run(validate_token("ACCESS", cfg=cfg))
        assert stub.decoded == ["ACCESS"]
        assert data is not None
        assert data["roles"] == ["user"]
        assert data["is_admin"] is False

    def test_both_tokens_invalid_returns_none(self, stub_jwt):
        stub_jwt({})
        cfg = _cfg(verify_issuer=False)
        assert asyncio.run(validate_token("ACCESS", cfg=cfg, id_token="ID")) is None

    def test_groups_claim_maps_admin(self, stub_jwt):
        """Authentik shape: flat ``groups`` list, no realm_access anywhere."""
        stub_jwt({"ID": self._payload(groups=["ot-admins", "staff"])})
        cfg = _cfg(verify_issuer=False, roles_claim="groups", admin_role="ot-admins")
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token="ID"))
        assert data is not None
        assert data["roles"] == ["ot-admins", "staff"]
        assert data["is_admin"] is True

    def test_missing_claim_falls_back_to_userinfo(self, stub_jwt, fake_http):
        stub_jwt({"ID": self._payload()})
        userinfo = (
            "https://keycloak.example.com/realms/opentranscribe/protocol/openid-connect/userinfo"
        )
        fake_http.routes[userinfo] = {"groups": ["ot-admins"]}
        cfg = _cfg(verify_issuer=False, roles_claim="groups", admin_role="ot-admins")
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token="ID"))
        assert data is not None
        assert data["is_admin"] is True
        assert userinfo in fake_http.calls

    def test_userinfo_failure_degrades_to_no_roles(self, stub_jwt, fake_http):
        stub_jwt({"ID": self._payload()})
        cfg = _cfg(verify_issuer=False, roles_claim="groups", admin_role="ot-admins")
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token="ID"))
        assert data is not None
        assert data["roles"] == []
        assert data["is_admin"] is False

    def test_jwks_unavailable_returns_none(self, stub_jwt, fake_http):
        stub_jwt({"ID": self._payload()})
        fake_http.routes.clear()
        cfg = _cfg(verify_issuer=False)
        assert asyncio.run(validate_token("ACCESS", cfg=cfg, id_token="ID")) is None
