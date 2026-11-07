"""Summary and topic-extraction failures reach the user as fixed sentences.

The failure notification is pushed over the WebSocket to the browser, so it may carry only
text we wrote. A database error is recorded by its class name: its text quotes the SQL
statement and the bound parameters. Topic extraction also retries a database error, which
is usually transient, instead of failing the file on the first dropped connection.
"""

from __future__ import annotations

import uuid as uuid_mod

import pytest
from celery.exceptions import Retry
from sqlalchemy.exc import OperationalError

from app.models.media import Task as TaskModel
from app.utils.task_utils import create_task_record
from tests.unit._session_lifetime_trackers import ScopeTracker
from tests.unit._session_lifetime_trackers import make_transcribed_file

LEAK_MARKER = "SELECT secret_column FROM transcript_segment"


def _db_error() -> OperationalError:
    return OperationalError(LEAK_MARKER, {"text": "private words"}, Exception("reset"))


def _task_error_message(db_session, task_id: str) -> str | None:
    db_session.expire_all()
    row = db_session.query(TaskModel).filter(TaskModel.id == task_id).one()
    return None if row.error_message is None else str(row.error_message)


@pytest.fixture
def summary_failure(db_session, normal_user, monkeypatch):
    from app.tasks import summarization as summ

    tracker = ScopeTracker(db_session)
    sent: list[tuple] = []
    monkeypatch.setattr(summ, "session_scope", tracker.scope)
    monkeypatch.setattr(summ, "send_summary_notification", lambda *a, **kw: sent.append(a))
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)
    task_id = str(uuid_mod.uuid4())
    create_task_record(db_session, task_id, normal_user.id, media_file.id, "summarization")
    db_session.commit()
    return summ, media_file, task_id, sent


@pytest.mark.parametrize(
    "error",
    [_db_error(), RuntimeError(f"provider said: {LEAK_MARKER}")],
    ids=["database", "other"],
)
def test_summary_failure_notification_is_a_fixed_sentence(db_session, summary_failure, error):
    summ, media_file, task_id, sent = summary_failure

    result = summ._handle_task_error(error, str(media_file.uuid), media_file.id, task_id)

    assert len(sent) == 1
    message = sent[0][3]
    assert message.startswith("AI summary generation failed: ")
    assert LEAK_MARKER not in message
    assert result["status"] == "error"


def test_summary_database_failure_is_recorded_by_class_name(db_session, summary_failure):
    summ, media_file, task_id, _sent = summary_failure

    result = summ._handle_task_error(_db_error(), str(media_file.uuid), media_file.id, task_id)

    assert result["message"] == "OperationalError"
    assert _task_error_message(db_session, task_id) == "OperationalError"


@pytest.fixture
def topic_task(db_session, monkeypatch):
    from app.tasks import topic_extraction as tex

    tracker = ScopeTracker(db_session)
    sent: list[dict] = []
    monkeypatch.setattr(tex, "session_scope", tracker.scope)
    monkeypatch.setattr(tex, "send_topic_extraction_notification", lambda **kw: sent.append(kw))
    return tex, sent


def test_topic_failure_notification_is_a_fixed_sentence(db_session, normal_user, topic_task):
    tex, sent = topic_task
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)

    result = tex._handle_task_error(RuntimeError(LEAK_MARKER), str(media_file.uuid))

    assert [kw["message"] for kw in sent] == ["Failed to extract topics from transcript"]
    assert result["status"] == "failed"


def _unreadable_settings(monkeypatch, tex) -> None:
    def _raise(user_id, db=None):
        raise _db_error()

    monkeypatch.setattr(tex.TopicExtractionService, "create_from_settings", _raise)


def test_topic_extraction_retries_a_database_error(
    db_session, normal_user, topic_task, monkeypatch
):
    tex, sent = topic_task
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)
    _unreadable_settings(monkeypatch, tex)
    retried: list[tuple[type, int]] = []

    def _record_retry(*, exc, countdown):
        retried.append((type(exc), countdown))
        return Retry()

    monkeypatch.setattr(tex.extract_topics_task, "retry", _record_retry)

    outcome = tex.extract_topics_task.apply(args=[str(media_file.uuid)])

    assert retried == [(OperationalError, 30)]
    assert outcome.state == "RETRY"
    assert sent == []
    assert tex.extract_topics_task.max_retries == 2


def test_topic_extraction_fails_once_database_retries_are_spent(
    db_session, normal_user, topic_task, monkeypatch
):
    tex, sent = topic_task
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)
    _unreadable_settings(monkeypatch, tex)
    retried: list[dict] = []

    def _record_retry(**kw):
        retried.append(kw)
        return Retry()

    monkeypatch.setattr(tex.extract_topics_task, "retry", _record_retry)

    result = tex.extract_topics_task.apply(args=[str(media_file.uuid)], retries=2).get()

    assert retried == []
    assert result == {"status": "failed", "error": "topic_extraction_failed"}
    assert [kw["message"] for kw in sent] == ["Failed to extract topics from transcript"]
