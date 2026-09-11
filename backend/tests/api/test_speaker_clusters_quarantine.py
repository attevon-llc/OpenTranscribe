"""A quarantined file's media must not be reachable through the speaker plane (#817).

``GET /api/speaker-clusters/speakers/{uuid}/media-preview`` resolves its file via
``speaker.media_file`` after an ownership check on the *speaker* row, so it never
passed through ``get_file_by_uuid_with_permission`` — the chokepoint that makes a
taken-down file 404 on every ``files/`` read surface. It then minted a **presigned
MinIO URL to the raw source media**, valid for ``MEDIA_URL_EXPIRE_SECONDS`` (6 h by
default), plus the filename, the display title and segment timestamps.

That is the same class #817 fixed for the thumbnail route, one router over, and with
a worse payload: a thumbnail is a frame, this is the recording. The guarded sibling
is ``speakers.get_speaker_cross_media_occurrences``, which already calls
``takedown_service.is_hidden_for`` for exactly this reason; the copy in
``speaker_clusters.py`` simply never got it.

404, not 403 — matching every other takedown surface, because "this file exists but
you may not see it" is itself a disclosure about a taken-down file.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker


def _make_file_and_speaker(db_session, owner, **file_overrides) -> tuple[MediaFile, Speaker]:
    """A completed file owned by ``owner``, plus one diarized speaker on it."""
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "cluster_preview.wav",
        "title": "cluster_preview",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": "completed",
        "is_public": False,
        "user_id": owner.id,
    }
    defaults.update(file_overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    speaker = Speaker(
        uuid=uuid.uuid4(),
        user_id=owner.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        display_name="Alex",
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)
    return media_file, speaker


def test_media_preview_is_refused_for_a_quarantined_file(
    client, user_token_headers, normal_user, db_session
):
    """The finding: a takedown must revoke the presigned media URL too."""
    _media_file, speaker = _make_file_and_speaker(db_session, normal_user, is_quarantined=True)

    response = client.get(
        f"/api/speaker-clusters/speakers/{speaker.uuid}/media-preview",
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    # Not merely the status: the point is that no URL and no filename escaped.
    body = response.text
    assert "media_url" not in body
    assert "cluster_preview" not in body


def test_media_preview_still_works_for_a_file_that_is_not_quarantined(
    client, user_token_headers, normal_user, db_session
):
    """The control, without which the test above would also pass if the route were
    simply broken for everyone."""
    _media_file, speaker = _make_file_and_speaker(db_session, normal_user)

    response = client.get(
        f"/api/speaker-clusters/speakers/{speaker.uuid}/media-preview",
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assert payload["speaker_uuid"] == str(speaker.uuid)
    assert payload["file_name"] == "cluster_preview.wav"


def test_an_admin_can_still_reach_a_quarantined_files_preview(
    client, admin_token_headers, admin_user, db_session
):
    """An admin who cannot see a quarantined file cannot review or release it —
    the same bypass ``is_hidden_for`` grants on every other takedown surface."""
    _media_file, speaker = _make_file_and_speaker(db_session, admin_user, is_quarantined=True)

    response = client.get(
        f"/api/speaker-clusters/speakers/{speaker.uuid}/media-preview",
        headers=admin_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
