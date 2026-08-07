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

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.endpoints.auth.dependencies import get_current_user
from app.api.endpoints.auth.flower import flower_authz


def _user(role: str) -> SimpleNamespace:
    """A resolved-user stand-in: only ``role`` is consulted."""
    return SimpleNamespace(role=role, is_active=True)


class TestAdminsAreAllowed:
    @pytest.mark.parametrize("role", ["admin", "super_admin"])
    def test_admin_roles_get_200(self, role):
        response = flower_authz(current_user=_user(role))
        assert response.status_code == 200

    def test_response_body_is_empty(self):
        """nginx discards the body; serializing anything is wasted work per asset."""
        assert flower_authz(current_user=_user("admin")).body == b""


class TestNonAdminsAreDenied:
    @pytest.mark.parametrize("role", ["user", "", None, "moderator"])
    def test_non_admin_roles_get_401_not_403(self, role):
        """403 is the dependency's native answer; auth_request needs it as 401."""
        with pytest.raises(HTTPException) as exc:
            flower_authz(current_user=_user(role))
        assert exc.value.status_code == 401

    def test_anonymous_is_rejected_before_the_handler(self):
        """No cookie and no bearer token: the dependency 401s, so the body never runs."""
        request = SimpleNamespace(state=SimpleNamespace(), cookies={}, headers={})
        with pytest.raises(HTTPException) as exc:
            get_current_user(request=request, token=None, db=None)
        assert exc.value.status_code == 401
