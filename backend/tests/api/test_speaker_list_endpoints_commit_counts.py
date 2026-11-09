"""Speaker list, picker and occurrence endpoints commit nothing, as before the query move."""

from __future__ import annotations

import uuid

import pytest

from app.models.media import MediaFile
from app.models.speaker import Speaker
from tests.commit_counter import endpoint_commits

pytestmark = pytest.mark.usefixtures("speaker_os_seams")


def _file_with_speaker(db_session, owner) -> tuple[MediaFile, Speaker]:
    mf = MediaFile(
        user_id=owner.id,
        filename="c.wav",
        storage_path=f"test/{uuid.uuid4().hex}.wav",
        file_size=1024,
        content_type="audio/wav",
        status="completed",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    spk = Speaker(user_id=owner.id, media_file_id=mf.id, name="SPEAKER_00", display_name="Ann")
    db_session.add(spk)
    db_session.commit()
    db_session.refresh(spk)
    return mf, spk


@pytest.mark.parametrize(
    "path",
    [
        "/api/speakers",
        "/api/speakers?for_filter=true",
        "/api/speaker-profiles/profiles",
        "/api/speaker-profiles/collections",
        "/api/speaker-clusters/stats",
    ],
)
def test_speaker_read_endpoints_do_not_commit(
    client, user_token_headers, normal_user, db_session, path
):
    _file_with_speaker(db_session, normal_user)
    assert endpoint_commits(client, db_session, "GET", path, headers=user_token_headers) == 0


def test_cross_media_occurrences_do_not_commit(client, user_token_headers, normal_user, db_session):
    _, spk = _file_with_speaker(db_session, normal_user)
    url = f"/api/speakers/{spk.uuid}/cross-media"
    assert endpoint_commits(client, db_session, "GET", url, headers=user_token_headers) == 0
