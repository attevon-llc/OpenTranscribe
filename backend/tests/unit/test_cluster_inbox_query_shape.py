"""Cluster inbox: the main SELECT joins media_file and speaker_cluster once, not twice (#1337 P8).

``joinedload`` on top of the explicit filter/sort joins read every unverified speaker through
a second, aliased copy of both joins before the top-N sort. ``contains_eager`` reuses them.
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import pytest

from tests.query_budget import count_statements

pytestmark = pytest.mark.unit


def _seed(db, user, n, offset=0):
    from app.models.media import MediaFile
    from app.models.speaker import Speaker

    for i in range(offset, offset + n):
        media = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=user.id,
            filename=f"f{i}.mp4",
            title=f"f{i}",
            storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
            content_type="video/mp4",
            file_size=1,
            status="completed",
            upload_time=dt.datetime.now(dt.UTC),
        )
        db.add(media)
        db.flush()
        db.add(
            Speaker(
                uuid=uuid_pkg.uuid4(),
                user_id=user.id,
                media_file_id=media.id,
                name="SPEAKER_00",
                verified=False,
            )
        )
    db.commit()


def test_inbox_joins_each_table_once_and_statements_stay_flat(
    client, db_session, normal_user, user_token_headers
):
    url = "/api/speaker-clusters/unverified/inbox"
    _seed(db_session, normal_user, 2)
    with count_statements(db_session.connection()) as small:
        r2 = client.get(url, headers=user_token_headers)
    _seed(db_session, normal_user, 10, offset=2)
    with count_statements(db_session.connection()) as large:
        r12 = client.get(url, headers=user_token_headers)

    assert r2.status_code == r12.status_code == 200
    assert (len(r2.json()["items"]), len(r12.json()["items"])) == (2, 12)  # control
    assert large.count == small.count, large.describe()

    mains = [s for s in large.statements if "FROM speaker" in s and "LIMIT" in s]
    assert mains, large.describe()
    for statement in mains:
        assert statement.count("JOIN speaker_cluster") == 1, statement
        assert statement.count("JOIN media_file") == 1, statement
