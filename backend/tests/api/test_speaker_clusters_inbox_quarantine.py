"""``GET /api/speaker-clusters/unverified/inbox`` must drop quarantined speakers (#817).

``get_unverified_speakers`` built its query straight off ``Speaker`` with no
notion of ``MediaFile.is_quarantined``, so a takedown left the speaker's
suggested/display name and media file title fully readable in the inbox. The
exclusion is applied BEFORE ``.count()`` on purpose: filtering ``items`` alone
after pagination would still leak the true total and silently shorten pages —
this test asserts total/pages, not just page contents, because that is the
mistake this class of fix keeps making.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker


def _make_unverified_speaker(db_session, owner, **file_overrides) -> tuple[MediaFile, Speaker]:
    """A file owned by ``owner`` plus one unverified, unprofiled speaker on it —
    the exact shape `get_unverified_speakers` selects for the inbox."""
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "inbox_quarantine_probe.wav",
        "title": "inbox_quarantine_probe",
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
        display_name=None,
        verified=False,
        profile_id=None,
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)
    return media_file, speaker


def test_quarantined_speakers_title_absent_from_inbox(
    client, user_token_headers, normal_user, db_session
):
    """The finding: a takedown must remove the speaker from the inbox listing."""
    _make_unverified_speaker(db_session, normal_user, is_quarantined=True)

    response = client.get("/api/speaker-clusters/unverified/inbox", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert "inbox_quarantine_probe" not in response.text


def test_total_and_pages_exclude_the_quarantined_speaker_too(
    client, user_token_headers, normal_user, db_session
):
    """The important half: total AND pages must exclude it too, not just the
    page's item list — the exclusion must run BEFORE `.count()`."""
    _make_unverified_speaker(db_session, normal_user, is_quarantined=True)

    response = client.get(
        "/api/speaker-clusters/unverified/inbox?per_page=1", headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assert payload["total"] == 0
    assert payload["pages"] == 1
    assert payload["items"] == []


def test_clean_speaker_still_in_inbox(client, user_token_headers, normal_user, db_session):
    """The control, without which the test above would also pass if the route
    were simply broken for everyone."""
    _make_unverified_speaker(db_session, normal_user)

    response = client.get("/api/speaker-clusters/unverified/inbox", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assert payload["total"] == 1
    assert "inbox_quarantine_probe" in response.text


def test_an_admin_sees_their_own_quarantined_speaker_in_the_inbox(
    client, admin_token_headers, admin_user, db_session
):
    """An admin who cannot see a quarantined file cannot review or release it —
    the same bypass every other takedown surface grants admins."""
    _make_unverified_speaker(db_session, admin_user, is_quarantined=True)

    response = client.get("/api/speaker-clusters/unverified/inbox", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert "inbox_quarantine_probe" in response.text
