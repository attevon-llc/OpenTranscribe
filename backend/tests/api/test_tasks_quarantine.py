"""A quarantined file's task rows must not surface on the task-list surfaces (#817).

``GET /api/tasks`` and ``GET /api/tasks/{task_id}`` build their response straight
from ``MediaFile`` rows (``_get_user_media_files`` / ``_latest_task_by_file`` /
``_get_media_file_by_id``) rather than through ``get_file_by_uuid_with_permission``
— the chokepoint that makes a taken-down file 404 on every ``files/`` read surface
— so none of the three had ever learned about ``is_quarantined``. A takedown left
the file's filename, status and progress fully visible in the task list, and its
task detail resolvable, to the file's own owner.

404, not filtered-out-with-a-different-message: matching every other takedown
surface, "this file exists but you may not see it" is itself a disclosure.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile


def _make_media_file(db_session, owner, **overrides) -> MediaFile:
    """A media file owned by ``owner``, with a distinctive filename to assert on."""
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "tasks_quarantine_probe.wav",
        "title": "tasks_quarantine_probe",
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


def test_quarantined_file_absent_from_task_list(
    client, user_token_headers, normal_user, db_session
):
    """The finding: a takedown must remove the file from the caller's task list too."""
    _make_media_file(db_session, normal_user, is_quarantined=True)

    response = client.get("/api/tasks", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assert payload["total"] == 0
    assert "tasks_quarantine_probe" not in response.text


def test_quarantined_file_task_detail_is_404(client, user_token_headers, normal_user, db_session):
    """The legacy `task_<media_file_id>` id form must also 404 for a taken-down file."""
    media_file = _make_media_file(db_session, normal_user, is_quarantined=True)

    response = client.get(f"/api/tasks/task_{media_file.id}", headers=user_token_headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert "tasks_quarantine_probe" not in response.text


def test_task_list_and_detail_still_work_for_a_file_that_is_not_quarantined(
    client, user_token_headers, normal_user, db_session
):
    """The control, without which the two tests above would also pass if the
    routes were simply broken for everyone."""
    media_file = _make_media_file(db_session, normal_user)

    list_response = client.get("/api/tasks", headers=user_token_headers)
    assert list_response.status_code == status.HTTP_200_OK, list_response.text
    assert list_response.json()["total"] == 1
    assert "tasks_quarantine_probe" in list_response.text

    detail_response = client.get(f"/api/tasks/task_{media_file.id}", headers=user_token_headers)
    assert detail_response.status_code == status.HTTP_200_OK, detail_response.text
    assert detail_response.json()["media_file"]["filename"] == "tasks_quarantine_probe.wav"


def test_an_admin_can_still_see_their_own_quarantined_files_task(
    client, admin_token_headers, admin_user, db_session
):
    """An admin who cannot see a quarantined file cannot review or release it —
    the same bypass every other takedown surface grants admins."""
    media_file = _make_media_file(db_session, admin_user, is_quarantined=True)

    # Admin "see all" is deliberately unscoped by owner (the dev DB carries other
    # accounts' files too), so assert presence rather than an exact total.
    list_response = client.get("/api/tasks", headers=admin_token_headers)
    assert list_response.status_code == status.HTTP_200_OK, list_response.text
    assert "tasks_quarantine_probe" in list_response.text

    detail_response = client.get(f"/api/tasks/task_{media_file.id}", headers=admin_token_headers)
    assert detail_response.status_code == status.HTTP_200_OK, detail_response.text
