"""Search indexing must not silently give up on a completed transcript (issue #1182).

Under load every attempt of ``index_transcript_search_task`` hit the OpenSearch client's
default 10 s read timeout; three retries later the ``search_indexing`` row was ``failed``
for good and nothing ever re-indexed the file. These tests pin the five requirements:
retry over a long horizon at a higher priority, a configurable indexing timeout, a
durable sweep, and visible state / metrics.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from celery.exceptions import Retry
from opensearchpy.exceptions import ConnectionTimeout
from prometheus_client import REGISTRY

from app.core.config import settings
from app.core.constants import EmbeddingPriority
from app.models.media import MediaFile
from app.models.media import Task
from app.models.media import TranscriptSegment

pytestmark = pytest.mark.unit


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


# --------------------------------------------------------------------------- backoff


class TestBackoff:
    def test_delay_is_exponential_capped_and_jittered(self, monkeypatch):
        from app.services.search import index_retry

        monkeypatch.setattr(settings, "SEARCH_INDEX_RETRY_BASE_DELAY_S", 30)
        monkeypatch.setattr(settings, "SEARCH_INDEX_RETRY_MAX_DELAY_S", 600)

        for retries in range(40):
            ceiling = min(600, 30 * 2**retries)
            samples = {index_retry.retry_delay_seconds(retries) for _ in range(200)}
            assert max(samples) <= ceiling
            assert min(samples) >= ceiling // 2
        # Jitter is real: the same attempt does not always wait the same time.
        assert len({index_retry.retry_delay_seconds(10) for _ in range(200)}) > 1

    def test_attempt_budget_covers_the_horizon(self, monkeypatch):
        from app.services.search import index_retry

        monkeypatch.setattr(settings, "SEARCH_INDEX_RETRY_BASE_DELAY_S", 30)
        monkeypatch.setattr(settings, "SEARCH_INDEX_RETRY_MAX_DELAY_S", 600)
        monkeypatch.setattr(settings, "SEARCH_INDEX_RETRY_HORIZON_S", 6 * 3600)

        n = index_retry.max_retry_attempts()
        # The old budget was 3 retries (~3.5 min). A six hour horizon at a 10 minute
        # cap needs dozens.
        assert n > 30
        nominal = sum(min(600, 30 * 2**i) for i in range(n))
        assert nominal >= 6 * 3600
        assert sum(min(600, 30 * 2**i) for i in range(n - 1)) < 6 * 3600


# ------------------------------------------------------------------- timeout / client


class _RecordingClient:
    """Minimal OpenSearch client that records the per-call ``request_timeout``."""

    def __init__(self) -> None:
        self.timeouts: list[float | None] = []

    def bulk(self, body, refresh=False, request_timeout=None):
        self.timeouts.append(request_timeout)
        return {"errors": False, "items": []}

    def index(self, index, body, request_timeout=None, **_doc_id):
        self.timeouts.append(request_timeout)
        return {"result": "created"}


class TestIdempotentWriteHelper:
    def test_applies_the_configured_timeout(self, monkeypatch):
        from app.services.opensearch_service.client import call_idempotent_write

        monkeypatch.setattr(settings, "OPENSEARCH_INDEX_TIMEOUT_S", 77)
        write = MagicMock(return_value={"errors": False})

        result = call_idempotent_write(write, body=[1], refresh=False)

        assert result == {"errors": False}
        write.assert_called_with(body=[1], refresh=False, request_timeout=77)

    def test_retries_a_timeout_then_succeeds(self, monkeypatch):
        from app.services.opensearch_service.client import call_idempotent_write

        monkeypatch.setattr(settings, "OPENSEARCH_INDEX_TIMEOUT_RETRIES", 2)
        write = MagicMock(side_effect=[ConnectionTimeout("TIMEOUT", "x", Exception()), {"ok": 1}])

        with patch("app.services.opensearch_service.client.time.sleep"):
            assert call_idempotent_write(write, body=[]) == {"ok": 1}
        assert write.call_count == 2

    def test_gives_up_after_the_retry_budget_and_reraises(self, monkeypatch):
        from app.services.opensearch_service.client import call_idempotent_write

        monkeypatch.setattr(settings, "OPENSEARCH_INDEX_TIMEOUT_RETRIES", 2)
        write = MagicMock(side_effect=ConnectionTimeout("TIMEOUT", "x", Exception()))

        with (
            patch("app.services.opensearch_service.client.time.sleep"),
            pytest.raises(ConnectionTimeout),
        ):
            call_idempotent_write(write, body=[])
        assert write.call_count == 3  # first try + 2 retries

    def test_does_not_retry_other_errors(self):
        from app.services.opensearch_service.client import call_idempotent_write

        write = MagicMock(side_effect=ValueError("bad body"))
        with pytest.raises(ValueError):
            call_idempotent_write(write, body=[])
        assert write.call_count == 1


class TestCallSitesUseTheIndexTimeout:
    def test_chunk_bulk_uses_index_timeout(self, monkeypatch):
        from app.services.search import indexing_service

        monkeypatch.setattr(settings, "OPENSEARCH_INDEX_TIMEOUT_S", 61)
        client = _RecordingClient()
        chunk = {"file_uuid": "f", "chunk_index": 0, "text": "t"}

        with patch.object(indexing_service, "opensearch_client", client):
            indexed = indexing_service.TranscriptIndexingService()._bulk_index_chunks([chunk])

        assert indexed == 1
        assert client.timeouts == [61]

    def test_digest_bulk_uses_index_timeout(self, monkeypatch):
        from app.services.search import indexing_service

        monkeypatch.setattr(settings, "OPENSEARCH_INDEX_TIMEOUT_S", 62)
        client = _RecordingClient()

        with patch.object(indexing_service, "opensearch_client", client):
            indexed = indexing_service.TranscriptIndexingService()._bulk_index_documents(
                [("f_digest_0", {"a": 1})], use_neural_pipeline=False
            )

        assert indexed == 1
        assert client.timeouts == [62]

    def test_full_document_index_uses_index_timeout(self, monkeypatch):
        from app.services.opensearch_service import client as os_client
        from app.services.opensearch_service import transcripts

        monkeypatch.setattr(settings, "OPENSEARCH_INDEX_TIMEOUT_S", 63)
        fake = _RecordingClient()
        monkeypatch.setattr(os_client, "opensearch_client", fake)

        with patch.object(transcripts, "ensure_indices_exist"):
            response = transcripts.index_transcript(
                1, "uuid-1", 1, "text", [], "t", organization_id=None
            )

        assert response == {"result": "created"}
        assert fake.timeouts == [63]

    def test_unrelated_search_calls_keep_the_client_default(self):
        """The indexing timeout is per write call; the shared client is not changed."""
        from app.core.opensearch_auth import opensearch_connection_kwargs

        assert "timeout" not in opensearch_connection_kwargs()
        assert "retry_on_timeout" not in opensearch_connection_kwargs()


# ------------------------------------------------------------------------ the task


@contextmanager
def _task_harness(*, retries: int, index_error: Exception):
    """Run ``index_transcript_search_task`` with the DB and OpenSearch faked.

    Yields ``(task, retry_mock, statuses)`` where ``statuses`` collects every
    ``update_task_status`` call as ``(status, error_message)``.
    """
    from app.tasks import search_indexing_task as mod

    statuses: list[tuple[str, str | None]] = []

    def fake_update(db, task_id, status, progress=None, error_message=None, completed=False):
        statuses.append((status, error_message))

    seg = MagicMock()
    seg.start_time, seg.end_time, seg.text, seg.speaker = 0.0, 1.0, "hello", None
    db = MagicMock()
    db.query.return_value.options.return_value.filter.return_value.order_by.return_value.all.return_value = [
        seg
    ]

    @contextmanager
    def fake_scope():
        yield db

    meta = {
        "title": "t",
        "tag_names": [],
        "upload_time": None,
        "language": "en",
        "content_type": "audio/wav",
        "duration": 1.0,
        "file_size": 1,
        "collection_ids": [],
        "accessible_user_ids": [1],
        "organization_id": None,
    }
    service = MagicMock()
    service.index_transcript_chunks.side_effect = index_error

    task = mod.index_transcript_search_task
    retry_mock = MagicMock(side_effect=Retry())
    task.push_request(id="task-1", retries=retries)
    try:
        with (
            patch("app.db.session_utils.session_scope", fake_scope),
            patch("app.db.session_utils.get_refreshed_object", return_value=MagicMock()),
            patch("app.utils.task_utils.create_task_record"),
            patch("app.utils.task_utils.update_task_status", side_effect=fake_update),
            patch.object(mod, "extract_file_index_metadata", return_value=meta),
            patch(
                "app.services.search.indexing_service.TranscriptIndexingService",
                return_value=service,
            ),
            patch("app.services.search.indexing_service.reset_neural_pipeline_state"),
            patch("app.services.opensearch_service.index_transcript"),
            patch.object(mod, "_send_indexing_notification"),
            patch.object(task, "retry", retry_mock),
        ):
            yield task, retry_mock, statuses
    finally:
        task.pop_request()


def _timeout() -> ConnectionTimeout:
    return ConnectionTimeout("TIMEOUT", "Read timed out", Exception("t"))


class TestTaskRetriesUntilIndexed:
    def test_timeout_schedules_a_high_priority_retry_and_shows_pending(self, monkeypatch):
        monkeypatch.setattr(settings, "SEARCH_INDEX_RETRY_MAX_DELAY_S", 600)
        before = _sample("search_indexing_retries_total")

        with _task_harness(retries=3, index_error=_timeout()) as (task, retry, statuses):
            with pytest.raises(Retry):
                task.run(file_id=1, file_uuid="u", user_id=1)

        kwargs = retry.call_args.kwargs
        # Retried indexing jumps the queue ahead of first attempts.
        assert kwargs["priority"] == EmbeddingPriority.PIPELINE_RETRY
        assert EmbeddingPriority.PIPELINE_RETRY < EmbeddingPriority.PIPELINE_CRITICAL
        assert 0 < kwargs["countdown"] <= 600
        # Far beyond the old 3-retry budget.
        assert kwargs["max_retries"] > 30
        # While retries remain the row reads "pending", never a terminal "failed".
        final_status, message = statuses[-1]
        assert final_status == "pending"
        assert "retry" in (message or "").lower()
        assert all(s != "failed" for s, _ in statuses)
        assert _sample("search_indexing_retries_total") == before + 1

    def test_old_retry_budget_no_longer_ends_in_permanent_failure(self):
        """Attempt 4 (retries == 3) used to be the last one."""
        with _task_harness(retries=3, index_error=_timeout()) as (task, retry, _):
            with pytest.raises(Retry):
                task.run(file_id=1, file_uuid="u", user_id=1)
        retry.assert_called_once()

    def test_exhausted_horizon_marks_failed_and_counts_it(self):
        from app.services.search import index_retry

        before = _sample("search_indexing_failures_total", {"reason": "exhausted"})
        with _task_harness(retries=index_retry.max_retry_attempts(), index_error=_timeout()) as (
            task,
            retry,
            statuses,
        ):
            result = task.run(file_id=1, file_uuid="u", user_id=1)

        retry.assert_not_called()
        assert result["status"] == "failed"
        assert statuses[-1][0] == "failed"
        assert _sample("search_indexing_failures_total", {"reason": "exhausted"}) == before + 1

    def test_deleted_file_is_terminal_not_retried_for_hours(self):
        from app.tasks.search_indexing_task import FileGoneError

        with _task_harness(retries=0, index_error=FileGoneError("gone")) as (task, retry, st):
            # The service is never reached for a vanished file; raise it from there
            # to prove the classification, not the lookup.
            result = task.run(file_id=1, file_uuid="u", user_id=1)

        retry.assert_not_called()
        assert result["status"] == "failed"
        assert st[-1][0] == "failed"


# ------------------------------------------------------------------------- sweep


def _make_file(db, user, *, status="completed", segments=True, completed_ago_h=1.0):
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        filename=f"{file_uuid}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        user_id=user.id,
        status=status,
        completed_at=datetime.now(UTC) - timedelta(hours=completed_ago_h),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    if segments:
        db.add(
            TranscriptSegment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=row.id,
                start_time=0.0,
                end_time=1.0,
                text="hello",
            )
        )
        db.commit()
    return row


def _add_index_task(db, user, file, status, *, age_min=60):
    when = datetime.now(UTC) - timedelta(minutes=age_min)
    db.add(
        Task(
            id=str(uuid_pkg.uuid4()),
            user_id=user.id,
            media_file_id=file.id,
            task_type="search_indexing",
            status=status,
            progress=0.0,
            created_at=when,
            updated_at=when,
        )
    )
    db.commit()


class _FakeRedis:
    def __init__(self):
        self.keys: set[str] = set()

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True


@pytest.fixture
def sweep_env(monkeypatch):
    """OpenSearch on, a fake Redis, and a captured dispatch."""
    monkeypatch.setattr(settings, "OPENSEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "SEARCH_INDEX_SWEEP_BATCH_SIZE", 50)
    redis = _FakeRedis()
    task_mock = MagicMock()
    with (
        patch("app.core.redis.get_redis", return_value=redis),
        patch("app.tasks.search_indexing_task.index_transcript_search_task", task_mock),
    ):
        yield redis, task_mock


class TestSweep:
    def test_redispatches_failed_and_missing_but_not_healthy_files(
        self, db_session, normal_user, sweep_env
    ):
        from app.tasks.search_index_sweep_task import run_search_index_sweep

        _, task_mock = sweep_env
        failed = _make_file(db_session, normal_user)
        _add_index_task(db_session, normal_user, failed, "failed")
        missing = _make_file(db_session, normal_user)  # completed, never got a row
        done = _make_file(db_session, normal_user)
        _add_index_task(db_session, normal_user, done, "completed")
        retrying = _make_file(db_session, normal_user)
        _add_index_task(db_session, normal_user, retrying, "pending")
        running = _make_file(db_session, normal_user)
        _add_index_task(db_session, normal_user, running, "in_progress")
        _make_file(db_session, normal_user, status="processing")  # not complete yet
        _make_file(db_session, normal_user, segments=False)  # nothing to index

        result = run_search_index_sweep(db_session)

        dispatched = {c.kwargs["kwargs"]["file_id"] for c in task_mock.apply_async.call_args_list}
        assert dispatched == {failed.id, missing.id}
        assert result["dispatched"] == 2
        for call in task_mock.apply_async.call_args_list:
            assert call.kwargs["priority"] == EmbeddingPriority.PIPELINE_RETRY

    def test_a_later_success_supersedes_an_earlier_failure(
        self, db_session, normal_user, sweep_env
    ):
        from app.tasks.search_index_sweep_task import run_search_index_sweep

        _, task_mock = sweep_env
        f = _make_file(db_session, normal_user)
        _add_index_task(db_session, normal_user, f, "failed", age_min=120)
        _add_index_task(db_session, normal_user, f, "completed", age_min=10)

        result = run_search_index_sweep(db_session)

        assert result["dispatched"] == 0
        task_mock.apply_async.assert_not_called()

    def test_idempotent_second_run_does_not_redispatch(self, db_session, normal_user, sweep_env):
        from app.tasks.search_index_sweep_task import run_search_index_sweep

        _, task_mock = sweep_env
        f = _make_file(db_session, normal_user)
        _add_index_task(db_session, normal_user, f, "failed")

        first = run_search_index_sweep(db_session)
        second = run_search_index_sweep(db_session)

        assert first["dispatched"] == 1
        assert second["dispatched"] == 0
        assert task_mock.apply_async.call_count == 1

    def test_batch_is_bounded(self, db_session, normal_user, sweep_env, monkeypatch):
        from app.tasks.search_index_sweep_task import run_search_index_sweep

        _, task_mock = sweep_env
        monkeypatch.setattr(settings, "SEARCH_INDEX_SWEEP_BATCH_SIZE", 2)
        for _ in range(5):
            f = _make_file(db_session, normal_user)
            _add_index_task(db_session, normal_user, f, "failed")

        result = run_search_index_sweep(db_session)

        assert result["dispatched"] == 2
        assert task_mock.apply_async.call_count == 2

    def test_files_older_than_the_lookback_without_a_row_are_left_alone(
        self, db_session, normal_user, sweep_env, monkeypatch
    ):
        from app.tasks.search_index_sweep_task import run_search_index_sweep

        _, task_mock = sweep_env
        monkeypatch.setattr(settings, "SEARCH_INDEX_SWEEP_LOOKBACK_HOURS", 24)
        _make_file(db_session, normal_user, completed_ago_h=24 * 30)

        result = run_search_index_sweep(db_session)

        assert result["found"] == 0
        task_mock.apply_async.assert_not_called()

    def test_disabled_when_opensearch_is_off(self, db_session, normal_user, sweep_env, monkeypatch):
        from app.tasks.search_index_sweep_task import run_search_index_sweep

        _, task_mock = sweep_env
        monkeypatch.setattr(settings, "OPENSEARCH_ENABLED", False)
        f = _make_file(db_session, normal_user)
        _add_index_task(db_session, normal_user, f, "failed")

        result = run_search_index_sweep(db_session)

        assert result == {"status": "skipped", "reason": "opensearch_disabled"}
        task_mock.apply_async.assert_not_called()

    def test_end_to_end_timeouts_exhausted_then_sweep_recovers(
        self, db_session, normal_user, sweep_env
    ):
        """The row a spent retry budget leaves behind is exactly what the sweep picks up."""
        from app.tasks.search_index_sweep_task import run_search_index_sweep

        _, task_mock = sweep_env
        f = _make_file(db_session, normal_user)
        _add_index_task(db_session, normal_user, f, "failed")

        run_search_index_sweep(db_session)

        (call,) = task_mock.apply_async.call_args_list
        assert call.kwargs["kwargs"] == {
            "file_id": f.id,
            "file_uuid": str(f.uuid),
            "user_id": normal_user.id,
        }


class TestDispatchPriority:
    def test_dispatch_transcript_reindex_forwards_priority(self):
        from app.services.search.reindex_dispatch import dispatch_transcript_reindex

        task_mock = MagicMock()
        with (
            patch("app.core.redis.get_redis", return_value=_FakeRedis()),
            patch("app.tasks.search_indexing_task.index_transcript_search_task", task_mock),
        ):
            assert dispatch_transcript_reindex(
                file_id=1, file_uuid="u", user_id=2, priority=EmbeddingPriority.PIPELINE_RETRY
            )
        assert task_mock.apply_async.call_args.kwargs["priority"] == 1

    def test_default_dispatch_is_unchanged(self):
        from app.services.search.reindex_dispatch import dispatch_transcript_reindex

        task_mock = MagicMock()
        with (
            patch("app.core.redis.get_redis", return_value=_FakeRedis()),
            patch("app.tasks.search_indexing_task.index_transcript_search_task", task_mock),
        ):
            queued = dispatch_transcript_reindex(file_id=1, file_uuid="u2", user_id=2)
        assert queued is True
        assert "priority" not in task_mock.apply_async.call_args.kwargs


# ---------------------------------------------------------------- metrics / schedule


class TestObservabilityAndSchedule:
    def test_awaiting_gauge_counts_failed_and_missing_files(self, db_session, normal_user):
        from app.core.backup_metrics import update_search_indexing_metrics

        failed = _make_file(db_session, normal_user)
        _add_index_task(db_session, normal_user, failed, "failed")
        _make_file(db_session, normal_user)  # missing
        ok = _make_file(db_session, normal_user)
        _add_index_task(db_session, normal_user, ok, "completed")
        retrying = _make_file(db_session, normal_user)
        _add_index_task(db_session, normal_user, retrying, "pending")

        update_search_indexing_metrics(db_session)

        # failed + missing + the file with a retry pending: all three are not yet searchable.
        assert _sample("search_indexing_files_awaiting_reindex") >= 3

    def test_beat_schedule_runs_the_sweep_and_routes_it(self):
        from app.core.celery import celery_app

        entry = celery_app.conf.beat_schedule["search-index-sweep"]
        assert entry["task"] == "search_index_sweep"
        assert celery_app.conf.task_routes["search_index_sweep"]["queue"] == "utility"

    def test_new_settings_are_documented(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        env_example = (root / ".env.example").read_text()
        docs = (root / "docs-site/docs/configuration/environment-variables.md").read_text()
        for name in (
            "OPENSEARCH_INDEX_TIMEOUT_S",
            "OPENSEARCH_INDEX_TIMEOUT_RETRIES",
            "SEARCH_INDEX_RETRY_HORIZON_S",
            "SEARCH_INDEX_RETRY_BASE_DELAY_S",
            "SEARCH_INDEX_RETRY_MAX_DELAY_S",
            "SEARCH_INDEX_SWEEP_BATCH_SIZE",
            "SEARCH_INDEX_SWEEP_COOLDOWN_S",
            "SEARCH_INDEX_SWEEP_LOOKBACK_HOURS",
        ):
            assert name in env_example, f"{name} missing from .env.example"
            assert name in docs, f"{name} missing from the environment-variables docs page"
