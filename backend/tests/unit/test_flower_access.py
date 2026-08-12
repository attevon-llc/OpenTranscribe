"""The Flower dashboard is admin-only, and denials must read as 401.

``/flower/`` is served by nginx, not FastAPI, so the only thing standing
between a logged-in non-admin and Flower's task arguments (file and user IDs)
plus worker topology is nginx's ``auth_request /api/auth/flower-authz``. nginx
allows the request on any 2xx and treats **401** — not 403 — as "not
authenticated"; a 403 is forwarded verbatim and a 4xx/5xx of any other shape
becomes a 500. So the contract this pins is narrow on purpose: 200 for
admin/super_admin, 401 for everyone else including anonymous.

Pure unit test — no DB, no HTTP client. The role check and the anonymous
rejection are both reachable by calling the functions directly.
"""

# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures that declare Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object. Declared once here rather than as a cast at every call site — casts
# bury the assertion, and widening a production signature to suit a test is worse.
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.endpoints.auth import flower as flower_module
from app.api.endpoints.auth.dependencies import get_current_user
from app.api.endpoints.auth.flower import flower_authz


def _user(role: str) -> SimpleNamespace:
    """A resolved-user stand-in: only ``role`` is consulted."""
    return SimpleNamespace(role=role, is_active=True)


def _request() -> SimpleNamespace:
    """A Request stand-in. Only the lifecycle gate reads it, and that is patched."""
    return SimpleNamespace(scope={}, url=SimpleNamespace(path="/api/auth/flower-authz"))


@pytest.fixture
def gate_allows(monkeypatch):
    """Neutralise the account-lifecycle gate so these tests isolate the ROLE check.

    ``flower_authz`` calls ``get_current_active_user`` in its body (#431), which
    does real DB work — session invalidation, banner lookup. Letting it run would
    make this a DB test and would conflate two independent contracts. The gate's own
    logic is covered by ``test_account_lifecycle.py``, and that it is *reached* is
    covered by ``TestTheLifecycleGateIsEnforcedInTheBody`` below, which is what the
    waiver in ``test_lifecycle_gate_coverage.py`` claims.
    """
    monkeypatch.setattr(flower_module, "get_current_active_user", lambda request, u, db: u)


class TestAdminsAreAllowed:
    @pytest.mark.parametrize("role", ["admin", "super_admin"])
    def test_admin_roles_get_200(self, role, gate_allows):
        response = flower_authz(request=_request(), current_user=_user(role), db=None)
        assert response.status_code == 200

    def test_response_body_is_empty(self, gate_allows):
        """nginx discards the body; serializing anything is wasted work per asset."""
        assert flower_authz(request=_request(), current_user=_user("admin"), db=None).body == b""


class TestNonAdminsAreDenied:
    @pytest.mark.parametrize("role", ["user", "", None, "moderator"])
    def test_non_admin_roles_get_401_not_403(self, role, gate_allows):
        """403 is the dependency's native answer; auth_request needs it as 401."""
        with pytest.raises(HTTPException) as exc:
            flower_authz(request=_request(), current_user=_user(role), db=None)
        assert exc.value.status_code == 401


class TestTheLifecycleGateIsEnforcedInTheBody:
    """The gate must run here, and its 403 must be normalized to 401.

    This is the evidence for the waiver in ``test_lifecycle_gate_coverage.py``:
    that test only proves the route does NOT reach the gate through its dependency
    tree. Without the two tests below, "it enforces the gate in the body" would be
    an unverified comment, and deleting the call would break nothing.
    """

    def test_a_gate_rejection_becomes_401_even_for_an_admin(self, monkeypatch):
        """An expired / must-change-password ADMIN is denied, as 401 not 403."""

        def _blocked(request, user, db):
            raise HTTPException(status_code=403, detail={"code": "password_change_required"})

        monkeypatch.setattr(flower_module, "get_current_active_user", _blocked)

        with pytest.raises(HTTPException) as exc:
            flower_authz(request=_request(), current_user=_user("admin"), db=None)

        assert exc.value.status_code == 401
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}

    def test_a_non_403_denial_is_forwarded_unchanged(self, monkeypatch):
        """Only 403 is remapped. A 400 (deactivated account) must not become 401.

        Pre-existing and deliberate: nginx turns a 400 from auth_request into a 500
        for the client, which is ugly but honest. Widening the remap to every status
        would make a real server error look like "please log in".
        """

        def _inactive(request, user, db):
            raise HTTPException(status_code=400, detail="Inactive user")

        monkeypatch.setattr(flower_module, "get_current_active_user", _inactive)

        with pytest.raises(HTTPException) as exc:
            flower_authz(request=_request(), current_user=_user("admin"), db=None)

        assert exc.value.status_code == 400

    def test_anonymous_is_rejected_before_the_handler(self):
        """No cookie and no bearer token: the dependency 401s, so the body never runs."""
        request = SimpleNamespace(state=SimpleNamespace(), cookies={}, headers={})
        with pytest.raises(HTTPException) as exc:
            get_current_user(request=request, token=None, db=None)
        assert exc.value.status_code == 401
