"""``GET /speakers`` full list: a read that writes nothing, and a bounded page (#1337 P1).

The handler used to assign the four ``computed_status`` columns on every listed ORM row, so
autoflush wrote one ``UPDATE speaker`` per speaker from a GET (about 198k per call for a
user with 597k speakers) and the request then rolled them all back.
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import pytest

from tests.query_budget import count_statements

pytestmark = pytest.mark.unit

_WRITES = r"^\s*(UPDATE|INSERT|DELETE)\b"


def _file(db, user):
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="r.mp4",
        title="r",
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
        upload_time=dt.datetime.now(dt.UTC),
    )
    db.add(media)
    db.commit()
    return media


def _speakers(db, user, media, names):
    from app.models.speaker import Speaker

    for name in names:
        db.add(
            Speaker(
                uuid=uuid_pkg.uuid4(),
                user_id=user.id,
                media_file_id=media.id,
                name=name,
                # A voiceprint-named row makes the status computation ask the privacy policy,
                # i.e. query mid-loop, which is what autoflushed the assigned columns.
                display_name=f"Person {name}",
                identity_source="voiceprint",
                verified=False,
            )
        )
    db.commit()


def test_list_issues_no_writes_and_statements_do_not_grow(
    client, db_session, normal_user, user_token_headers
):
    media = _file(db_session, normal_user)
    _speakers(db_session, normal_user, media, [f"SPEAKER_{i:02d}" for i in range(2)])
    with count_statements(db_session.connection()) as small:
        r2 = client.get("/api/speakers", headers=user_token_headers)
    _speakers(db_session, normal_user, media, [f"SPEAKER_{i:02d}" for i in range(2, 12)])
    with count_statements(db_session.connection()) as large:
        r12 = client.get("/api/speakers", headers=user_token_headers)

    assert (len(r2.json()), len(r12.json())) == (2, 12)  # control: the rows came back
    assert not large.matching(_WRITES), large.describe()
    assert not small.matching(_WRITES), small.describe()
    assert large.count == small.count, large.describe()


def test_list_is_paged_in_speaker_number_order(client, db_session, normal_user, user_token_headers):
    media = _file(db_session, normal_user)
    _speakers(db_session, normal_user, media, ["SPEAKER_10", "Zed", "SPEAKER_02", "SPEAKER_01"])
    page1 = client.get("/api/speakers", params={"limit": 2}, headers=user_token_headers).json()
    page2 = client.get(
        "/api/speakers", params={"limit": 2, "skip": 2}, headers=user_token_headers
    ).json()
    assert [s["name"] for s in page1 + page2] == ["SPEAKER_01", "SPEAKER_02", "SPEAKER_10", "Zed"]
    default = client.get("/api/speakers", headers=user_token_headers).json()
    assert len(default) == 4
