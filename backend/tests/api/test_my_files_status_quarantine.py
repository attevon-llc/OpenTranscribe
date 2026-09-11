"""``GET /api/my-files/status`` must drop a recently-quarantined file (#817).

Three sub-queries build this summary. `recent_files` (last 24h) carried NO
quarantine filter at all — the real leak this fixes: a file quarantined within
24h of upload returned in full (uuid, filename, status, duration, file_size,
age) to its own owner. `status_counts` incremented `total` unconditionally,
including quarantined files, a weaker count leak fixed for uniformity.
`problem_files` (status in PROCESSING/PENDING/ERROR) is NOT actually reachable
by a quarantined file — quarantine overwrites `status` to the distinct
`QUARANTINED` value, which matches none of those three — so this test only
needs to cover the two real leaks plus the admin-bypass control.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile


def _make_recent_file(db_session, owner, **overrides) -> MediaFile:
    """A file owned by ``owner``, uploaded now (inside the `recent_files` 24h window)."""
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "my_files_status_probe.wav",
        "title": "my_files_status_probe",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": "completed",
        "is_public": False,
        "user_id": owner.id,
    }
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def test_recently_quarantined_file_absent_from_recent_files(
    client, user_token_headers, normal_user, db_session
):
    """The real leak: a file quarantined within 24h of upload must not appear
    in `recent_files`."""
    _make_recent_file(db_session, normal_user, is_quarantined=True)

    response = client.get("/api/my-files/status", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assert payload["recent_files"]["count"] == 0
    assert "my_files_status_probe" not in response.text


def test_status_total_excludes_the_quarantined_file(
    client, user_token_headers, normal_user, db_session
):
    """The weaker count leak: `status_counts.total` must exclude it too."""
    _make_recent_file(db_session, normal_user, is_quarantined=True)

    response = client.get("/api/my-files/status", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["status_counts"]["total"] == 0


def test_clean_recent_file_still_reported(client, user_token_headers, normal_user, db_session):
    """The control, without which the two tests above would also pass if the
    route were simply broken for everyone."""
    _make_recent_file(db_session, normal_user)

    response = client.get("/api/my-files/status", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assert payload["recent_files"]["count"] == 1
    assert payload["status_counts"]["total"] == 1
    assert "my_files_status_probe" in response.text


def test_an_admin_sees_their_own_quarantined_file_in_the_summary(
    client, admin_token_headers, admin_user, db_session
):
    """An admin who cannot see a quarantined file cannot review or release it —
    the same bypass every other takedown surface grants admins. This endpoint
    is always self-scoped by `user_id`, so the bypass only matters when the
    admin is looking at their OWN quarantined file."""
    _make_recent_file(db_session, admin_user, is_quarantined=True)

    response = client.get("/api/my-files/status", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assert payload["recent_files"]["count"] == 1
    assert payload["status_counts"]["total"] == 1
    assert "my_files_status_probe" in response.text
