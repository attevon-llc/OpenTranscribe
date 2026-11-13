"""Analytics are computed by the CPU-queue task, never inside a request (loadrun A5/A6).

``PUT segments/{uuid}/speaker`` recomputed analytics inline and ``GET files/{uuid}`` computed
and INSERTed a missing row. Both now dispatch ``analytics.analyze_transcript``, debounced per
file, and a GET answers ``analytics: null`` until the task has stored the row.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from tests.query_budget import count_statements

ANALYTICS_WRITE = r"^\s*(INSERT INTO|UPDATE|DELETE FROM)\s+analytics\b"

pytestmark = pytest.mark.usefixtures("speaker_os_seams")


@pytest.fixture
def dispatched(monkeypatch):
    """Record analytics task dispatches (file uuids are unique per test, so keys never collide)."""
    calls: list[dict] = []
    from app.tasks.analytics import analyze_transcript_task

    def _record(**kw) -> None:
        calls.append(kw)

    monkeypatch.setattr(analyze_transcript_task, "apply_async", _record)
    return calls


def _file(db_session, owner) -> MediaFile:
    mf = MediaFile(
        user_id=owner.id,
        filename="a.wav",
        storage_path=f"test/{uuid.uuid4().hex}.wav",
        file_size=1024,
        content_type="audio/wav",
        status="completed",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


def test_speaker_reassign_dispatches_instead_of_writing_analytics(
    client, user_token_headers, normal_user, db_session, dispatched
):
    mf = _file(db_session, normal_user)
    spk = Speaker(user_id=normal_user.id, media_file_id=mf.id, name="SPEAKER_00")
    db_session.add(spk)
    seg = TranscriptSegment(media_file_id=mf.id, start_time=0.0, end_time=1.0, text="hi")
    db_session.add(seg)
    db_session.commit()

    with count_statements(db_session.connection()) as log:
        resp = client.put(
            f"/api/transcripts/segments/{seg.uuid}/speaker",
            headers=user_token_headers,
            json={"speaker_uuid": str(spk.uuid)},
        )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assert not log.matching(ANALYTICS_WRITE), log.describe()
    assert [c["kwargs"]["file_uuid"] for c in dispatched] == [str(mf.uuid)]


def test_file_detail_get_does_not_write_analytics(
    client, user_token_headers, normal_user, db_session, dispatched
):
    mf = _file(db_session, normal_user)
    db_session.add(TranscriptSegment(media_file_id=mf.id, start_time=0, end_time=1, text="a b"))
    db_session.commit()

    with count_statements(db_session.connection()) as log:
        resp = client.get(f"/api/files/{mf.uuid}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assert resp.json().get("analytics") is None
    assert not log.matching(ANALYTICS_WRITE), log.describe()
    assert [c["kwargs"]["file_uuid"] for c in dispatched] == [str(mf.uuid)]
