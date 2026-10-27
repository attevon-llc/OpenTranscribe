"""Pinning the local Whisper model rides ``asr.model_selection``, not ``asr.user_providers`` (#1207).

The whole ``/asr-settings`` router used to sit behind ``asr.user_providers`` (bring-your-own
cloud ASR keys), so a deployment that switched that off also lost the super_admin's
``/local-model/*`` routes. The three routes now live in ``asr_local_model`` behind
``asr.model_selection`` and keep their paths.

Runs the real ``api_router`` on a bare FastAPI app with only the DB and the caller overridden,
so nothing here needs Postgres, Redis or a worker.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints.auth import get_current_active_user
from app.api.router import api_router
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.db.base import get_db

ACTIVE = "/api/asr-settings/local-model/active"
SET = "/api/asr-settings/local-model/set"
PROVIDERS = "/api/asr-settings/providers"


class _NoRows:
    """A session whose every query finds nothing (no ``asr.local_model`` pin stored)."""

    def query(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return None


def _caller(role: str):
    return SimpleNamespace(
        id=1,
        email=f"{role}@example.com",
        role=role,
        is_active=True,
        is_superuser=role == "super_admin",
    )


def _client(role: str) -> TestClient:
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: _NoRows()
    app.dependency_overrides[get_current_active_user] = lambda: _caller(role)
    return TestClient(app)


def _capabilities_off(*keys: str) -> None:
    overrides = dict.fromkeys(keys, False)
    set_capability_resolver(lambda _request: {**COMMUNITY_CAPABILITIES, **overrides})


@pytest.fixture(autouse=True)
def _community_capabilities() -> Iterator[None]:
    reset_capability_resolver()
    yield
    reset_capability_resolver()


def test_user_providers_off_still_lets_the_super_admin_read_the_local_model():
    _capabilities_off("asr.user_providers")

    response = _client("super_admin").get(ACTIVE)

    assert response.status_code == 200, response.text
    assert "active_model" in response.json()


def test_user_providers_off_still_reaches_the_super_admin_set_route():
    _capabilities_off("asr.user_providers")

    response = _client("super_admin").post(SET, json={"model_name": "not-a-catalog-model"})

    # 400 is the handler's own refusal: the route was reached, which 404 would not be.
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "unknown_local_model"


def test_user_providers_off_still_hides_the_bring_your_own_provider_routes():
    _capabilities_off("asr.user_providers")

    assert _client("super_admin").get(PROVIDERS).status_code == 404


@pytest.mark.parametrize("route", ["get_active", "set"])
def test_model_selection_off_hides_the_local_model_routes(route):
    _capabilities_off("asr.model_selection")
    client = _client("super_admin")

    response = (
        client.get(ACTIVE) if route == "get_active" else client.post(SET, json={"model_name": "x"})
    )

    assert response.status_code == 404, response.text
