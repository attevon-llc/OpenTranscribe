"""Topic-suggestion and summary-delete endpoints commit where they did before the query move."""

from __future__ import annotations

import uuid

from app.models.media import MediaFile
from app.models.topic import TopicSuggestion
from tests.commit_counter import endpoint_commits


def _file(db_session, owner, *, summary: bool = False) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    mf = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="f.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        summary_data={"bluf": "x"} if summary else None,
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


def test_dismissing_suggestions_commits_once(client, user_token_headers, normal_user, db_session):
    mf = _file(db_session, normal_user)
    db_session.add(TopicSuggestion(media_file_id=mf.id, user_id=normal_user.id, status="pending"))
    db_session.commit()
    commits = endpoint_commits(
        client,
        db_session,
        "DELETE",
        f"/api/files/{mf.uuid}/suggestions",
        expect=204,
        headers=user_token_headers,
    )
    assert commits == 1


def test_reading_suggestions_does_not_commit(client, user_token_headers, normal_user, db_session):
    mf = _file(db_session, normal_user)
    db_session.add(
        TopicSuggestion(
            media_file_id=mf.id,
            user_id=normal_user.id,
            status="pending",
            suggested_tags=[],
            suggested_collections=[],
        )
    )
    db_session.commit()
    commits = endpoint_commits(
        client, db_session, "GET", f"/api/files/{mf.uuid}/suggestions", headers=user_token_headers
    )
    assert commits == 0


def test_deleting_a_summary_commits_once(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    from app.tasks import search_file_field_tasks

    monkeypatch.setattr(search_file_field_tasks.index_file_summary, "delay", lambda *a, **k: None)
    mf = _file(db_session, normal_user, summary=True)
    commits = endpoint_commits(
        client, db_session, "DELETE", f"/api/files/{mf.uuid}/summary", headers=user_token_headers
    )
    assert commits == 1
