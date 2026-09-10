"""``GET /api/speakers/{speaker_uuid}`` must hide a quarantined file's speaker (#817).

Its sibling ``get_speaker_cross_media_occurrences`` (same file, ``:1267``) already
calls ``takedown_service.is_hidden_for`` after resolving the speaker, guarded by
this exact comment: "A quarantined file is hidden from its own owner ... without
this a non-admin whose OWN file got taken down could still reach its speaker's
[data] through this endpoint even though the file itself 404s." This endpoint,
one screen over, never got the same guard — a takedown left the speaker's
display name, confidence and verification state fully readable by the file's
own owner.

404, not 403: matching every other takedown surface, "this file exists but you
may not see it" is itself a disclosure.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker


def _make_file_and_speaker(db_session, owner, **file_overrides) -> tuple[MediaFile, Speaker]:
    """A file owned by ``owner``, plus one diarized speaker on it."""
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "speaker_quarantine_probe.wav",
        "title": "speaker_quarantine_probe",
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
        display_name="Zylofenix",
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)
    return media_file, speaker


def test_speaker_detail_is_refused_for_a_quarantined_file(
    client, user_token_headers, normal_user, db_session
):
    """The finding: a takedown must revoke the speaker detail too."""
    _media_file, speaker = _make_file_and_speaker(db_session, normal_user, is_quarantined=True)

    response = client.get(f"/api/speakers/{speaker.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert "Zylofenix" not in response.text


def test_speaker_detail_still_works_for_a_file_that_is_not_quarantined(
    client, user_token_headers, normal_user, db_session
):
    """The control, without which the test above would also pass if the route
    were simply broken for everyone."""
    _media_file, speaker = _make_file_and_speaker(db_session, normal_user)

    response = client.get(f"/api/speakers/{speaker.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["display_name"] == "Zylofenix"


def test_an_admin_can_still_reach_their_own_quarantined_speaker(
    client, admin_token_headers, admin_user, db_session
):
    """An admin who cannot see a quarantined file cannot review or release it —
    the same bypass every other takedown surface grants admins. `get_speaker`
    has no blanket admin bypass on the underlying permission check, so the
    admin must OWN the file for `get_file_permission` to succeed."""
    _media_file, speaker = _make_file_and_speaker(db_session, admin_user, is_quarantined=True)

    response = client.get(f"/api/speakers/{speaker.uuid}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["display_name"] == "Zylofenix"
