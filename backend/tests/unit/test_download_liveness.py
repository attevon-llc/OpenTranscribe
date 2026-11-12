"""A URL download is judged stuck by its lease, not by how old its file is.

``identify_stuck_downloading_files`` used to treat a file as stuck once it had been
DOWNLOADING for five minutes after ``upload_time`` (when the file was CREATED) unless a Task
row of a download type existed, and nothing writes such a row. Every download still running
five minutes in was reset to QUEUED and downloaded a second time, with a second transcription
behind it. ``download.media_url`` now holds a lease (``task_liveness.download_run_id``) for as
long as it runs; only a DEAD lease is stuck.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest
from celery.exceptions import Retry

from app.core import task_liveness
from app.core.task_liveness import RunLiveness
from app.core.task_liveness import RunState
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services.task_detection_service import task_detection_service
from tests.unit._fake_liveness_redis import FakeRedis
from tests.unit._fake_liveness_redis import install_fake_redis


class _Query:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def filter(self, *_a: Any, **_k: Any) -> _Query:
        return self

    def all(self) -> list[Any]:
        return self._rows


def _db(files: list[MediaFile]) -> Any:
    """A session answering the DOWNLOADING query with ``files`` and any other with nothing."""
    from unittest.mock import create_autospec

    from sqlalchemy.orm import Session

    db = create_autospec(Session, instance=True)
    db.query.side_effect = lambda model: _Query(files if model is MediaFile else [])
    return db


def _downloading(age: timedelta) -> MediaFile:
    return MediaFile(
        id=1,
        uuid=uuid_module.uuid4(),
        status=FileStatus.DOWNLOADING,
        upload_time=datetime.now(UTC) - age,
    )


def test_only_a_download_whose_lease_is_dead_is_stuck(monkeypatch):
    files = {state: _downloading(timedelta(hours=2)) for state in RunState}
    by_run = {task_liveness.download_run_id(str(mf.uuid)): s for s, mf in files.items()}
    asked: list[str] = []

    def probe(run_ids: Any) -> dict[str, RunLiveness]:
        ids = list(run_ids)
        asked.extend(ids)
        return {run_id: RunLiveness(by_run[run_id]) for run_id in ids}

    monkeypatch.setattr(task_liveness, "probe_runs", probe)

    stuck = task_detection_service.identify_stuck_downloading_files(_db(list(files.values())))

    assert stuck == [files[RunState.DEAD]]
    assert sorted(asked) == sorted(by_run)


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    return install_fake_redis(monkeypatch)


def test_a_finished_download_releases_its_lease(fake_redis):
    run_id = task_liveness.download_run_id("f-1")

    with task_liveness.run_heartbeat(run_id, chained=False):
        during = task_liveness.probe_runs([run_id])[run_id].state

    assert during == RunState.RUNNING
    assert task_liveness.probe_runs([run_id])[run_id].state == RunState.DEAD


def test_a_download_waiting_for_its_celery_retry_reads_as_queued_not_dead(fake_redis):
    run_id = task_liveness.download_run_id("f-1")

    with pytest.raises(Retry), task_liveness.run_heartbeat(run_id, chained=False):
        raise Retry()

    assert task_liveness.probe_runs([run_id])[run_id].state == RunState.QUEUED


@pytest.fixture
def url_task_seams(monkeypatch, db_session) -> Any:
    """The URL task against the test's savepoint, with no network, jitter or pipeline."""
    import contextlib

    from app.tasks import youtube_processing
    from tests.helpers import set_youtube_policy

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(youtube_processing.settings, "YOUTUBE_PRE_DOWNLOAD_JITTER_ENABLED", False)
    set_youtube_policy(monkeypatch, auto_retry_enabled=True)
    monkeypatch.setattr(youtube_processing, "session_scope", _scope)
    monkeypatch.setattr(
        youtube_processing, "send_youtube_notification_via_redis", lambda **_k: True
    )
    monkeypatch.setattr(youtube_processing, "dispatch_transcription_pipeline", lambda **_k: None)
    monkeypatch.setattr(youtube_processing, "send_ws_event_for_file", lambda *_a, **_k: None)
    return youtube_processing


def test_the_url_task_holds_the_files_lease_while_it_downloads(
    monkeypatch, fake_redis, url_task_seams, db_session, normal_user
):
    """Against Postgres (run in the full test wave): the lease is live during the download."""
    mf = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=normal_user.id,
        filename="video.mp4",
        title="Video",
        storage_path="",
        file_size=0,
        content_type="video/mp4",
        status=FileStatus.QUEUED,
        source_url="https://youtu.be/abc123",
    )
    db_session.add(mf)
    db_session.commit()
    run_id = task_liveness.download_run_id(str(mf.uuid))
    seen: list[RunState] = []

    class _Service:
        def process_media_url_sync(self, **kwargs: Any) -> MediaFile:
            seen.append(task_liveness.probe_runs([run_id])[run_id].state)
            media_file: MediaFile = kwargs["media_file"]
            return media_file

    monkeypatch.setattr(url_task_seams, "MediaDownloadService", _Service)

    url_task_seams.process_youtube_url_task.apply(
        args=("https://youtu.be/abc123", normal_user.id, str(mf.uuid))
    ).get()

    assert seen == [RunState.RUNNING]
    assert task_liveness.probe_runs([run_id])[run_id].state == RunState.DEAD
