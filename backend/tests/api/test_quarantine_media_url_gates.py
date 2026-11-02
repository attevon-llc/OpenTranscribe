"""A taken-down file's media cannot be reached by an admin the platform bypass does not reach.

Multi-tenant deployment (``support_world``): a platform admin holds no support-access grant
for ``org``, so ``bypass.allows`` is False for that tenant's files. Review visibility of a
quarantined file is part of that bypass, never a separate exemption, so the PUBLIC branch
(which skips the tenant gate on purpose) must not hand a quarantined public file of another
tenant to such an admin (the thumbnail route; `get_file_by_uuid_with_permission` applies the
same rule to an `allow_public` caller, of which there is none today).

``set_file_urls`` is also covered on its own: it is the one place list and detail responses
mint a thumbnail URL, and it refuses a taken-down file for a non-admin whatever its caller
filtered.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi import Response

from app.services.platform_bypass import PlatformBypass

APP = pathlib.Path(__file__).resolve().parents[2] / "app"


@pytest.fixture
def stub_thumbnail(monkeypatch):
    """Serve a fixed body instead of reading storage; the authorization is what is tested."""
    monkeypatch.setattr(
        "app.api.endpoints.files.get_thumbnail_streaming_response",
        lambda _file: Response(content=b"thumb", media_type="image/jpeg"),
    )


def _public(world, *, quarantined: bool):
    file = world.file
    file.is_public = True
    file.thumbnail_path = f"{file.storage_path}.thumb.jpg"
    file.is_quarantined = quarantined
    world.db.commit()
    return file


@pytest.mark.parametrize("quarantined", [True, False])
def test_an_unreached_admin_gets_no_thumbnail_of_a_quarantined_public_file(
    support_world, stub_thumbnail, quarantined
):
    w = support_world
    file = _public(w, quarantined=quarantined)

    response = w.client.get(f"/api/files/{file.uuid}/thumbnail", headers=w.admin_headers)

    assert response.status_code == (404 if quarantined else 200), response.text


def test_the_owner_still_gets_a_404_for_their_own_quarantined_file(support_world, stub_thumbnail):
    w = support_world
    file = _public(w, quarantined=True)

    response = w.client.get(f"/api/files/{file.uuid}/thumbnail", headers=w.owner_headers)

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("quarantined", "who", "hidden"),
    [(True, "owner", True), (True, "admin", False), (False, "owner", False)],
)
def test_the_export_replay_check_reads_the_live_quarantine_state(
    support_world, monkeypatch, quarantined, who, hidden
):
    from app.services import media_revocation_cache as cache

    w = support_world
    w.file.is_quarantined = quarantined
    w.db.commit()
    # The check opens its own short session; point it at the test's.
    from contextlib import contextmanager

    @contextmanager
    def _scope():
        yield w.db

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope)
    requester = w.owner if who == "owner" else w.admin

    assert cache.hidden_from_requester([w.file.id], int(requester.id)) is hidden


class TestSetFileUrls:
    def _file(self, *, quarantined: bool):
        import uuid

        from app.models.media import MediaFile

        return MediaFile(
            uuid=uuid.uuid4(),
            storage_path="user_1/file_1/original.mp4",
            thumbnail_path="user_1/file_1/thumb.jpg",
            is_quarantined=quarantined,
        )

    def test_a_quarantined_file_gets_no_urls_without_admin_review(self):
        from app.api.endpoints.files.crud import set_file_urls

        file = self._file(quarantined=True)
        set_file_urls(file, PlatformBypass.none())

        assert (file.download_url, file.thumbnail_url) == (None, None)

    def test_an_ordinary_file_gets_its_urls(self):
        from app.api.endpoints.files.crud import set_file_urls

        file = self._file(quarantined=False)
        set_file_urls(file, PlatformBypass.none())

        assert file.download_url == f"/api/files/{file.uuid}/prepare-download"
        assert file.thumbnail_url is not None


def _thumbnail_url_writers() -> list[str]:
    found = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                targets = node.targets if isinstance(node, ast.Assign) else []
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "thumbnail_url":
                        found.append(f"{path.relative_to(APP)}::{fn.name}")
    return sorted(set(found))


def test_only_set_file_urls_stamps_a_thumbnail_url_on_a_response_row():
    """A second writer would mint thumbnail URLs past the quarantine check inside it."""
    assert _thumbnail_url_writers() == ["api/endpoints/files/crud.py::set_file_urls"]


def test_the_writer_scan_actually_finds_assignments():
    tree = ast.parse("def f(row):\n    row.thumbnail_url = 'x'\n")
    (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)]
    assert assigns and isinstance(assigns[0].targets[0], ast.Attribute)
