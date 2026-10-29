"""Characterization of ``ErrorHandler.internal_error`` and one endpoint that uses it.

Pins the wire contract across the move of ``error_handlers`` from ``app.utils`` to
``app.api``: status 500, the exact client-facing detail, and no exception text.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.tags import router as tags_router
from app.api.error_handlers import ErrorHandler
from app.db.base import get_db
from app.models.user import User
from app.services.platform_bypass import PlatformBypass
from app.services.request_context import RequestContext

_SECRET = "relation tags_secret_table does not exist"


def test_internal_error_default_is_opaque_500() -> None:
    exc = ErrorHandler.internal_error()
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 500
    assert exc.detail == "Internal server error"


def test_internal_error_uses_the_given_detail_verbatim() -> None:
    exc = ErrorHandler.internal_error("Could not clean up unused tags.")
    assert exc.status_code == 500
    assert exc.detail == "Could not clean up unused tags."


def test_endpoint_using_internal_error_returns_opaque_500_bytes() -> None:
    admin = User(id=1, role="admin")
    ctx = RequestContext(user=admin, bypass=PlatformBypass(user=admin))
    app = FastAPI()
    app.include_router(tags_router, prefix="/api/tags")
    app.dependency_overrides[get_db] = lambda: MagicMock()
    app.dependency_overrides[get_current_active_user] = lambda: admin
    app.dependency_overrides[get_current_context] = lambda: ctx

    with patch(
        "app.api.endpoints.tags.crud.cleanup_unreferenced_tags",
        side_effect=RuntimeError(_SECRET),
    ):
        resp = TestClient(app).delete("/api/tags/cleanup")

    assert resp.status_code == 500
    assert resp.content == b'{"detail":"Could not clean up unused tags."}'
    assert _SECRET not in resp.text
