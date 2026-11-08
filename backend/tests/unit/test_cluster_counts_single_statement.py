"""Cluster list and stats: header counts come from one statement each (#1337 P9).

The list ran three separate scans of the tenant's clusters (labeled, unlabeled, newest update)
and the stats ran two counts over the user's speakers; each is now one ``FILTER`` aggregate.
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import pytest

from tests.query_budget import count_statements

pytestmark = pytest.mark.unit


def _seed(db, user):
    from app.models.media import MediaFile
    from app.models.speaker import Speaker
    from app.models.speaker import SpeakerCluster

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="f.mp4",
        title="f",
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1,
        status="completed",
        upload_time=dt.datetime.now(dt.UTC),
    )
    db.add(media)
    db.flush()
    clusters = [
        SpeakerCluster(user_id=user.id, label="Named", member_count=2),
        SpeakerCluster(user_id=user.id, label=None, member_count=2),
        SpeakerCluster(user_id=user.id, label=None, member_count=3),
    ]
    db.add_all(clusters)
    db.flush()
    for i, cluster in enumerate((*clusters, None)):
        db.add(
            Speaker(
                uuid=uuid_pkg.uuid4(),
                user_id=user.id,
                media_file_id=media.id,
                name=f"SPEAKER_{i:02d}",
                cluster_id=cluster.id if cluster else None,
            )
        )
    db.commit()


def test_list_header_counts_are_one_statement(client, db_session, normal_user, user_token_headers):
    _seed(db_session, normal_user)
    with count_statements(db_session.connection()) as log:
        response = client.get("/api/speaker-clusters", headers=user_token_headers)
    body = response.json()
    assert (body["labeled_count"], body["unlabeled_count"]) == (1, 2)  # control
    assert body["last_clustered_at"] is not None
    aggregates = log.matching(r"max\(speaker_cluster\.updated_at\)")
    assert len(aggregates) == 1, log.describe()
    assert "FILTER (WHERE" in aggregates[0], aggregates[0]


def test_stats_speaker_counts_are_one_statement(
    client, db_session, normal_user, user_token_headers
):
    _seed(db_session, normal_user)
    with count_statements(db_session.connection()) as log:
        response = client.get("/api/speaker-clusters/stats", headers=user_token_headers)
    assert response.status_code == 200
    body = response.json()
    assert (body["total_speakers"], body["clustered_speakers"]) == (4, 3)  # control
    speaker_counts = log.matching(r"count\(speaker\.id\)|FROM speaker\b(?!_)")
    assert len(speaker_counts) == 1, log.describe()
