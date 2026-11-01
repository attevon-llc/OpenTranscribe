"""Privileged dependencies: the super_admin tier and lifecycle chaining (split pins, 2026-11-01).

Pins for three hand-written mutants that survived the whole suite at HEAD and after the
``dependencies.py`` split (see ``test_dependencies_survivor_mutants.py`` for the triage):
an ``admin`` passing ``get_current_active_superuser``, and either privileged dependency
resolving ``get_current_user`` instead of ``get_current_active_user``.
"""

import pytest
from fastapi import HTTPException

from app.api.endpoints.auth.dependencies import get_current_active_superuser
from app.api.endpoints.auth.dependencies import get_current_admin_user
from app.api.endpoints.auth.dependencies import get_current_user
from app.core.config import settings
from tests.unit.auth_dependencies._helpers import _user


class TestGetCurrentActiveSuperuserRefusesAnAdmin:
    def test_an_admin_is_not_a_super_admin(self):
        """``role != ROLE_SUPER_ADMIN`` — an ``admin`` must not pass the super_admin gate."""
        with pytest.raises(HTTPException) as exc:
            get_current_active_superuser(current_user=_user(role="admin"))

        assert exc.value.status_code == 403
        assert exc.value.detail == "Not enough permissions - super_admin required"

    def test_a_super_admin_passes(self):
        user = _user(role="super_admin")

        assert get_current_active_superuser(current_user=user) is user


class TestPrivilegedGatesChainThroughTheLifecycleGate:
    """Both privileged dependencies resolve ``get_current_active_user``, not
    ``get_current_user``: otherwise a flagged admin keeps the whole admin surface
    while every user route refuses them. Only visible through FastAPI's resolution.
    """

    @pytest.mark.parametrize(
        ("dependency", "role"),
        [(get_current_admin_user, "admin"), (get_current_active_superuser, "super_admin")],
    )
    def test_a_privileged_account_owing_a_password_change_is_refused(
        self, monkeypatch, dependency, role
    ):
        from fastapi import Depends
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.db.base import get_db

        monkeypatch.setattr(settings, "LOGIN_BANNER_ENABLED", False)
        app = FastAPI()

        @app.get("/probe")
        def probe(user: object = Depends(dependency)) -> dict:
            return {"ok": True}

        app.dependency_overrides[get_current_user] = lambda: _user(
            role=role, must_change_password=True
        )
        app.dependency_overrides[get_db] = lambda: None

        response = TestClient(app).get("/probe")

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "password_change_required"
