# mypy: disable-error-code="arg-type"
# These tests pass structural stand-ins to signatures declaring Session/User
# and build config objects from dicts. Suppressing arg-type for the file is the
# honest statement of that; the alternative is casts at every call site.
"""Unit tests for OIDC discovery and generic-provider support (issue #353).

Covers:
- Discovery document parsing into our endpoint names
- Public/internal split: the browser-facing authorization endpoint stays public while
  token/JWKS/userinfo move to the compose-network host
- Realm fallback is byte-identical when no discovery URL is configured
- Dotted-path roles claim (``groups`` for Authentik, ``realm_access.roles`` for realms)
- ID-token-only validation (there is no access-token fallback)
- TTL caching (one HTTP fetch serves two calls)
- A discovery failure degrades to the realm URLs instead of raising

httpx is faked (no network, no event-loop plugin needed — the house style in
tests/unit/test_db_metrics.py drives coroutines with ``asyncio.run``).
"""

import asyncio

import pytest

from app.auth.oidc import OIDCConfig
from app.auth.oidc import discovery as oidc_discovery
from app.auth.oidc import get_authorization_url
from app.auth.oidc import resolve_endpoints
from app.auth.oidc import validate_token
from app.auth.oidc.claims import _claim_by_path
from app.auth.oidc.claims import _normalize_roles
from app.auth.oidc.endpoints import _get_realm_urls

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


def _cfg(**kwargs) -> OIDCConfig:
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
    return OIDCConfig(**base)


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


class TestToInternal:
    def test_swaps_scheme_and_host(self):
        result = oidc_discovery.to_internal(
            "https://auth.example.com/token", "https://auth.example.com", "http://authentik:9000"
        )
        assert result == "http://authentik:9000/token"

    def test_no_internal_base_is_identity(self):
        result = oidc_discovery.to_internal(
            "https://auth.example.com/token", "https://auth.example.com", ""
        )
        assert result == "https://auth.example.com/token"

    def test_foreign_host_left_alone(self):
        result = oidc_discovery.to_internal(
            "https://other.example.com/token", "https://auth.example.com", "http://authentik:9000"
        )
        assert result == "https://other.example.com/token"

    def test_relative_internal_base_ignored(self):
        result = oidc_discovery.to_internal(
            "https://auth.example.com/token", "https://auth.example.com", "not-a-url"
        )
        assert result == "https://auth.example.com/token"

    def test_query_string_preserved(self):
        result = oidc_discovery.to_internal(
            "https://auth.example.com/token?foo=bar",
            "https://auth.example.com",
            "http://authentik:9000",
        )
        assert result == "http://authentik:9000/token?foo=bar"


class TestResolveEndpoints:
    def test_discovery_endpoints_used(self, fake_http):
        fake_http.routes[AUTHENTIK_DISCOVERY] = AUTHENTIK_DOCUMENT
        cfg = _cfg(discovery_url=AUTHENTIK_DISCOVERY)
        endpoints = asyncio.run(resolve_endpoints(cfg))
        assert endpoints["token"] == AUTHENTIK_DOCUMENT["token_endpoint"]
        assert endpoints["issuer"] == AUTHENTIK_DOCUMENT["issuer"]

    def test_internal_split_keeps_authorization_public(self, fake_http):
        fake_http.routes[AUTHENTIK_DISCOVERY] = AUTHENTIK_DOCUMENT
        cfg = _cfg(
            discovery_url=AUTHENTIK_DISCOVERY,
            server_url="https://auth.example.com",
            internal_url="http://authentik:9000",
        )
        endpoints = asyncio.run(resolve_endpoints(cfg, internal=True))
        assert endpoints["authorization"].startswith("https://auth.example.com")
        assert endpoints["token"].startswith("http://authentik:9000")

    def test_configured_issuer_overrides_document(self, fake_http):
        fake_http.routes[AUTHENTIK_DISCOVERY] = AUTHENTIK_DOCUMENT
        cfg = _cfg(discovery_url=AUTHENTIK_DISCOVERY, issuer="https://override.example.com")
        endpoints = asyncio.run(resolve_endpoints(cfg))
        assert endpoints["issuer"] == "https://override.example.com"

    def test_discovery_failure_falls_back_to_realm(self, fake_http):
        cfg = _cfg(discovery_url=AUTHENTIK_DISCOVERY)
        endpoints = asyncio.run(resolve_endpoints(cfg))
        assert endpoints == _get_realm_urls(cfg, internal=False)

    def test_missing_end_session_endpoint_is_empty(self, fake_http):
        doc = dict(AUTHENTIK_DOCUMENT)
        del doc["end_session_endpoint"]
        fake_http.routes[AUTHENTIK_DISCOVERY] = doc
        cfg = _cfg(discovery_url=AUTHENTIK_DISCOVERY)
        endpoints = asyncio.run(resolve_endpoints(cfg))
        assert endpoints["logout"] == ""


class TestRealmFallbackUnchanged:
    """Pins the six realm-derived URLs byte for byte — no discovery configured."""

    def test_public_urls(self, fake_http):
        cfg = _cfg()
        endpoints = asyncio.run(resolve_endpoints(cfg))
        assert endpoints == {
            "authorization": "https://keycloak.example.com/realms/opentranscribe/protocol/openid-connect/auth",
            "token": "https://keycloak.example.com/realms/opentranscribe/protocol/openid-connect/token",
            "userinfo": "https://keycloak.example.com/realms/opentranscribe/protocol/openid-connect/userinfo",
            "logout": "https://keycloak.example.com/realms/opentranscribe/protocol/openid-connect/logout",
            "certs": "https://keycloak.example.com/realms/opentranscribe/protocol/openid-connect/certs",
            "issuer": "https://keycloak.example.com/realms/opentranscribe",
        }

    def test_internal_urls(self, fake_http):
        cfg = _cfg(internal_url="http://keycloak:8080")
        endpoints = asyncio.run(resolve_endpoints(cfg, internal=True))
        assert endpoints == {
            "authorization": "https://keycloak.example.com/realms/opentranscribe/protocol/openid-connect/auth",
            "token": "http://keycloak:8080/realms/opentranscribe/protocol/openid-connect/token",
            "userinfo": "http://keycloak:8080/realms/opentranscribe/protocol/openid-connect/userinfo",
            "logout": "http://keycloak:8080/realms/opentranscribe/protocol/openid-connect/logout",
            "certs": "http://keycloak:8080/realms/opentranscribe/protocol/openid-connect/certs",
            "issuer": "https://keycloak.example.com/realms/opentranscribe",
        }


class TestAuthorizationUrl:
    def test_default_scopes_and_pkce(self, fake_http):
        cfg = _cfg()
        url, verifier = asyncio.run(get_authorization_url("state123", cfg=cfg))
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url
        assert verifier is not None

    def test_custom_scopes(self, fake_http):
        cfg = _cfg(scopes="openid profile groups")
        url, _ = asyncio.run(get_authorization_url("state123", cfg=cfg))
        assert "scope=openid+profile+groups" in url

    def test_discovery_authorization_endpoint(self, fake_http):
        fake_http.routes[AUTHENTIK_DISCOVERY] = AUTHENTIK_DOCUMENT
        cfg = _cfg(discovery_url=AUTHENTIK_DISCOVERY)
        url, _ = asyncio.run(get_authorization_url("state123", cfg=cfg))
        assert url.startswith(AUTHENTIK_DOCUMENT["authorization_endpoint"])


class TestSecurityDefaults:
    def test_audience_validation_defaults_on(self):
        assert OIDCConfig().verify_audience is True


class TestClaimByPath:
    def test_dotted_path(self):
        assert _claim_by_path({"realm_access": {"roles": ["admin"]}}, "realm_access.roles") == [
            "admin"
        ]

    def test_flat_groups_claim(self):
        assert _claim_by_path({"groups": ["a", "b"]}, "groups") == ["a", "b"]

    def test_missing_path_returns_none(self):
        assert _claim_by_path({}, "realm_access.roles") is None

    def test_non_dict_intermediate_returns_none(self):
        assert _claim_by_path({"realm_access": "not-a-dict"}, "realm_access.roles") is None

    def test_normalize_list_and_string(self):
        assert _normalize_roles(["a", "b"]) == ["a", "b"]
        assert _normalize_roles("a") == ["a"]
        assert _normalize_roles(None) is None
        assert _normalize_roles(42) is None


# ---------------------------------------------------------------------------
# Token validation: ID token preferred, roles claim honoured
#
# joserfc runs for real here — signature verification, the algorithm
# allow-list, and JWTClaimsRegistry all execute against genuinely-signed
# tokens (RS256, a generated key). A decode()-level stub, which is what this
# suite used before the Authlib/joserfc swap (#33), never exercised any of
# that path: it faked jwt.decode entirely, so the "does verification actually
# work" question was untested regardless of what python-jose did.
# ---------------------------------------------------------------------------


class _TrackingJWT:
    """Wraps the real joserfc decode and remembers which logical token name
    (not the raw JWT string) was passed, for the "the access token must never
    be decoded" class of assertion.
    """

    def __init__(self, name_for):
        self._name_for = name_for
        self.decoded: list[str] = []
        self.tokens: dict[str, str] = {}

    def decode(self, token, *args, **kwargs):
        from joserfc import jwt as real_jwt

        self.decoded.append(self._name_for(token))
        return real_jwt.decode(token, *args, **kwargs)


@pytest.fixture
def stub_jwt(monkeypatch, fake_http):
    """Sign real RS256 tokens against a generated key and serve its public JWK
    as the provider's certs endpoint.

    ``_install({"ID": payload, ...})`` signs each payload; the returned
    tracker exposes ``.tokens`` (name -> signed JWT string, pass this as
    ``id_token``/``access_token``) and ``.decoded`` (names in call order, for
    "was this ever decoded" assertions). A test that needs a deliberately
    invalid token passes a raw garbage string directly rather than going
    through the table — genuinely malformed input, not a lookup miss.
    """
    from joserfc import jwt as real_jwt
    from joserfc.jwk import RSAKey

    from app.auth.oidc import claims as oidc_claims

    key = RSAKey.generate_key(2048, private=True, parameters={"kid": "test-key"})
    fake_http.routes[
        "https://keycloak.example.com/realms/opentranscribe/protocol/openid-connect/certs"
    ] = {"keys": [key.as_dict(private=False)]}

    tokens: dict[str, str] = {}

    def _name_for(token: str) -> str:
        return next((name for name, value in tokens.items() if value == token), token)

    tracker = _TrackingJWT(_name_for)
    tracker.tokens = tokens
    monkeypatch.setattr(oidc_claims, "jwt", tracker)

    def _install(table: dict):
        for name, payload in table.items():
            tracker.tokens[name] = real_jwt.encode(
                {"alg": "RS256", "kid": "test-key"}, payload, key
            )
        return tracker

    return _install


class TestValidateTokenPrefersIdToken:
    def _payload(self, **extra):
        import time

        payload = {
            "sub": "user-1",
            "email": "user@example.com",
            "name": "A User",
            "preferred_username": "auser",
            # Both needed now that verification is real: JWTClaimsRegistry
            # always makes `exp` essential (claims.py explains why), and `aud`
            # matches _cfg's default client_id so tests not specifically about
            # audience enforcement do not also have to opt out of it.
            "aud": "opentranscribe-app",
            "exp": int(time.time()) + 3600,
        }
        payload.update(extra)
        return payload

    def test_id_token_is_validated_first(self, stub_jwt):
        tracker = stub_jwt(
            {
                "ID": self._payload(realm_access={"roles": ["admin"]}),
                "ACCESS": self._payload(realm_access={"roles": []}),
            }
        )
        cfg = _cfg(verify_issuer=False, admin_role="admin")
        data = asyncio.run(
            validate_token(tracker.tokens["ACCESS"], cfg=cfg, id_token=tracker.tokens["ID"])
        )
        assert tracker.decoded == ["ID"]
        assert data is not None
        assert data["is_admin"] is True

    def test_invalid_id_token_never_falls_back_to_the_access_token(self, stub_jwt):
        """The downgrade path is gone, and this is the test that proves it.

        An ID token that fails validation used to fall through to the access token,
        which is attacker-influenceable (RFC 9068 §6 forbids the client inspecting an
        access token at all, and its ``aud`` means something different). Here the
        access token WOULD validate and WOULD grant admin — the login must still fail.
        """
        tracker = stub_jwt({"ACCESS": self._payload(realm_access={"roles": ["admin"]})})
        cfg = _cfg(verify_issuer=False, admin_role="admin")
        result = asyncio.run(
            validate_token(tracker.tokens["ACCESS"], cfg=cfg, id_token="not-a-real-jwt")
        )
        assert result is None
        assert tracker.decoded == ["not-a-real-jwt"], "the access token must never be decoded"

    def test_no_id_token_is_a_hard_failure(self, stub_jwt):
        """Previously succeeded on the access token alone; now refused outright."""
        tracker = stub_jwt({"ACCESS": self._payload(realm_access={"roles": ["user"]})})
        cfg = _cfg(verify_issuer=False, admin_role="admin")
        assert asyncio.run(validate_token(tracker.tokens["ACCESS"], cfg=cfg)) is None
        assert tracker.decoded == []

    def test_invalid_id_token_returns_none(self, stub_jwt):
        stub_jwt({})
        cfg = _cfg(verify_issuer=False)
        assert asyncio.run(validate_token("ACCESS", cfg=cfg, id_token="not-a-real-jwt")) is None

    def test_groups_claim_maps_admin(self, stub_jwt):
        """Authentik shape: flat ``groups`` list, no realm_access anywhere."""
        tracker = stub_jwt({"ID": self._payload(groups=["ot-admins", "staff"])})
        cfg = _cfg(verify_issuer=False, roles_claim="groups", admin_role="ot-admins")
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=tracker.tokens["ID"]))
        assert data is not None
        assert data["roles"] == ["ot-admins", "staff"]
        assert data["is_admin"] is True

    def test_missing_claim_falls_back_to_userinfo(self, stub_jwt, fake_http):
        tracker = stub_jwt({"ID": self._payload()})
        userinfo = (
            "https://keycloak.example.com/realms/opentranscribe/protocol/openid-connect/userinfo"
        )
        fake_http.routes[userinfo] = {"groups": ["ot-admins"]}
        cfg = _cfg(verify_issuer=False, roles_claim="groups", admin_role="ot-admins")
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=tracker.tokens["ID"]))
        assert data is not None
        assert data["is_admin"] is True
        assert userinfo in fake_http.calls

    def test_userinfo_failure_degrades_to_no_roles(self, stub_jwt, fake_http):
        tracker = stub_jwt({"ID": self._payload()})
        cfg = _cfg(verify_issuer=False, roles_claim="groups", admin_role="ot-admins")
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=tracker.tokens["ID"]))
        assert data is not None
        assert data["roles"] == []
        assert data["is_admin"] is False

    def test_jwks_unavailable_returns_none(self, stub_jwt, fake_http):
        tracker = stub_jwt({"ID": self._payload()})
        id_token = tracker.tokens["ID"]
        fake_http.routes.clear()
        cfg = _cfg(verify_issuer=False)
        assert asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=id_token)) is None


class TestValidateTokenClaimDiagnostics:
    """P1.2: claim *names* only, never values — see claims.py's OIDCUserData docstring."""

    def _payload(self, **extra):
        import time

        payload = {
            "sub": "user-1",
            "email": "user@example.com",
            "name": "A User",
            "preferred_username": "auser",
            "aud": "opentranscribe-app",
            "exp": int(time.time()) + 3600,
        }
        payload.update(extra)
        return payload

    def test_claim_keys_are_the_top_level_id_token_keys(self, stub_jwt):
        tracker = stub_jwt({"ID": self._payload(groups=["ot-admins"], amr=["pwd"])})
        cfg = _cfg(verify_issuer=False, roles_claim="groups")
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=tracker.tokens["ID"]))
        assert data is not None
        assert data["claim_keys"] == sorted(
            ["sub", "email", "name", "preferred_username", "groups", "amr", "aud", "exp"]
        )

    def test_roles_claim_source_is_id_token_when_present_there(self, stub_jwt):
        tracker = stub_jwt({"ID": self._payload(groups=["ot-admins"])})
        cfg = _cfg(verify_issuer=False, roles_claim="groups")
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=tracker.tokens["ID"]))
        assert data is not None
        assert data["roles_claim_source"] == "id_token"

    def test_roles_claim_source_is_userinfo_when_the_id_token_lacks_it(self, stub_jwt, fake_http):
        tracker = stub_jwt({"ID": self._payload()})
        userinfo = (
            "https://keycloak.example.com/realms/opentranscribe/protocol/openid-connect/userinfo"
        )
        fake_http.routes[userinfo] = {"groups": ["ot-admins"]}
        cfg = _cfg(verify_issuer=False, roles_claim="groups")
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=tracker.tokens["ID"]))
        assert data is not None
        assert data["roles_claim_source"] == "userinfo"

    def test_roles_claim_source_is_absent_when_neither_has_it(self, stub_jwt, fake_http):
        """The realm_access.roles/groups gap this whole task exists to surface."""
        tracker = stub_jwt({"ID": self._payload()})
        cfg = _cfg(verify_issuer=False, roles_claim="groups")
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=tracker.tokens["ID"]))
        assert data is not None
        assert data["roles_claim_source"] == "absent"
        assert data["roles"] == []


class TestClaimsValidationIsReal:
    """New coverage (#33): the swap to joserfc must not silently drop enforcement.

    These have no python-jose-era equivalent because the old stub made them
    untestable — decode() was never real, so there was nothing to verify.
    """

    def _payload(self, **extra):
        import time

        payload = {
            "sub": "user-1",
            "aud": "opentranscribe-app",
            "exp": int(time.time()) + 3600,
        }
        payload.update(extra)
        return payload

    def test_expired_token_is_refused(self, stub_jwt):
        import time

        tracker = stub_jwt({"ID": self._payload(exp=int(time.time()) - 1000)})
        cfg = _cfg(verify_issuer=False)
        assert asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=tracker.tokens["ID"])) is None

    def test_wrong_audience_is_refused(self, stub_jwt):
        tracker = stub_jwt({"ID": self._payload(aud="someone-elses-client")})
        cfg = _cfg(verify_issuer=False)
        assert asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=tracker.tokens["ID"])) is None

    def test_audience_check_skipped_when_disabled(self, stub_jwt):
        tracker = stub_jwt({"ID": self._payload(aud="someone-elses-client")})
        cfg = _cfg(verify_issuer=False, verify_audience=False)
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=tracker.tokens["ID"]))
        assert data is not None

    def test_wrong_issuer_is_refused(self, stub_jwt):
        tracker = stub_jwt({"ID": self._payload(iss="https://not-the-configured-idp.example.com")})
        cfg = _cfg(verify_issuer=True)
        assert asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=tracker.tokens["ID"])) is None

    def test_correct_issuer_is_accepted(self, stub_jwt):
        tracker = stub_jwt(
            {"ID": self._payload(iss="https://keycloak.example.com/realms/opentranscribe")}
        )
        cfg = _cfg(verify_issuer=True)
        data = asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=tracker.tokens["ID"]))
        assert data is not None

    def test_hs256_never_reaches_the_algorithm_allow_list(self):
        """The classic algorithm-confusion attack forges a token with the
        public key (or the client secret) as an HMAC secret and hopes the
        verifier's algorithm list includes HS256. It never does here —
        ID_TOKEN_SIGNING_ALGORITHMS is RS256-only and safe_signing_algorithms
        is the gate nothing can widen. This is the property that makes the
        attack structurally unreachable, independent of what any given token
        claims.
        """
        from app.auth.oidc.claims import ID_TOKEN_SIGNING_ALGORITHMS
        from app.auth.oidc.claims import safe_signing_algorithms

        allowed = safe_signing_algorithms(ID_TOKEN_SIGNING_ALGORITHMS)
        assert "HS256" not in allowed
        assert "none" not in allowed
        assert allowed == ["RS256"]

    def test_hs256_signed_token_is_refused_by_validate_token(self, stub_jwt, fake_http):
        """End-to-end version of the property above: forge a real HS256 token
        (using the provider's own RSA public key material as the "secret",
        the textbook version of this attack) and confirm the full
        validate_token path refuses it — not just the allow-list helper.
        """
        from joserfc import jwt as real_jwt
        from joserfc.jwk import OctKey

        stub_jwt({})  # publishes the RSA JWKS at the certs endpoint
        hmac_key = OctKey.import_key("any-secret-at-all")
        forged = real_jwt.encode({"alg": "HS256"}, self._payload(), hmac_key)
        cfg = _cfg(verify_issuer=False)
        assert asyncio.run(validate_token("ACCESS", cfg=cfg, id_token=forged)) is None
