"""Speaker profile, collection and cluster endpoints commit where they did before."""

from __future__ import annotations

import pytest

from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerProfile
from tests.commit_counter import endpoint_commits

pytestmark = pytest.mark.usefixtures("speaker_os_seams")


def test_profile_create_update_delete_commit_counts(
    client, user_token_headers, normal_user, db_session
):
    headers = user_token_headers
    commits = endpoint_commits(
        client,
        db_session,
        "POST",
        "/api/speaker-profiles/profiles",
        params={"name": "Casey"},
        headers=headers,
    )
    assert commits == 1
    prof = db_session.query(SpeakerProfile).filter_by(user_id=normal_user.id, name="Casey").one()
    url = f"/api/speaker-profiles/profiles/{prof.uuid}"
    commits = endpoint_commits(
        client, db_session, "PUT", url, params={"name": "Casey 2"}, headers=headers
    )
    assert commits == 1
    commits = endpoint_commits(client, db_session, "DELETE", url, expect=204, headers=headers)
    # The delete's own commit, plus the one speaker cleanup now issues to end the read
    # transaction before it erases the profile's voiceprint document (no transaction across
    # OpenSearch I/O).
    assert commits == 2


def test_collection_create_commits_once(client, user_token_headers, db_session):
    commits = endpoint_commits(
        client,
        db_session,
        "POST",
        "/api/speaker-profiles/collections",
        params={"name": "Team"},
        headers=user_token_headers,
    )
    assert commits == 1


def test_cluster_update_commits_once_and_delete_twice(
    client, user_token_headers, normal_user, db_session
):
    cluster = SpeakerCluster(user_id=normal_user.id, label="old")
    db_session.add(cluster)
    db_session.commit()
    db_session.refresh(cluster)
    url = f"/api/speaker-clusters/{cluster.uuid}"
    commits = endpoint_commits(
        client, db_session, "PUT", url, json={"label": "new"}, headers=user_token_headers
    )
    assert commits == 1
    commits = endpoint_commits(
        client, db_session, "DELETE", url, expect=204, headers=user_token_headers
    )
    assert commits == 2
