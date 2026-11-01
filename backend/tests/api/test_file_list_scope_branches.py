"""Scope pins for ``GET /api/files`` (``files/listing.py::list_media_files``).

Three branches no other test fails on: personal scope lists only org-less rows, a taken-down
file is hidden from a non-admin, and ``ownership=shared`` never lists the caller's own files.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.models.media import MediaFile
from app.models.organization import Organization
from app.services.platform_access import reset_tenancy_mode_cache


@pytest.fixture(autouse=True)
def _auto_mode(monkeypatch):
    monkeypatch.setattr(settings, "TENANCY_MODE", "auto")
    reset_tenancy_mode_cache()
    yield
    reset_tenancy_mode_cache()


def _file(db_session, owner, organization_id: int | None = None, **extra) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        organization_id=organization_id,
        filename=f"scope_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
        **extra,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _listed(client, headers, **params) -> set[str]:
    response = client.get("/api/files", headers=headers, params={"page_size": 100, **params})
    assert response.status_code == 200, response.text
    return {item["uuid"] for item in response.json()["items"]}


def test_personal_scope_lists_only_org_less_files(
    client, user_token_headers, normal_user, db_session
):
    org = Organization(name=f"scope-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    personal = _file(db_session, normal_user)
    in_org = _file(db_session, normal_user, organization_id=org.id)
    listed = _listed(client, user_token_headers)
    assert str(personal.uuid) in listed
    assert str(in_org.uuid) not in listed


def test_a_taken_down_file_is_hidden_from_its_owner(
    client, user_token_headers, normal_user, db_session
):
    visible = _file(db_session, normal_user)
    hidden = _file(db_session, normal_user, is_quarantined=True)
    listed = _listed(client, user_token_headers)
    assert str(visible.uuid) in listed
    assert str(hidden.uuid) not in listed


def test_shared_never_lists_the_callers_own_files(
    client, user_token_headers, normal_user, db_session
):
    own = _file(db_session, normal_user)
    assert str(own.uuid) in _listed(client, user_token_headers, ownership="all")
    assert str(own.uuid) not in _listed(client, user_token_headers, ownership="shared")
