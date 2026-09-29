"""GET /api/files/{uuid} must not reveal whether another tenant's file exists.

A file the caller cannot access (another user's, another org's) must answer exactly like a
UUID that does not exist: 404 with the same detail. A 403 would confirm existence and let a
caller enumerate files by UUID. Each denial is paired with an own-file control so a route
that is simply broken for everyone cannot pass.
"""

from __future__ import annotations

import uuid as uuid_pkg

from app.models.media import MediaFile


def _file(db, owner) -> MediaFile:
    mf = MediaFile(
        user_id=owner.id,
        filename="owned.wav",
        storage_path=f"test/{uuid_pkg.uuid4().hex}.wav",
        file_size=1024,
        content_type="audio/wav",
    )
    db.add(mf)
    db.commit()
    db.refresh(mf)
    return mf


def test_another_users_file_is_indistinguishable_from_a_missing_one(
    client, db_session, normal_user, other_user, other_user_auth_headers
):
    foreign = _file(db_session, normal_user)

    denied = client.get(f"/api/files/{foreign.uuid}", headers=other_user_auth_headers)
    missing = client.get(f"/api/files/{uuid_pkg.uuid4()}", headers=other_user_auth_headers)

    assert missing.status_code == 404
    assert denied.status_code == 404
    assert denied.json() == missing.json()


def test_own_file_is_still_served(client, db_session, normal_user, user_token_headers):
    own = _file(db_session, normal_user)

    response = client.get(f"/api/files/{own.uuid}", headers=user_token_headers)

    assert response.status_code == 200, response.text
