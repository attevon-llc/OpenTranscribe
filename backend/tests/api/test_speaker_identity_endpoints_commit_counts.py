"""Speaker identity endpoints commit exactly where they did before their queries moved."""

from __future__ import annotations

import uuid

import pytest

from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from tests.commit_counter import endpoint_commits

pytestmark = pytest.mark.usefixtures("speaker_os_seams")


def _profile(db_session, owner) -> SpeakerProfile:
    prof = SpeakerProfile(user_id=owner.id, name=f"Profile {uuid.uuid4().hex[:6]}")
    db_session.add(prof)
    db_session.commit()
    db_session.refresh(prof)
    return prof


def _speaker(db_session, owner, **kw) -> Speaker:
    mf = MediaFile(
        user_id=owner.id,
        filename="s.wav",
        storage_path=f"test/{uuid.uuid4().hex}.wav",
        file_size=1024,
        content_type="audio/wav",
        status="completed",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    spk = Speaker(user_id=owner.id, media_file_id=mf.id, name="SPEAKER_00", **kw)
    db_session.add(spk)
    db_session.commit()
    db_session.refresh(spk)
    return spk


def test_profile_confirm_gender_commits_once(client, user_token_headers, normal_user, db_session):
    prof = _profile(db_session, normal_user)
    commits = endpoint_commits(
        client,
        db_session,
        "POST",
        f"/api/speaker-profiles/profiles/{prof.uuid}/confirm-gender",
        params={"gender": "female"},
        headers=user_token_headers,
    )
    assert commits == 1


def _verify(client, db_session, headers, speaker, **params) -> int:
    return endpoint_commits(
        client,
        db_session,
        "POST",
        f"/api/speakers/{speaker.uuid}/verify",
        params=params,
        headers=headers,
    )


def test_rejecting_a_suggestion_commits_twice(client, user_token_headers, normal_user, db_session):
    speaker = _speaker(db_session, normal_user, suggested_name="Ann", confidence=0.6)
    assert _verify(client, db_session, user_token_headers, speaker, action="reject") == 2


def test_creating_a_profile_from_a_speaker_commits_four_times(
    client, user_token_headers, normal_user, db_session
):
    speaker = _speaker(db_session, normal_user)
    commits = _verify(
        client,
        db_session,
        user_token_headers,
        speaker,
        action="create_profile",
        profile_name="New Person",
    )
    # Two of its own, plus one each from the profile-embedding and cluster-centroid rebuilds,
    # which now end their read transaction before the OpenSearch phase.
    assert commits == 4
