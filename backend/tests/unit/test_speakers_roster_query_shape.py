"""``GET /speakers?for_filter=true`` roster: no ``NOT IN (SELECT ...)`` and a flat statement count (#1337 P2)."""

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
                display_name=f"Person {i}",
                verified=True,
            )
        )
    db.commit()


def test_roster_uses_anti_join_and_flat_statement_count(
    client, db_session, normal_user, user_token_headers
):
    _seed(db_session, normal_user, 2)
    with count_statements(db_session.connection()) as small:
        r2 = client.get("/api/speakers", params={"for_filter": True}, headers=user_token_headers)
    _seed(db_session, normal_user, 10, offset=2)
    with count_statements(db_session.connection()) as large:
        r12 = client.get("/api/speakers", params={"for_filter": True}, headers=user_token_headers)

    assert (len(r2.json()), len(r12.json())) == (2, 12)  # control: the rows came back
    assert large.count == small.count, large.describe()
    assert not large.matching(r"NOT IN \(SELECT"), large.describe()
