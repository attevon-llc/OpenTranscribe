"""One user's bulk NLP work must not block another user's (concurrency audit M11).

Every summary went in at ``NLPPriority.USER_TRIGGERED`` (the task's decorator default)
whether one person asked for one or a bulk action asked for fifty, and the Redis transport
is FIFO within a priority. Dispatch here runs for real through the bulk-action handler and
the single-file endpoint helper; only the broker publish is captured. The queue order is
derived the way the transport serves it: lowest priority number first, FIFO within one
(Celery, "Routing Tasks: Redis Message Priorities"; ``core/celery.py``).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.endpoints.files import bulk_action_handlers as handlers
from app.core.constants import TRANSCRIPTION_FAIR_SHARE_THRESHOLD_KEY
from app.core.queues import DownloadPriority
from app.core.queues import NLPPriority
from app.models.media import MediaFile
from app.models.media import Task
from app.services.system_settings_service import set_setting
from tests.fixtures.ws_handshake import make_user


@pytest.fixture
def published(monkeypatch) -> list[dict[str, Any]]:
    """Every summary publish, in order, with the priority it was given."""
    from app.tasks.summarization import summarize_transcript_task

    sent: list[dict[str, Any]] = []
    default = summarize_transcript_task.priority  # what a bare .delay() is published at

    def _apply_async(kwargs=None, priority=None, **_options):
        sent.append({"file_uuid": kwargs["file_uuid"], "priority": priority})
        return SimpleNamespace(id=f"task-{len(sent)}")

    def _delay(**kwargs):
        return _apply_async(kwargs=kwargs, priority=default)

    monkeypatch.setattr(summarize_transcript_task, "apply_async", _apply_async)
    monkeypatch.setattr(summarize_transcript_task, "delay", _delay)
    monkeypatch.setattr(handlers, "env_bool", lambda name, default=False: False)
    monkeypatch.setattr(handlers, "_llm_unavailable", lambda file_uuid, user_id: None)
    return sent


def _completed_files(db, user_id: int, count: int) -> list[MediaFile]:
    files = []
    for _ in range(count):
        file_uuid = str(uuid.uuid4())
        mf = MediaFile(
            uuid=file_uuid,
            filename="fair.wav",
            storage_path=f"media/test/{file_uuid}.wav",
            content_type="audio/wav",
            file_size=1,
            status="completed",
            user_id=user_id,
        )
        db.add(mf)
        files.append(mf)
    db.commit()
    return files


def _service_order(sent: list[dict[str, Any]]) -> list[str]:
    indexed = sorted(enumerate(sent), key=lambda pair: (pair[1]["priority"], pair[0]))
    return [entry["file_uuid"] for _, entry in indexed]


def test_one_users_fifty_bulk_summaries_do_not_block_anothers(
    client, db_session, published, normal_user, user_token_headers, admin_user, admin_token_headers
):
    bulk_files = _completed_files(db_session, normal_user.id, 50)
    (single,) = _completed_files(db_session, admin_user.id, 1)

    # The bulk action is dispatched first, so FIFO alone would serve it first.
    bulk = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={"file_uuids": [str(mf.uuid) for mf in bulk_files], "action": "summarize"},
    )
    one = client.post(
        "/api/files/management/bulk-action",
        headers=admin_token_headers,
        json={"file_uuids": [str(single.uuid)], "action": "summarize"},
    )

    assert bulk.status_code == one.status_code == 200, (bulk.text, one.text)
    assert all(r["success"] for r in bulk.json() + one.json())
    order = _service_order(published)
    assert len(order) == 51
    assert order[0] == str(single.uuid), "the other user's summary waited behind the batch"


def _set_threshold(db, value: int) -> None:
    set_setting(db, TRANSCRIPTION_FAIR_SHARE_THRESHOLD_KEY, value, "fair share (test)")


def _active_transcriptions(db, user_id: int, files: list[MediaFile]) -> None:
    for mf in files:
        db.add(
            Task(
                id=str(uuid.uuid4()),
                user_id=user_id,
                media_file_id=mf.id,
                task_type="transcription",
                status="pending",
            )
        )
    db.commit()


def test_an_owner_past_their_fair_share_has_nlp_work_demoted(db_session):
    from app.services import dispatch_priority

    _set_threshold(db_session, 2)
    busy, idle = make_user(db_session), make_user(db_session)
    _active_transcriptions(db_session, busy.id, _completed_files(db_session, busy.id, 2))

    auto = NLPPriority.AUTO_PIPELINE
    assert dispatch_priority.nlp_priority(db_session, idle.id, base=auto) == auto
    assert dispatch_priority.nlp_priority(db_session, busy.id, base=auto) == auto + 1
    assert (
        dispatch_priority.nlp_priority(db_session, idle.id, base=auto, bulk=True)
        == NLPPriority.ADMIN_BATCH
    )


def test_a_user_with_many_url_imports_in_flight_has_downloads_demoted(db_session):
    from app.services import dispatch_priority

    _set_threshold(db_session, 2)
    flooder, other = make_user(db_session), make_user(db_session)
    for _ in range(3):
        file_uuid = str(uuid.uuid4())
        db_session.add(
            MediaFile(
                uuid=file_uuid,
                filename="url.mp4",
                storage_path="",
                content_type="video/mp4",
                file_size=0,
                status="processing",
                user_id=flooder.id,
                source_url=f"https://example.com/{file_uuid}",
            )
        )
    db_session.commit()

    assert dispatch_priority.download_priority(db_session, flooder.id) == DownloadPriority.PLAYLIST
    assert dispatch_priority.download_priority(db_session, other.id) == DownloadPriority.SINGLE_URL
