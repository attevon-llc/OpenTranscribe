"""Drive the real ``/ws`` endpoint in-process, for handshake and live-socket tests.

Plain helpers rather than a fixture plugin: each test file declares its own small
fixtures on top of these, so no change to ``tests/conftest.py``'s plugin list is needed.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid as uuid_pkg
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import websockets as ws_module
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.session import InMemoryStore
from app.auth.token_service import USER_REVOCATION_EPOCH_PREFIX
from app.auth.token_service import TokenService
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.user import User
from tests.jwt_compat import jwt

#: The handshake's refusal for a token it will not accept.
INVALID_TOKEN_CLOSE = 4003


def isolated_revocation(monkeypatch: Any) -> TokenService:
    """A ``TokenService`` on a private in-memory store, wired into the WebSocket module."""
    service = TokenService()
    service._store = InMemoryStore()
    monkeypatch.setattr(ws_module, "token_service", service)
    monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", True)
    return service


@contextlib.contextmanager
def ws_test_client(db_session: Session, monkeypatch: Any) -> Iterator[TestClient]:
    """A TestClient for the real endpoint, using the test's own DB session."""

    @contextlib.contextmanager
    def _test_session_scope():
        yield db_session

    monkeypatch.setattr(ws_module, "session_scope", _test_session_scope)
    monkeypatch.setattr(ws_module, "setup_redis", lambda: None)
    monkeypatch.setattr(ws_module, "manager", ws_module.ConnectionManager())
    app = FastAPI()
    app.add_api_websocket_route("/ws", ws_module.websocket_endpoint)
    with TestClient(app) as client:
        yield client


def make_user(db_session: Session, **fields: Any) -> User:
    """A committed, active local account; ``fields`` override any column."""
    unique = uuid_pkg.uuid4().hex[:8]
    values: dict[str, Any] = {
        "email": f"ws-{unique}@example.com",
        "full_name": "Socket Person",
        "hashed_password": get_password_hash("irrelevant-Passphrase99!"),
        "role": "user",
        "auth_type": "local",
        "is_active": True,
        "is_superuser": False,
    }
    values.update(fields)
    user = User(**values)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def access_token(user: User, *, issued_at: int | None = None) -> str:
    """An otherwise-valid access token with a chosen ``iat`` (now by default)."""
    return jwt.encode(
        {
            "sub": str(user.uuid),
            "type": TOKEN_TYPE_ACCESS,
            "jti": uuid_pkg.uuid4().hex,
            "iat": int(time.time()) if issued_at is None else issued_at,
            "exp": int(time.time()) + 600,
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def stamp_epoch(service: TokenService, user: User, *, seconds_ago: int) -> int:
    """Write a revocation epoch directly, without the fan-out a real revocation sends."""
    epoch = int(time.time()) - seconds_ago
    service.store.set(f"{USER_REVOCATION_EPOCH_PREFIX}{user.uuid}", str(epoch), ex=600)
    return epoch


def authenticate_frame(token: str) -> str:
    return json.dumps({"type": "authenticate", "token": token})


def cookie_header(token: str) -> dict[str, str]:
    return {"cookie": f"access_token={token}"}
