"""Stand-ins shared by the ``auth_dependencies`` survivor-mutant suites."""

# mypy: disable-error-code="arg-type,comparison-overlap,index"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures declaring Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object -- the same shape test_lockout_survivor_mutants.py and
# test_banner_acknowledgment.py suppress for the same reason. Declared once here
# rather than as a cast at every call site.
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID
from uuid import uuid4

from starlette.requests import Request

from app.auth.direct_auth import create_access_token
from app.core.config import settings

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000ee"
CLIENT_IP = "198.51.100.7"
USER_AGENT = "pytest-survivor-suite"
ORDINARY_PATH = f"{settings.API_PREFIX}/files"
PROVIDER = "oidc"


def _user(**overrides: Any) -> Any:
    attrs: dict[str, Any] = {
        "id": 9001,
        "uuid": UUID(USER_UUID),
        "email": "survivor.person@example.com",
        "role": "user",
        "auth_type": "local",
        "is_active": True,
        "must_change_password": False,
        "account_expires_at": None,
        "banner_acknowledged_at": None,
        "approval_status": "approved",
        "external_org_id": None,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def _request(path: str = ORDINARY_PATH, *, matched_route: bool = True) -> Any:
    """A Request stand-in carrying a matched route, as Starlette would."""
    return SimpleNamespace(
        scope={"route": SimpleNamespace(path=path)} if matched_route else {},
        url=SimpleNamespace(path=path),
        client=SimpleNamespace(host=CLIENT_IP),
        headers={"User-Agent": USER_AGENT},
        state=SimpleNamespace(),
        cookies={},
    )


def _real_request(*, path: str = "/api/files", headers: list | None = None) -> Request:
    """A real Starlette ``Request``, for paths that read ``request.headers``/state."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "server": ("backend", 8080),
            "root_path": "",
            "path": path,
            "query_string": b"",
            "headers": headers or [(b"user-agent", b"pytest")],
            "client": ("10.0.0.9", 40000),
        }
    )


def _access_token(**claim_overrides: Any) -> str:
    claims = {"sub": USER_UUID, "role": "user"}
    claims.update(claim_overrides)
    return create_access_token(claims)


def _persist_user(db_session, **overrides: Any):
    """A real, committed-shape ``User`` row -- so a false ACCEPT is observable as
    a returned user object, not just "still 401s" (which a broken path can also
    produce, masking the very gap the test exists to catch)."""
    from app.models.user import User

    attrs: dict[str, Any] = {
        "uuid": uuid4(),
        "email": f"survivor-{uuid4().hex[:8]}@example.com",
        "hashed_password": "x",
        "role": "user",
        "is_active": True,
    }
    attrs.update(overrides)
    user = User(**attrs)
    db_session.add(user)
    db_session.flush()
    return user
