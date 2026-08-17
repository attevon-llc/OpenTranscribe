"""``app/tasks/summary_retry.py`` -- the failed-AI-summary retry helpers.

``reset_summary_for_retry`` / ``retry_summary_if_available`` are called from
``POST /files/{file_uuid}/retry-summary`` (``api/endpoints/files/summary_status.py``).

**Real bug found and fixed here**: both functions were written to expect
``get_file_by_uuid`` to return a falsy value for "not found" --

    media_file = get_file_by_uuid(db, file_uuid)
    if not media_file:
        return False

-- but ``get_file_by_uuid`` (``app/utils/uuid_helpers.py``) raises
``fastapi.HTTPException(404)`` on a missing row and ``HTTPException(400)`` on a
malformed UUID string; it never returns a falsy value. The ``if not media_file``
branch was dead code, and a missing/malformed ``file_uuid`` raised an uncaught
``HTTPException`` out of a plain (non-endpoint) helper instead of returning
``False`` as the surrounding code and this module's docstring assume. Fixed by
switching to ``get_by_uuid_optional``, which returns ``None`` for both cases and
restores the originally-intended contract. ``TestNotFoundAndMalformedInput``
below is red against the pre-fix code (raises instead of returning ``False``).

A second bug was found and fixed in ``retry_summary_if_available``: it reset
the summary (committed) before dispatching ``summarize_transcript_task``, so a
dispatch failure (e.g. a broker outage) left the previous summary destroyed
with nothing queued to regenerate it, while returning ``False`` -- which the
caller reports as "nothing happened". Fixed by capturing the pre-reset values
and restoring them on a failed dispatch, rather than reordering to
dispatch-before-reset (rejected: a fast worker could complete the task and
write the new summary before this function's own commit ran, and the reset
would then clobber that result). See
``TestRetrySummaryIfAvailable::test_a_dispatch_failure_restores_the_prior_summary_instead_of_destroying_it``.

A third bug, found while integrating this file with the sibling API-level fix in
``tests/api/test_files_summary_status.py``: ``retry_summary_if_available`` called
``asyncio.run(check_llm_availability())`` internally, but its one caller
(``retry_summary`` in ``api/endpoints/files/summary_status.py``) is an async
endpoint already running inside a live event loop -- ``asyncio.run()`` cannot
nest inside a running loop, so every real call from that endpoint raised
``RuntimeError`` regardless of the uuid bug above. Fixed by making this function
``async`` and ``await``-ing ``check_llm_availability()`` directly; every call
below is wrapped in ``asyncio.run(...)`` accordingly.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.tasks import summarization
from app.tasks import summary_retry


@pytest.fixture
def make_media_file(db_session, normal_user):
    def _make(
        status: FileStatus = FileStatus.COMPLETED,
        summary_status: str | None = "failed",
        summary_data: dict | None = None,
        summary_opensearch_id: str | None = None,
    ) -> MediaFile:
        mf = MediaFile(
            user_id=normal_user.id,
            filename=f"retry-test-{uuid.uuid4().hex[:8]}.mp3",
            storage_path=f"retry-test/{uuid.uuid4().hex}.mp3",
            file_size=1234,
            content_type="audio/mpeg",
            status=status,
            summary_status=summary_status,
            summary_data=summary_data,
            summary_opensearch_id=summary_opensearch_id,
        )
        db_session.add(mf)
        db_session.commit()
        db_session.refresh(mf)
        return mf

    return _make


class TestNotFoundAndMalformedInput:
    """Pins the fix: a missing/malformed uuid returns False, never raises."""

    def test_reset_summary_for_retry_returns_false_for_a_nonexistent_uuid(self, db_session):
        result = summary_retry.reset_summary_for_retry(db_session, str(uuid.uuid4()))

        assert result is False

    def test_reset_summary_for_retry_returns_false_for_a_malformed_uuid(self, db_session):
        result = summary_retry.reset_summary_for_retry(db_session, "not-a-uuid-at-all")

        assert result is False

    def test_retry_summary_if_available_returns_false_for_a_nonexistent_uuid(
        self, db_session, monkeypatch
    ):
        monkeypatch.setattr(
            summary_retry, "check_llm_availability", _async_returning(True), raising=True
        )

        result = asyncio.run(
            summary_retry.retry_summary_if_available(db_session, str(uuid.uuid4()))
        )

        assert result is False

    def test_retry_summary_if_available_returns_false_for_a_malformed_uuid(
        self, db_session, monkeypatch
    ):
        monkeypatch.setattr(
            summary_retry, "check_llm_availability", _async_returning(True), raising=True
        )

        result = asyncio.run(
            summary_retry.retry_summary_if_available(db_session, "definitely-not-a-uuid")
        )

        assert result is False


def _async_returning(value):
    async def _coro():
        return value

    return lambda: _coro()


class TestResetSummaryForRetry:
    def test_resets_summary_fields_and_persists_when_transcription_is_completed(
        self, db_session, make_media_file
    ):
        mf = make_media_file(
            status=FileStatus.COMPLETED,
            summary_status="failed",
            summary_data={"bluf": "old summary"},
            summary_opensearch_id="legacy-doc-id",
        )

        result = summary_retry.reset_summary_for_retry(db_session, str(mf.uuid))

        assert result is True
        assert mf.summary_data is None
        assert mf.summary_opensearch_id is None
        assert mf.summary_status == "pending"
        # Prove it was actually committed, not just mutated in memory.
        db_session.expire(mf)
        refetched = db_session.query(MediaFile).filter(MediaFile.id == mf.id).one()
        assert refetched.summary_status == "pending"
        assert refetched.summary_data is None

    def test_refuses_and_leaves_data_untouched_when_transcription_is_not_completed(
        self, db_session, make_media_file
    ):
        mf = make_media_file(
            status=FileStatus.PROCESSING,
            summary_status="failed",
            summary_data={"bluf": "must survive"},
        )

        result = summary_retry.reset_summary_for_retry(db_session, str(mf.uuid))

        assert result is False
        assert mf.summary_data == {"bluf": "must survive"}
        assert mf.summary_status == "failed"

    def test_returns_false_without_raising_when_commit_fails(self, db_session, make_media_file):
        """The ``except Exception: db.rollback(); return False`` branch: a commit
        failure (e.g. a dropped DB connection) must come back as ``False``, never
        propagate -- and the except block's own cleanup call must actually run.
        (Session survival past a rollback of a savepoint mid-test is a property of
        this test harness's nested-transaction plumbing, not of
        ``reset_summary_for_retry`` itself, so it is intentionally not asserted
        here -- asserting it would test the fixture, not the function.)"""
        mf = make_media_file(status=FileStatus.COMPLETED, summary_status="failed")

        def _explode():
            raise RuntimeError("connection reset")

        real_commit = db_session.commit
        real_rollback = db_session.rollback
        rollback_calls: list[bool] = []

        def _spy_rollback():
            rollback_calls.append(True)
            return real_rollback()

        db_session.commit = _explode
        db_session.rollback = _spy_rollback
        try:
            result = summary_retry.reset_summary_for_retry(db_session, str(mf.uuid))
        finally:
            db_session.commit = real_commit
            db_session.rollback = real_rollback

        assert result is False
        assert rollback_calls == [True]


class TestCheckLlmAvailability:
    def test_true_when_the_service_reports_available(self, monkeypatch):
        async def _available(user_id: int | None = None) -> bool:
            return True

        monkeypatch.setattr(summary_retry, "is_llm_available", _available, raising=True)

        assert asyncio.run(summary_retry.check_llm_availability()) is True

    def test_false_when_the_service_raises(self, monkeypatch):
        async def _boom(user_id: int | None = None) -> bool:
            raise RuntimeError("provider unreachable")

        monkeypatch.setattr(summary_retry, "is_llm_available", _boom, raising=True)

        assert asyncio.run(summary_retry.check_llm_availability()) is False


class TestRetrySummaryIfAvailable:
    def test_returns_false_and_does_not_reset_when_llm_is_unavailable(
        self, db_session, make_media_file, monkeypatch
    ):
        monkeypatch.setattr(
            summary_retry, "check_llm_availability", _async_returning(False), raising=True
        )
        mf = make_media_file(
            status=FileStatus.COMPLETED,
            summary_status="failed",
            summary_data={"bluf": "must survive"},
        )

        result = asyncio.run(summary_retry.retry_summary_if_available(db_session, str(mf.uuid)))

        assert result is False
        # The early "LLM unavailable" return must happen BEFORE any reset.
        assert mf.summary_status == "failed"
        assert mf.summary_data == {"bluf": "must survive"}

    def test_returns_false_when_the_transcription_is_not_completed(
        self, db_session, make_media_file, monkeypatch
    ):
        monkeypatch.setattr(
            summary_retry, "check_llm_availability", _async_returning(True), raising=True
        )
        mf = make_media_file(status=FileStatus.PROCESSING, summary_status="failed")

        result = asyncio.run(summary_retry.retry_summary_if_available(db_session, str(mf.uuid)))

        assert result is False

    def test_resets_the_summary_and_dispatches_the_task_when_available(
        self, db_session, make_media_file, monkeypatch
    ):
        monkeypatch.setattr(
            summary_retry, "check_llm_availability", _async_returning(True), raising=True
        )
        mf = make_media_file(
            status=FileStatus.COMPLETED,
            summary_status="failed",
            summary_data={"bluf": "stale"},
        )
        calls: list[str] = []
        monkeypatch.setattr(
            summarization.summarize_transcript_task,
            "delay",
            lambda file_uuid: calls.append(file_uuid),
            raising=True,
        )

        result = asyncio.run(summary_retry.retry_summary_if_available(db_session, str(mf.uuid)))

        assert result is True
        assert calls == [str(mf.uuid)]
        assert mf.summary_status == "pending"
        assert mf.summary_data is None

    def test_a_dispatch_failure_restores_the_prior_summary_instead_of_destroying_it(
        self, db_session, make_media_file, monkeypatch
    ):
        """Regression test for a fixed bug (was: known bug, deliberately not fixed).

        ``retry_summary_if_available`` resets the summary (commits) and only
        THEN calls ``summarize_transcript_task.delay(...)``. If dispatch raises
        (e.g. a broker outage), the function must return ``False`` WITHOUT
        leaving the previous ``summary_data``/``summary_status`` destroyed --
        the caller (``summary_status.py``'s ``retry_summary`` endpoint) turns
        the ``False`` into a 500 "Failed to queue summary retry", which reads
        as "nothing happened", so it must actually be true. Fixed by capturing
        the pre-reset values and restoring them if dispatch fails, rather than
        reordering to dispatch-before-reset -- that reordering was considered
        and rejected because a fast worker could complete the task and write
        the new summary before this function's own commit ran, and the reset
        would then immediately clobber that freshly-written result.
        """
        monkeypatch.setattr(
            summary_retry, "check_llm_availability", _async_returning(True), raising=True
        )
        mf = make_media_file(
            status=FileStatus.COMPLETED,
            summary_status="failed",
            summary_data={"bluf": "should survive a failed retry"},
        )

        def _boom(file_uuid):
            raise RuntimeError("broker unreachable")

        monkeypatch.setattr(summarization.summarize_transcript_task, "delay", _boom, raising=True)

        result = asyncio.run(summary_retry.retry_summary_if_available(db_session, str(mf.uuid)))

        assert result is False
        # The fix: a failed dispatch restores the prior summary rather than
        # leaving it wiped with nothing queued to regenerate it.
        assert mf.summary_status == "failed"
        assert mf.summary_data == {"bluf": "should survive a failed retry"}
        # Prove it was actually committed, not just mutated in memory.
        db_session.expire(mf)
        refetched = db_session.query(MediaFile).filter(MediaFile.id == mf.id).one()
        assert refetched.summary_status == "failed"
        assert refetched.summary_data == {"bluf": "should survive a failed retry"}
