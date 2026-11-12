"""A playlist download claimed but never sent is found and sent again, once (issue #1159).

``media_playlist_processing`` commits each download's claim before it sends the message, so a
worker lost between the two leaves a QUEUED placeholder with no download. The health check
finds the claim once it is well past due, takes it over (only the claim it found, only while
the file is still QUEUED) and sends again under the SAME Celery task id. If the first message
was only late, the download task starts the first delivery under that id and turns the other
away. A delivery lost after it started the claim, before the file reached DOWNLOADING, is
found once its download lease reads DEAD and is sent again under a NEW id. The unit tests pin
each piece against fakes; the ``db_session`` tests prove the SQL against Postgres and run in
the full test wave.
"""

from __future__ import annotations

import time
import uuid as uuid_module
from collections.abc import Collection
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest
import redis
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services.media_download_playlist import DOWNLOAD_DISPATCHED_KEY
from app.services.media_download_playlist import DOWNLOAD_DUE_AT_KEY
from app.services.media_download_playlist import DOWNLOAD_OPTIONS_KEY
from app.services.media_download_playlist import DOWNLOAD_STARTED_KEY
from app.services.media_download_playlist import DOWNLOAD_TASK_ID_KEY
from app.tasks import media_playlist_processing as mpp
from app.tasks import youtube_processing


def _session(statements: list[Any], *, returns: int | None) -> Any:
    db = create_autospec(Session, instance=True)

    def execute(statement: Any) -> SimpleNamespace:
        statements.append(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: returns)

    db.execute.side_effect = execute
    return db


def _compiled(statement: Any) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


def _stamp(params: dict[str, Any]) -> dict[str, Any]:
    (stamp,) = [v for v in params.values() if isinstance(v, dict) and v]
    return stamp


@pytest.fixture
def sent(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(mpp.process_youtube_url_task, "apply_async", lambda **kw: calls.append(kw))
    return calls


def test_the_first_send_carries_the_task_id_its_claim_recorded(sent):
    statements: list[Any] = []
    db = _session(statements, returns=7)
    mf = MediaFile(id=7, source_url="https://x", title="t", metadata_raw={})

    before = int(time.time())
    assert mpp._dispatch_playlist_video_task(mf, 1, db, countdown=40, video_quality="720p") is True

    sql, params = _compiled(statements[0])
    stamp = _stamp(params)
    assert " ? " in sql, "the first claim is conditional on there being no claim yet"
    assert stamp[DOWNLOAD_DISPATCHED_KEY] is True
    assert stamp[DOWNLOAD_OPTIONS_KEY] == sent[0]["kwargs"]
    assert stamp[DOWNLOAD_OPTIONS_KEY]["video_quality"] == "720p"
    assert before + 40 <= stamp[DOWNLOAD_DUE_AT_KEY] <= int(time.time()) + 40
    (call,) = sent
    assert call["task_id"] == stamp[DOWNLOAD_TASK_ID_KEY]
    assert call["countdown"] == 40


def test_a_resend_takes_over_only_the_claim_it_found_and_reuses_its_task_id(sent):
    statements: list[Any] = []
    db = _session(statements, returns=7)
    options = {"collection_ids": None, "tag_names": ["t"], "video_quality": "720p"}
    meta = {
        DOWNLOAD_DISPATCHED_KEY: True,
        DOWNLOAD_TASK_ID_KEY: "t-1",
        DOWNLOAD_DUE_AT_KEY: 100,
        DOWNLOAD_OPTIONS_KEY: options,
    }
    mf = MediaFile(id=7, uuid=uuid_module.uuid4(), user_id=3, source_url="https://x")
    mf.metadata_raw = meta

    assert mpp.resend_stranded_playlist_download(db, mf) is True

    sql, params = _compiled(statements[0])
    assert "media_file.status = " in sql
    assert "->> " in sql and "AS BIGINT) = " in sql
    assert 100 in params.values() and FileStatus.QUEUED in params.values()
    assert _stamp(params)[DOWNLOAD_TASK_ID_KEY] == "t-1"
    assert _stamp(params)[DOWNLOAD_OPTIONS_KEY] == options, "the claim keeps the import's options"
    assert sent == [{"args": ["https://x", 3, str(mf.uuid)], "kwargs": options, "task_id": "t-1"}]


def test_a_resend_whose_claim_was_taken_sends_nothing(sent):
    db = _session([], returns=None)
    mf = MediaFile(id=7, uuid=uuid_module.uuid4(), user_id=3, source_url="https://x")
    mf.metadata_raw = {DOWNLOAD_TASK_ID_KEY: "t-1", DOWNLOAD_DUE_AT_KEY: 100}

    assert mpp.resend_stranded_playlist_download(db, mf) is False
    assert sent == []


def test_the_recovery_counts_each_resend_and_isolates_a_failure(monkeypatch):
    outcomes: list[bool | Exception] = [True, OSError("broker down"), False]

    def resend(_db: Any, _mf: Any) -> bool:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(mpp, "resend_stranded_playlist_download", resend)
    db = create_autospec(Session, instance=True)
    files = [MediaFile(id=i) for i in (1, 2, 3)]

    assert mpp.resend_stranded_playlist_downloads(db, files) == 1
    assert outcomes == []
    db.rollback.assert_called_once_with()


def test_a_lost_started_download_is_resent_under_a_new_id_without_its_start_mark(sent):
    statements: list[Any] = []
    db = _session(statements, returns=7)
    options = {"collection_ids": None, "tag_names": None, "video_quality": "480p"}
    mf = MediaFile(id=7, uuid=uuid_module.uuid4(), user_id=3, source_url="https://x")
    mf.metadata_raw = {
        DOWNLOAD_TASK_ID_KEY: "t-1",
        DOWNLOAD_DUE_AT_KEY: 100,
        DOWNLOAD_STARTED_KEY: 150,
        DOWNLOAD_OPTIONS_KEY: options,
    }

    assert mpp.resend_stranded_playlist_download(db, mf) is True

    sql, params = _compiled(statements[0])
    assert 100 in params.values() and FileStatus.QUEUED in params.values()
    assert " - CAST(" in sql and DOWNLOAD_STARTED_KEY in params.values(), "the start mark goes"
    stamp = _stamp(params)
    assert stamp[DOWNLOAD_TASK_ID_KEY] != "t-1", "the started id would be turned away"
    assert stamp[DOWNLOAD_OPTIONS_KEY] == options
    (call,) = sent
    assert call["task_id"] == stamp[DOWNLOAD_TASK_ID_KEY]
    assert call["kwargs"] == options


class _Leases:
    """``get_request_redis`` stand-in: the download leases that are held, or an outage."""

    def __init__(self, running: Collection[str] = (), queued: Collection[str] = ()):
        self.running, self.queued, self.down = running, queued, False

    def mget(self, keys: list[str]) -> list[str | None]:
        if self.down:
            raise redis.ConnectionError("redis unreachable")
        return [
            '{"started": 1, "owner": "w"}'
            if key.startswith("transcription_heartbeat:") and key.split(":", 1)[1] in self.running
            else "1"
            if key.startswith("transcription_queued:") and key.split(":", 1)[1] in self.queued
            else None
            for key in keys
        ]


def _found_by_the_finder(monkeypatch, claims: list[MediaFile], leases: _Leases) -> list[MediaFile]:
    from app.core import redis as core_redis
    from app.services.task_detection_service import task_detection_service

    monkeypatch.setattr(core_redis, "get_request_redis", lambda: leases)
    db = create_autospec(Session, instance=True)
    db.query.return_value.filter.return_value.all.return_value = claims
    return task_detection_service.identify_stranded_playlist_dispatches(db)


def _claim(*, started: bool) -> MediaFile:
    meta: dict[str, Any] = {DOWNLOAD_TASK_ID_KEY: "t", DOWNLOAD_DUE_AT_KEY: 1}
    if started:
        meta[DOWNLOAD_STARTED_KEY] = 1
    return MediaFile(id=1, uuid=uuid_module.uuid4(), metadata_raw=meta)


def test_a_started_claim_is_stranded_only_when_its_download_lease_reads_dead(monkeypatch):
    from app.core.task_liveness import download_run_id

    unstarted, lost, running, retrying = (_claim(started=s) for s in (False, True, True, True))
    leases = _Leases(
        running={download_run_id(str(running.uuid))},
        queued={download_run_id(str(retrying.uuid))},
    )

    found = _found_by_the_finder(monkeypatch, [unstarted, lost, running, retrying], leases)

    assert found == [unstarted, lost]


def test_an_unreadable_redis_leaves_a_started_claim_alone(monkeypatch):
    unstarted, lost = _claim(started=False), _claim(started=True)
    leases = _Leases()
    leases.down = True

    assert _found_by_the_finder(monkeypatch, [unstarted, lost], leases) == [unstarted]


def _request(task_id: str | None, retries: int = 0) -> SimpleNamespace:
    return SimpleNamespace(id=task_id, retries=retries)


def test_a_delivery_that_is_not_a_claimed_playlist_download_is_never_turned_away():
    statements: list[Any] = []
    db = _session(statements, returns=None)
    mf = MediaFile(id=7, metadata_raw={DOWNLOAD_TASK_ID_KEY: "t-1"})

    assert youtube_processing._starts_claimed_download(db, mf, _request("other-id")) is True
    assert youtube_processing._starts_claimed_download(db, mf, _request("t-1", retries=1)) is True
    assert statements == [], "a single-URL import or a Celery retry wrote nothing"


def test_only_the_first_delivery_of_a_claimed_download_starts(monkeypatch):
    mf = MediaFile(id=7, metadata_raw={DOWNLOAD_TASK_ID_KEY: "t-1"})
    statements: list[Any] = []

    assert youtube_processing._starts_claimed_download(
        _session(statements, returns=7), mf, _request("t-1")
    )
    assert not youtube_processing._starts_claimed_download(
        _session(statements, returns=None), mf, _request("t-1")
    )
    sql, params = _compiled(statements[0])
    assert " ? " in sql and "t-1" in params.values()
    (stamp,) = _stamp(params).items()
    assert stamp[0] == DOWNLOAD_STARTED_KEY
    assert abs(stamp[1] - time.time()) < 60, "the start records when it happened"


# --- against Postgres (run in the full test wave) ------------------------------------------


def _placeholder(
    db_session: Any, user: Any, *, status: FileStatus = FileStatus.QUEUED
) -> MediaFile:
    mf = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=user.id,
        filename="video.mp4",
        title="t",
        storage_path="",
        file_size=0,
        content_type="video/mp4",
        status=status,
        source_url="https://youtu.be/abc123",
        metadata_raw={"from_playlist": True},
    )
    db_session.add(mf)
    db_session.commit()
    return mf


def _age_claim(db_session: Any, mf: MediaFile, seconds_past_due: int) -> None:
    db_session.refresh(mf)
    mf.metadata_raw = {
        **(mf.metadata_raw or {}),
        DOWNLOAD_DUE_AT_KEY: int(time.time()) - seconds_past_due,
    }
    db_session.commit()


def test_a_stranded_claim_is_recovered_exactly_once(db_session, normal_user, sent):
    from app.services.task_detection_service import task_detection_service

    grace = task_detection_service.config.PLAYLIST_DISPATCH_GRACE_SECONDS
    stranded = _placeholder(db_session, normal_user)
    fresh = _placeholder(db_session, normal_user)
    started = _placeholder(db_session, normal_user)
    downloading = _placeholder(db_session, normal_user)
    for mf in (stranded, fresh, started, downloading):
        assert mpp._dispatch_playlist_video_task(mf, normal_user.id, db_session)
    claimed_ids = {call["args"][2]: call["task_id"] for call in sent}
    sent.clear()
    for mf in (stranded, started, downloading):
        _age_claim(db_session, mf, grace + 60)
    started.metadata_raw = {**(started.metadata_raw or {}), DOWNLOAD_STARTED_KEY: int(time.time())}
    downloading.status = FileStatus.DOWNLOADING
    db_session.commit()

    ours = {mf.id for mf in (stranded, fresh, started, downloading)}

    for _sweep in range(2):
        found = task_detection_service.identify_stranded_playlist_dispatches(db_session)
        mine = [mf for mf in found if mf.id in ours]  # the dev database holds other rows
        mpp.resend_stranded_playlist_downloads(db_session, mine)

    assert [call["args"][2] for call in sent] == [str(stranded.uuid)]
    assert sent[0]["task_id"] == claimed_ids[str(stranded.uuid)]


def test_two_deliveries_of_one_claimed_download_start_once(db_session, normal_user, sent):
    mf = _placeholder(db_session, normal_user)
    assert mpp._dispatch_playlist_video_task(mf, normal_user.id, db_session)
    db_session.refresh(mf)
    request = _request(sent[0]["task_id"])

    first = youtube_processing._starts_claimed_download(db_session, mf, request)
    db_session.refresh(mf)
    second = youtube_processing._starts_claimed_download(db_session, mf, request)

    assert (first, second) == (True, False)


def test_a_started_claim_lost_before_downloading_is_resent_once_under_a_new_id(
    db_session, normal_user, sent, monkeypatch
):
    from app.core import redis as core_redis
    from app.core.task_liveness import download_run_id
    from app.services.task_detection_service import task_detection_service

    grace = task_detection_service.config.PLAYLIST_DISPATCH_GRACE_SECONDS
    lost, live, just_started = (_placeholder(db_session, normal_user) for _ in range(3))
    for mf in (lost, live, just_started):
        assert mpp._dispatch_playlist_video_task(mf, normal_user.id, db_session)
    claimed_ids = {call["args"][2]: call["task_id"] for call in sent}
    sent.clear()
    for mf, started_ago in ((lost, grace + 60), (live, grace + 60), (just_started, 5)):
        _age_claim(db_session, mf, grace + 120)
        mf.metadata_raw = {
            **(mf.metadata_raw or {}),
            DOWNLOAD_STARTED_KEY: int(time.time()) - started_ago,
        }
    db_session.commit()
    leases = _Leases(running={download_run_id(str(live.uuid))})
    monkeypatch.setattr(core_redis, "get_request_redis", lambda: leases)

    ours = {mf.id for mf in (lost, live, just_started)}
    for _sweep in range(2):
        found = task_detection_service.identify_stranded_playlist_dispatches(db_session)
        mpp.resend_stranded_playlist_downloads(db_session, [mf for mf in found if mf.id in ours])

    (call,) = sent
    assert call["args"][2] == str(lost.uuid)
    assert call["task_id"] != claimed_ids[str(lost.uuid)]
    db_session.refresh(lost)
    assert youtube_processing._starts_claimed_download(db_session, lost, _request(call["task_id"]))
