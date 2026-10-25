"""``GET /api/system/capabilities`` knows who is asking (issue #1274).

A resolver can hide a platform-audience surface from tenants. The endpoint resolves
the caller optionally and applies the platform-staff overlay in core, so a super_admin
sees what the server lets them use while every other caller, anonymous included, gets
the resolver's answer unchanged. The response is per viewer, so it must never be
shared by a cache.

The negative cases carry the weight here: each states a caller who must NOT receive a
grant, and each has a positive control that differs in exactly one thing.
"""

from __future__ import annotations

import time
import uuid as uuid_pkg
from collections.abc import Iterator
from typing import Any

import pytest

from app.api.endpoints.auth import dependencies as deps_module
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.provider_registry import ExternalIdentity
from app.auth.provider_registry import register_verifier
from app.auth.provider_registry import unregister_verifier
from app.auth.session import InMemoryStore
from app.auth.token_service import USER_REVOCATION_EPOCH_PREFIX
from app.auth.token_service import TokenService
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import NO_PLATFORM_BYPASS
from app.core.capabilities import PLATFORM_OVERLAY_KEYS
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.core.config import settings
from tests.jwt_compat import jwt

URL = "/api/system/capabilities"
ALL_DENIED = dict.fromkeys(COMMUNITY_CAPABILITIES, False)


@pytest.fixture(autouse=True)
def _community_resolver() -> Iterator[None]:
    reset_capability_resolver()
    yield
    reset_capability_resolver()


def _get(client, headers: dict[str, str] | None = None):
    """GET the endpoint as the given caller; ``None`` means a truly anonymous one.

    The shared ``client`` keeps the httpOnly cookie a token fixture's login set, so an
    "anonymous" request would silently carry that session. Clear the jar for it.
    """
    if headers is None:
        client.cookies.clear()
    return client.get(URL, headers=headers)


def _caps(client, headers: dict[str, str] | None = None) -> dict[str, bool]:
    response = _get(client, headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    caps: dict[str, bool] = body["capabilities"]
    return caps


def _granted(caps: dict[str, bool]) -> set[str]:
    return {k for k, v in caps.items() if v}


def _deny_platform_keys() -> None:
    set_capability_resolver(
        lambda _req: {**COMMUNITY_CAPABILITIES, **dict.fromkeys(PLATFORM_OVERLAY_KEYS, False)}
    )


def test_platform_key_denied_by_resolver_is_shown_to_super_admin(client, super_admin_token_headers):
    set_capability_resolver(lambda _req: {**COMMUNITY_CAPABILITIES, "admin.takedown": False})

    assert _caps(client)["admin.takedown"] is False
    assert _caps(client, super_admin_token_headers)["admin.takedown"] is True


def test_overlay_denied_to_user_admin_and_anonymous(
    client, user_token_headers, admin_token_headers
):
    """The resolver turned platform surfaces off for tenants; tenants must stay denied."""
    _deny_platform_keys()
    expected = _granted({**COMMUNITY_CAPABILITIES, **dict.fromkeys(PLATFORM_OVERLAY_KEYS, False)})

    assert _granted(_caps(client)) == expected
    assert _granted(_caps(client, user_token_headers)) == expected
    assert _granted(_caps(client, admin_token_headers)) == expected
    assert not (_granted(_caps(client, admin_token_headers)) & PLATFORM_OVERLAY_KEYS)


def test_overlay_applies_when_resolver_raises(client, super_admin_token_headers):
    def _boom(_req):
        raise RuntimeError("resolver down")

    set_capability_resolver(_boom)

    assert _granted(_caps(client, super_admin_token_headers)) == set(PLATFORM_OVERLAY_KEYS)
    assert _granted(_caps(client)) == set()


def test_no_bypass_keys_are_never_overlaid(client, super_admin_token_headers):
    set_capability_resolver(lambda _req: dict(ALL_DENIED))

    caps = _caps(client, super_admin_token_headers)

    assert NO_PLATFORM_BYPASS
    for key in NO_PLATFORM_BYPASS:
        assert caps[key] is False, key


def test_non_platform_keys_are_never_overlaid(client, super_admin_token_headers):
    set_capability_resolver(lambda _req: dict(ALL_DENIED))

    granted = _granted(_caps(client, super_admin_token_headers))

    assert granted == set(PLATFORM_OVERLAY_KEYS)


def test_anonymous_response_identical_to_resolver_output(client):
    """No credential: the body is the resolver's own answer, fail-closed, no overlay."""
    set_capability_resolver(lambda _req: {**COMMUNITY_CAPABILITIES, "admin.takedown": False})

    assert _caps(client) == {**COMMUNITY_CAPABILITIES, "admin.takedown": False}


def test_garbage_credential_is_anonymous_not_an_error(client):
    """An unusable token must never turn the public bootstrap endpoint into a 401."""
    set_capability_resolver(lambda _req: dict(ALL_DENIED))

    response = _get(client, {"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 200
    assert _granted(response.json()["capabilities"]) == set()


def test_community_body_is_identical_for_every_role(
    client, user_token_headers, admin_token_headers, super_admin_token_headers
):
    baseline = _caps(client)

    assert baseline == COMMUNITY_CAPABILITIES
    assert _caps(client, user_token_headers) == baseline
    assert _caps(client, admin_token_headers) == baseline
    assert _caps(client, super_admin_token_headers) == baseline


def _vary_tokens(value: str) -> set[str]:
    return {part.strip().lower() for part in value.split(",")}


@pytest.mark.parametrize("who", ["anonymous", "super_admin"])
def test_capabilities_response_is_private_and_varies_on_credentials(
    client, super_admin_token_headers, who
):
    headers = super_admin_token_headers if who == "super_admin" else None

    response = _get(client, headers)

    assert response.status_code == 200
    cache_control = {p.strip().lower() for p in response.headers["cache-control"].split(",")}
    assert cache_control == {"private", "no-store"}
    assert {"cookie", "authorization"} <= _vary_tokens(response.headers["vary"])


# -- the revocation dependency (WP-A1 is a prerequisite of this one) -------------------


@pytest.fixture
def revocation(monkeypatch) -> TokenService:
    service = TokenService()
    service._store = InMemoryStore()
    monkeypatch.setattr(deps_module, "token_service", service)
    monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", True)
    return service


def _token_issued_at(user: Any, issued_at: int) -> dict[str, str]:
    token = jwt.encode(
        {
            "sub": str(user.uuid),
            "type": TOKEN_TYPE_ACCESS,
            "jti": uuid_pkg.uuid4().hex,
            "iat": issued_at,
            "exp": int(time.time()) + 600,
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    return {"Authorization": f"Bearer {token}"}


def test_revoked_super_admin_token_gets_no_overlay(client, super_admin_user, revocation):
    """Logout-all must also drop the staff view: the token is stateless and still valid."""
    _deny_platform_keys()
    epoch = int(time.time())
    revocation.store.set(
        f"{USER_REVOCATION_EPOCH_PREFIX}{super_admin_user.uuid}", str(epoch), ex=600
    )

    stale = _token_issued_at(super_admin_user, epoch - 60)

    assert not (_granted(_caps(client, stale)) & PLATFORM_OVERLAY_KEYS)


def test_super_admin_token_issued_after_revocation_keeps_the_overlay(
    client, super_admin_user, revocation
):
    """Control: signing in again after the revocation restores the staff view."""
    _deny_platform_keys()
    revocation.store.set(
        f"{USER_REVOCATION_EPOCH_PREFIX}{super_admin_user.uuid}",
        str(int(time.time()) - 120),
        ex=600,
    )

    fresh = _token_issued_at(super_admin_user, int(time.time()))

    assert set(PLATFORM_OVERLAY_KEYS) <= _granted(_caps(client, fresh))


# -- what the resolver can now see ---------------------------------------------------


class _AcceptOneToken:
    def __init__(self, token: str, identity: ExternalIdentity):
        self.token = token
        self.identity = identity

    def verify(self, token: str, request: Any) -> ExternalIdentity | None:
        return self.identity if token == self.token else None


def test_resolver_sees_the_external_caller_at_this_endpoint(client):
    """An external token is resolved optionally, so a tier-scoped resolver can read who asked."""
    identity = ExternalIdentity(
        provider="oidc",
        external_id=f"user_{uuid_pkg.uuid4().hex[:12]}",
        email=f"caps-viewer-{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name="Caps Viewer",
        org_id="org_caps_viewer",
        org_role="org:member",
    )
    seen: list[tuple[Any, Any]] = []

    def _resolver(request):
        seen.append(
            (
                getattr(request.state, "external_identity", None),
                getattr(request.state, "user_id", None),
            )
        )
        return dict(COMMUNITY_CAPABILITIES)

    register_verifier("oidc", _AcceptOneToken("ext-token", identity))
    set_capability_resolver(_resolver)
    try:
        assert client.get(URL, headers={"Authorization": "Bearer ext-token"}).status_code == 200
        seen_with_token = list(seen)
        seen.clear()
        assert _get(client).status_code == 200
        seen_anonymous = list(seen)
    finally:
        unregister_verifier("oidc")

    assert len(seen_with_token) == 1
    assert seen_with_token[0][0] is identity
    assert seen_with_token[0][1] is not None
    # Control: no credential, nothing stashed.
    assert seen_anonymous == [(None, None)]
