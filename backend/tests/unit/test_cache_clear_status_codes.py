"""The two cache-clear routes answer a locked/incomplete purge with the SAME status (issue #1357).

``DELETE /files/{uuid}/cache`` turned every storage failure into a 500 while
``POST /admin/settings/cache-config/clear`` answered the same ``StorageDeleteIncompleteError``
with a 503. A purge that left objects behind because of a lock or a refused delete is a
conflict the caller can resolve (409); only an actual outage is a 5xx (503).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast

import pytest
from fastapi import HTTPException

from app.api.endpoints import admin as admin_api
from app.api.endpoints import files as files_api
from app.services import cache_management_service
from app.services.storage_versions import StorageDeleteIncompleteError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.api.deps_context import RequestContext
    from app.models.user import User

_INCOMPLETE = StorageDeleteIncompleteError("cache", ["derived/a@v1", "derived/b@v2"], "left")


class _Db:
    def commit(self) -> None:
        return None


def _file_route(monkeypatch, clear):
    monkeypatch.setattr(
        files_api,
        "get_media_file_by_uuid",
        lambda *a, **k: SimpleNamespace(id=7, filename="talk.mp4"),
    )
    monkeypatch.setattr(files_api, "_clear_derived_cache", clear)
    return lambda: files_api.clear_video_cache(
        "u",
        cast("Session", _Db()),
        cast("User", SimpleNamespace(id=1)),
        cast("RequestContext", SimpleNamespace(bypass=None, org_id=None)),
    )


def _admin_route(monkeypatch, clear):
    monkeypatch.setattr(cache_management_service, "clear_derived_cache", clear)
    ctx = SimpleNamespace(bypass=SimpleNamespace(require_deployment_scope=lambda *_: None))
    return lambda: admin_api.clear_cache_now(
        cast("Session", _Db()),
        cast("RequestContext", ctx),
        cast("User", SimpleNamespace(email="a@example.com")),
    )


def _raiser(exc):
    def _raise(*_a, **_k):
        raise exc

    return _raise


@pytest.mark.parametrize("make_route", [_file_route, _admin_route], ids=["file", "admin"])
def test_incomplete_purge_is_409_on_both_routes(monkeypatch, make_route):
    route = make_route(monkeypatch, _raiser(_INCOMPLETE))
    with pytest.raises(HTTPException) as caught:
        route()
    assert caught.value.status_code == 409


@pytest.mark.parametrize("make_route", [_file_route, _admin_route], ids=["file", "admin"])
def test_storage_outage_is_503_on_both_routes(monkeypatch, make_route):
    route = make_route(monkeypatch, _raiser(ConnectionError("storage unreachable")))
    with pytest.raises(HTTPException) as caught:
        route()
    assert caught.value.status_code == 503


def test_successful_purge_still_succeeds(monkeypatch):
    assert _file_route(monkeypatch, lambda *a: None)() is None
    monkeypatch.setattr(cache_management_service, "clear_derived_cache", lambda: 3)
    assert _admin_route(monkeypatch, lambda: 3)().deleted == 3
