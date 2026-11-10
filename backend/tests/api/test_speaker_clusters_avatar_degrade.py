"""One unsignable avatar must not fail the whole speaker-clusters list.

``get_file_url`` raises ``StorageError`` / ``StorageUnavailableError`` (never the client's own
``S3Error``), so the per-avatar handler has to catch those: the cluster is still listed, with no
avatar URL.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.core.exceptions import StorageError
from app.core.exceptions import StorageUnavailableError
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerProfile

PREFIX = "/api/speaker-clusters"

pytestmark = pytest.mark.usefixtures("speaker_os_seams")


@pytest.mark.parametrize("failure", [StorageUnavailableError, StorageError])
def test_list_omits_the_avatar_url_when_presigning_fails(
    client, user_token_headers, normal_user, db_session, monkeypatch, failure
):
    profile = SpeakerProfile(
        uuid=uuid.uuid4(), user_id=normal_user.id, name="Dana", avatar_path="avatars/dana.png"
    )
    db_session.add(profile)
    db_session.flush()
    cluster = SpeakerCluster(
        user_id=normal_user.id, label="Dana", member_count=0, promoted_to_profile_id=profile.id
    )
    db_session.add(cluster)
    db_session.commit()

    def _unsignable(*args, **kwargs):
        raise failure("Storage unavailable while presigning a URL: ConnectionError")

    monkeypatch.setattr("app.services.minio_service.get_file_url", _unsignable)

    resp = client.get(PREFIX, headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    item = next(i for i in resp.json()["items"] if i["uuid"] == str(cluster.uuid))
    assert item["promoted_to_profile_name"] == "Dana"
    assert item["promoted_to_profile_avatar_url"] is None
