"""``GET /api/speaker-profiles/profiles/{uuid}/occurrences`` must drop quarantined
files (#817).

``find_speaker_occurrences`` (``speaker_matching_service.py``) built its occurrence
list straight off ``Speaker`` rows with no notion of ``MediaFile.is_quarantined`` —
so a takedown left the file's filename, title and upload time fully readable to the
profile owner via the profile's occurrence list, the same class of leak already
fixed on the sibling speaker-detail and cross-media-occurrence routes.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile


def _make_profile_with_occurrence(
    db_session, owner, *, profile=None, **file_overrides
) -> tuple[SpeakerProfile, MediaFile, Speaker]:
    """A profile owned by ``owner``, plus one speaker/file occurrence attached to it."""
    if profile is None:
        profile = SpeakerProfile(
            uuid=uuid.uuid4(), user_id=owner.id, name=f"Profile {uuid.uuid4().hex[:8]}"
        )
        db_session.add(profile)
        db_session.commit()
        db_session.refresh(profile)

    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "profile_occurrence_probe.wav",
        "title": "profile_occurrence_probe",
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
        profile_id=profile.id,
        name="SPEAKER_00",
        display_name="Occurrence Speaker",
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)
    return profile, media_file, speaker


def test_quarantined_occurrence_absent_and_clean_occurrence_still_listed(
    client, user_token_headers, normal_user, db_session
):
    """The finding, plus its control in one test: a takedown must remove only the
    quarantined occurrence, leaving a clean sibling occurrence for the same profile
    fully visible."""
    profile, _quarantined_file, _quarantined_speaker = _make_profile_with_occurrence(
        db_session, normal_user, is_quarantined=True
    )
    _profile2, _clean_file, _clean_speaker = _make_profile_with_occurrence(
        db_session,
        normal_user,
        profile=profile,
        filename="clean_occurrence_probe.wav",
        title="clean_occurrence_probe",
        uuid=str(uuid.uuid4()),
        storage_path=f"media/test/{uuid.uuid4()}.wav",
    )

    response = client.get(
        f"/api/speaker-profiles/profiles/{profile.uuid}/occurrences", headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assert len(payload) == 1
    assert "profile_occurrence_probe" not in response.text
    assert "clean_occurrence_probe" in response.text


def test_an_admin_can_still_see_their_own_quarantined_occurrence(
    client, admin_token_headers, admin_user, db_session
):
    """An admin who cannot see a quarantined file cannot review or release it —
    the same bypass every other takedown surface grants admins."""
    profile, _quarantined_file, _quarantined_speaker = _make_profile_with_occurrence(
        db_session, admin_user, is_quarantined=True
    )
    _profile2, _clean_file, _clean_speaker = _make_profile_with_occurrence(
        db_session,
        admin_user,
        profile=profile,
        filename="clean_occurrence_probe.wav",
        title="clean_occurrence_probe",
        uuid=str(uuid.uuid4()),
        storage_path=f"media/test/{uuid.uuid4()}.wav",
    )

    response = client.get(
        f"/api/speaker-profiles/profiles/{profile.uuid}/occurrences", headers=admin_token_headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assert len(payload) == 2
    assert "profile_occurrence_probe" in response.text
    assert "clean_occurrence_probe" in response.text
