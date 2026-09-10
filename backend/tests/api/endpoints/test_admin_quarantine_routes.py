"""Functional (HTTP) tests for the admin quarantine/takedown routes (``admin.py``).

``backend/tests/test_takedown_quarantine.py`` is thorough but calls
``quarantine_file()`` / ``release_file()`` / ``exclude_quarantined()`` directly as
service functions — it never issues a real request. This file closes that gap for
the three HTTP endpoints:

* ``GET  /api/admin/files/quarantined``
* ``POST /api/admin/files/{file_uuid}/quarantine``
* ``POST /api/admin/files/{file_uuid}/release``

This is the DMCA/safe-harbor takedown surface: a relaxed admin dependency on the
list route would leak takedown reasons and counter-notice contacts to any
authenticated user, and a wrong field name on the action routes would silently
leave a "quarantined" file fully accessible.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import status

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User


def _mk_owner(db) -> User:
    from app.core.security import get_password_hash

    uid = str(uuid_pkg.uuid4())[:8]
    user = User(
        email=f"quarantine_owner_{uid}@example.com",
        full_name="Quarantine Test Owner",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_file(db, *, owner: User, quarantined: bool = False) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    f = MediaFile(
        uuid=fuuid,
        filename=f"quarantine_target_{str(fuuid)[:8]}.mp4",
        # No real object in storage — keeps the best-effort S3 legal-hold a
        # harmless no-op, same pattern as test_takedown_quarantine.py.
        storage_path="",
        content_type="video/mp4",
        file_size=1000,
        user_id=owner.id,
        status=FileStatus.COMPLETED,
    )
    if quarantined:
        f.is_quarantined = True
        f.quarantine_reason = "pre-seeded for release test"
        f.legal_hold = True
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


@pytest.fixture
def owned_file(db_session):
    owner = _mk_owner(db_session)
    return _mk_file(db_session, owner=owner)


@pytest.fixture
def quarantined_file(db_session):
    owner = _mk_owner(db_session)
    return _mk_file(db_session, owner=owner, quarantined=True)


# ---------------------------------------------------------------------------
# Privilege tier
# ---------------------------------------------------------------------------


def test_non_admin_is_refused_on_list_quarantined(client, user_token_headers, quarantined_file):
    """A plain user must not see the takedown queue (reasons, contacts, etc.)."""
    response = client.get("/api/admin/files/quarantined", headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_non_admin_is_refused_on_quarantine(client, user_token_headers, owned_file):
    response = client.post(
        f"/api/admin/files/{owned_file.uuid}/quarantine",
        headers=user_token_headers,
        json={"reason": "DMCA-99999"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_non_admin_is_refused_on_release(client, user_token_headers, quarantined_file):
    response = client.post(
        f"/api/admin/files/{quarantined_file.uuid}/release",
        headers=user_token_headers,
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# GET /files/quarantined
# ---------------------------------------------------------------------------


def test_admin_can_list_quarantined_files(client, admin_token_headers, quarantined_file):
    response = client.get("/api/admin/files/quarantined", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    uuids = [f["uuid"] for f in body["files"]]
    assert str(quarantined_file.uuid) in uuids
    matched = next(f for f in body["files"] if f["uuid"] == str(quarantined_file.uuid))
    assert matched["quarantine_reason"] == "pre-seeded for release test"
    assert matched["legal_hold"] is True


# ---------------------------------------------------------------------------
# POST /files/{uuid}/quarantine
# ---------------------------------------------------------------------------


def test_admin_quarantine_flips_real_db_state(client, admin_token_headers, owned_file, db_session):
    """Assert real DB state, not just a 200 — the audit's own emphasis."""
    response = client.post(
        f"/api/admin/files/{owned_file.uuid}/quarantine",
        headers=admin_token_headers,
        json={"reason": "DMCA-12345", "legal_hold": True},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["is_quarantined"] is True
    assert body["legal_hold"] is True

    db_session.expire_all()
    reloaded = db_session.query(MediaFile).filter(MediaFile.uuid == owned_file.uuid).one()
    assert reloaded.is_quarantined is True
    assert reloaded.quarantine_reason == "DMCA-12345"
    assert reloaded.legal_hold is True
    assert reloaded.quarantined_by is not None
    assert reloaded.quarantined_at is not None


# ---------------------------------------------------------------------------
# POST /files/{uuid}/release
# ---------------------------------------------------------------------------


def test_admin_release_flips_real_db_state_back(
    client, admin_token_headers, quarantined_file, db_session
):
    response = client.post(
        f"/api/admin/files/{quarantined_file.uuid}/release",
        headers=admin_token_headers,
        json={"clear_legal_hold": True},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["is_quarantined"] is False
    assert body["legal_hold"] is False

    db_session.expire_all()
    reloaded = db_session.query(MediaFile).filter(MediaFile.uuid == quarantined_file.uuid).one()
    assert reloaded.is_quarantined is False
    assert reloaded.legal_hold is False


def test_admin_release_on_a_never_quarantined_file_is_409(client, admin_token_headers, owned_file):
    """Confirms the actual current behavior: releasing a non-quarantined file is a
    409, not a silent no-op 200 (which would make the response indistinguishable
    from a real release for a caller that mistyped the uuid)."""
    response = client.post(
        f"/api/admin/files/{owned_file.uuid}/release",
        headers=admin_token_headers,
    )
    assert response.status_code == status.HTTP_409_CONFLICT
    assert "not quarantined" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# release(clear_legal_hold=False) is a real, distinct state (issue #825)
# ---------------------------------------------------------------------------


def test_release_with_hold_kept_produces_the_intermediate_state(
    client, admin_token_headers, quarantined_file, db_session
):
    """release(clear_legal_hold=False): quarantine lifted (visible again to normal
    users), legal-hold kept (still undeletable). This is a real, valid state --
    not a mistake -- so the DB must show it correctly."""
    response = client.post(
        f"/api/admin/files/{quarantined_file.uuid}/release",
        headers=admin_token_headers,
        json={"clear_legal_hold": False},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["is_quarantined"] is False
    assert body["legal_hold"] is True

    db_session.expire_all()
    reloaded = db_session.query(MediaFile).filter(MediaFile.uuid == quarantined_file.uuid).one()
    assert reloaded.is_quarantined is False
    assert reloaded.legal_hold is True


def test_released_but_held_file_is_absent_from_the_default_quarantine_list(
    client, admin_token_headers, quarantined_file
):
    """The default GET /files/quarantined is the takedown QUEUE (is_quarantined
    True) -- a released-but-held file has left the queue by design, so it must NOT
    reappear there by default. This pins the unchanged default behavior so the
    next test's `include_legal_holds=true` is proven to be additive, not a
    silent widening of what every existing caller already gets."""
    client.post(
        f"/api/admin/files/{quarantined_file.uuid}/release",
        headers=admin_token_headers,
        json={"clear_legal_hold": False},
    )
    response = client.get("/api/admin/files/quarantined", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    uuids = [f["uuid"] for f in response.json()["files"]]
    assert str(quarantined_file.uuid) not in uuids


def test_released_but_held_file_is_listable_with_include_legal_holds(
    client, admin_token_headers, quarantined_file
):
    """Issue #825: release(clear_legal_hold=False) previously produced a state no
    endpoint could list at all -- an admin who kept a hold on a released file had
    no way to find it again through the API a review-queue client would use.
    ``include_legal_holds=true`` surfaces it, and the response's own
    ``is_quarantined`` field lets a caller tell the two states apart within one
    list."""
    client.post(
        f"/api/admin/files/{quarantined_file.uuid}/release",
        headers=admin_token_headers,
        json={"clear_legal_hold": False},
    )
    response = client.get(
        "/api/admin/files/quarantined?include_legal_holds=true",
        headers=admin_token_headers,
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    matched = next((f for f in body["files"] if f["uuid"] == str(quarantined_file.uuid)), None)
    assert matched is not None, (
        "released-but-held file did not appear with include_legal_holds=true"
    )
    assert matched["is_quarantined"] is False
    assert matched["legal_hold"] is True


def test_include_legal_holds_does_not_drop_still_quarantined_files(
    client, admin_token_headers, quarantined_file
):
    """A currently-quarantined file must still appear when the broader filter is
    requested -- include_legal_holds is additive, not a replacement filter."""
    response = client.get(
        "/api/admin/files/quarantined?include_legal_holds=true",
        headers=admin_token_headers,
    )
    assert response.status_code == status.HTTP_200_OK
    uuids = [f["uuid"] for f in response.json()["files"]]
    assert str(quarantined_file.uuid) in uuids
