"""``/auth/demo*``: the passwordless demo sign-in and what the demo reports (#628, D5).

Driven through a small app that mounts the real router with the real limiter wiring and a
stand-in database, so a regression in the gating, the per-IP limit or the role check fails
here. The mint itself, the role refusal and the 404 are proven on a real database in
``tests/api/test_demo_session_mint.py``; what stays here needs no database: the
missing-user refusal, the per-IP limit and the two read endpoints.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi import Request
from fastapi import Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from app.api import rate_limit
from app.core.config import settings
from app.core.demo_info import DEMO_LICENSE_KEY

EMAIL = "demo@example.com"
LIMIT = 3


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class FakeDb:
    def __init__(self, users=(), file_rows=()):
        self.users = list(users)
        self.file_rows = list(file_rows)

    def query(self, *entities):
        return FakeQuery(self.file_rows if len(entities) > 1 else self.users)


def _user(role="user", active=True):
    return SimpleNamespace(
        id=7, uuid=uuid4(), email=EMAIL, role=role, is_active=active, auth_type="local"
    )


@pytest.fixture
def mod(monkeypatch):
    """The router module bound to a fresh in-memory limiter, with LIMIT sign-ins per minute."""
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_URL", "memory://")
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_PER_MINUTE", LIMIT)
    monkeypatch.setattr(settings, "DEMO_USER_EMAIL", EMAIL)
    monkeypatch.setattr(rate_limit, "limiter", rate_limit._create_limiter())
    from app.api.endpoints.auth import demo_session

    module = importlib.reload(demo_session)
    yield module
    monkeypatch.undo()
    importlib.reload(demo_session)


@pytest.fixture
def minted(mod, monkeypatch):
    calls: list[dict] = []

    def fake(request, db, user, user_uuid_str, role, user_agent, client_ip, auth_method=""):
        calls.append({"user": user, "role": role, "auth_method": auth_method})
        return JSONResponse({"access_token": "x", "token_type": "bearer"})

    monkeypatch.setattr(mod, "_generate_login_tokens", fake)
    return calls


def _client(mod, db: FakeDb, current_user=None) -> TestClient:
    app = FastAPI()
    app.state.limiter = rate_limit.limiter

    def on_limit(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, RateLimitExceeded)
        return rate_limit.rate_limit_exceeded_handler(request, exc)

    app.add_exception_handler(RateLimitExceeded, on_limit)
    app.include_router(mod.router, prefix="/api/auth")
    app.dependency_overrides[mod.get_db] = lambda: db
    if current_user is not None:
        app.dependency_overrides[mod.get_current_active_user] = lambda: current_user
    return TestClient(app)


@pytest.mark.parametrize("users", [[], [_user(active=False)]])
def test_a_missing_or_disabled_demo_user_is_unavailable_not_an_error(
    mod, minted, monkeypatch, users
):
    monkeypatch.setattr(settings, "DEMO_MODE", True)

    response = _client(mod, FakeDb(users)).post("/api/auth/demo/session")

    assert response.status_code == 503
    assert minted == []


def test_the_session_endpoint_is_limited_per_client_ip(mod, minted, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    client = _client(mod, FakeDb([_user()]))

    statuses = [client.post("/api/auth/demo/session").status_code for _ in range(LIMIT + 2)]

    assert statuses == [200] * LIMIT + [429, 429]


def test_the_info_endpoint_says_nothing_outside_demo_mode(mod, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)

    body = _client(mod, FakeDb()).get("/api/auth/demo").json()

    assert body == {"enabled": False, "next_reset_at": None, "upgraded_at": None}


def test_the_info_endpoint_reports_the_next_reset_and_last_upgrade(mod, monkeypatch, tmp_path):
    status = tmp_path / "demo-status.json"
    status.write_text(
        json.dumps({"tag": "v0.7.0", "result": "upgraded", "at": "2026-11-08T10:15:00+00:00"})
    )
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    monkeypatch.setattr(settings, "DEMO_STATUS_FILE", str(status))

    body = _client(mod, FakeDb()).get("/api/auth/demo").json()

    reset = datetime.fromisoformat(body["next_reset_at"])
    assert reset > datetime.now(UTC)
    assert (reset.hour, reset.minute) == (4, 0)
    assert datetime.fromisoformat(body["upgraded_at"]) == datetime(2026, 11, 8, 10, 15, tzinfo=UTC)


def test_attributions_serve_the_licence_the_seeder_recorded(mod, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    file_uuid = uuid4()
    meta = {
        DEMO_LICENSE_KEY: {
            "license": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "attribution": "AMI Corpus",
            "source_url": "https://groups.inf.ed.ac.uk/ami/corpus/",
        }
    }
    db = FakeDb(file_rows=[(file_uuid, meta)])

    response = _client(mod, db, current_user=_user()).get("/api/auth/demo/attributions")

    assert response.status_code == 200
    assert response.json()["items"] == [{"file_uuid": str(file_uuid), **meta[DEMO_LICENSE_KEY]}]


def test_attributions_do_not_exist_outside_demo_mode(mod, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)

    response = _client(mod, FakeDb(), current_user=_user()).get("/api/auth/demo/attributions")

    assert response.status_code == 404
