"""Tests for ``app.utils.stats_helpers`` — the consolidated admin/system stats
aggregate queries (issue #474).

Most of these functions run one FILTER-based aggregate query over the WHOLE
``user``/``media_file``/``task`` tables — tables the live dev database already has
real rows in, since these tests run against the dev Postgres via the savepoint-isolated
``db_session`` fixture (see ``backend/tests/CLAUDE.md``). Two strategies are used
depending on whether the table can be safely reset within the test's own transaction
(rolled back at teardown either way, per ``tests/conftest.py::db_session``):

* ``task`` has **no foreign keys pointing at it** (verified: `grep 'ForeignKey("task'
  app/models/` finds nothing), so tests that need an exact, uncontaminated count
  clear it first with ``db_session.query(Task).delete()`` and assert exact values.
* ``user`` and ``media_file`` are referenced by many other tables, so clearing them
  risks FK errors against real dev rows. Those tests use **delta assertions**
  (call, seed known rows, call again, assert the exact delta) instead, which is
  correct for every counter/sum here because they are all linear (``COUNT``/``SUM``
  with a ``FILTER``) — the one exception, ``get_processing_eta``'s division-based
  ``files_per_hour``/``hours_remaining``, is additionally tested with a tiny hand-built
  fake session (not ``unittest.mock`` — a two-method stand-in for the exact
  ``db.query(func.count()).filter(...).scalar()`` chain the function calls) so the
  zero-throughput and positive-throughput branches get exact-value coverage that
  does not depend on what is happening in the shared dev database "right now".
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func

from app.core.constants import CeleryQueues
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.models.user import User
from app.utils.stats_helpers import get_file_stats
from app.utils.stats_helpers import get_file_timing_stats
from app.utils.stats_helpers import get_models_info
from app.utils.stats_helpers import get_processing_eta
from app.utils.stats_helpers import get_queue_depths
from app.utils.stats_helpers import get_recent_tasks
from app.utils.stats_helpers import get_task_stats
from app.utils.stats_helpers import get_throughput_stats
from app.utils.stats_helpers import get_user_stats
from app.utils.task_utils import TASK_STATUS_COMPLETED
from app.utils.task_utils import TASK_STATUS_FAILED
from app.utils.task_utils import TASK_STATUS_IN_PROGRESS
from app.utils.task_utils import TASK_STATUS_PENDING


def _make_user(db_session, *, is_active: bool = True, role: str = "user") -> User:
    from app.core.security import get_password_hash

    uid = uuid_pkg.uuid4().hex[:10]
    user = User(
        email=f"stats-fixture-{uid}@example.com",
        full_name="Stats Fixture",
        hashed_password=get_password_hash("password123"),  # noqa: S106 — throwaway fixture row
        is_active=is_active,
        is_superuser=role == "super_admin",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _make_media_file(
    db_session,
    user_id: int,
    *,
    status: str = "completed",
    file_size: int = 100,
    duration: float | None = None,
    completed_at: datetime | None = None,
) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=fuuid,
        filename=f"stats-fixture-{fuuid.hex[:8]}.wav",
        storage_path=f"media/stats-fixture/{fuuid}.wav",
        content_type="audio/wav",
        file_size=file_size,
        duration=duration,
        user_id=user_id,
        status=status,
        completed_at=completed_at,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _make_task(
    db_session,
    user_id: int,
    *,
    status: str,
    task_type: str = "transcription",
    created_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> Task:
    task = Task(
        id=f"stats-fixture-task-{uuid_pkg.uuid4().hex[:12]}",
        user_id=user_id,
        task_type=task_type,
        status=status,
        created_at=created_at if created_at is not None else datetime.now(UTC),
        completed_at=completed_at,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


# ---------------------------------------------------------------------------
# get_user_stats — delta assertions (user has too many FK dependents to clear)
# ---------------------------------------------------------------------------


class TestGetUserStats:
    def test_default_counts_users_created_just_now_as_new(self, db_session):
        before = get_user_stats(db_session)

        _make_user(db_session)
        _make_user(db_session)

        after = get_user_stats(db_session)

        assert after["total"] - before["total"] == 2
        assert after["new"] - before["new"] == 2
        assert set(after.keys()) == {"total", "new"}  # no breakdown keys leak in

    def test_breakdown_reports_active_inactive_and_superuser_deltas(self, db_session):
        before = get_user_stats(db_session, include_breakdown=True)

        _make_user(db_session, is_active=True)
        _make_user(db_session, is_active=True, role="super_admin")
        _make_user(db_session, is_active=False)

        after = get_user_stats(db_session, include_breakdown=True)

        assert after["total"] - before["total"] == 3
        assert after["active"] - before["active"] == 2
        assert after["superusers"] - before["superusers"] == 1
        assert after["new"] - before["new"] == 3
        # inactive is DERIVED as total - active; the identity must hold on the
        # actual returned numbers, not just on the delta.
        assert after["inactive"] == after["total"] - after["active"]
        assert after["inactive"] - before["inactive"] == 1


# ---------------------------------------------------------------------------
# get_file_stats — delta assertions (media_file has 17 FK dependents)
# ---------------------------------------------------------------------------


class TestGetFileStats:
    def test_default_totals_and_sums(self, db_session, normal_user):
        before = get_file_stats(db_session)

        _make_media_file(db_session, normal_user.id, file_size=1000, duration=10.5)
        _make_media_file(db_session, normal_user.id, file_size=2000, duration=5.25)

        after = get_file_stats(db_session)

        assert after["total"] - before["total"] == 2
        assert after["new"] - before["new"] == 2
        assert after["total_size"] - before["total_size"] == 3000
        # Each side is independently rounded to 2dp before the subtraction, so allow
        # a hair of slack rather than asserting bit-exact float equality.
        assert (after["total_duration"] - before["total_duration"]) == pytest.approx(
            15.75, abs=0.02
        )

    def test_status_breakdown_deltas(self, db_session, normal_user):
        before = get_file_stats(db_session, include_status_breakdown=True)

        _make_media_file(db_session, normal_user.id, status="pending")
        _make_media_file(db_session, normal_user.id, status="processing")
        _make_media_file(db_session, normal_user.id, status="completed")
        _make_media_file(db_session, normal_user.id, status="completed")
        _make_media_file(db_session, normal_user.id, status="error")

        after = get_file_stats(db_session, include_status_breakdown=True)

        assert after["by_status"]["pending"] - before["by_status"]["pending"] == 1
        assert after["by_status"]["processing"] - before["by_status"]["processing"] == 1
        assert after["by_status"]["completed"] - before["by_status"]["completed"] == 2
        assert after["by_status"]["error"] - before["by_status"]["error"] == 1
        assert "by_status" not in get_file_stats(db_session)  # default omits it

    def test_segment_and_speaker_counts_reflect_new_rows(self, db_session, normal_user):
        media_file = _make_media_file(db_session, normal_user.id)
        before = get_file_stats(db_session)

        speaker = Speaker(
            uuid=uuid_pkg.uuid4(),
            name="SPEAKER_00",
            user_id=normal_user.id,
            media_file_id=media_file.id,
        )
        db_session.add(speaker)
        db_session.commit()
        db_session.refresh(speaker)

        db_session.add_all(
            [
                TranscriptSegment(
                    uuid=uuid_pkg.uuid4(),
                    media_file_id=media_file.id,
                    speaker_id=speaker.id,
                    start_time=0.0,
                    end_time=1.0,
                    text="one",
                ),
                TranscriptSegment(
                    uuid=uuid_pkg.uuid4(),
                    media_file_id=media_file.id,
                    speaker_id=speaker.id,
                    start_time=1.0,
                    end_time=2.0,
                    text="two",
                ),
            ]
        )
        db_session.commit()

        after = get_file_stats(db_session)

        assert after["segments"] - before["segments"] == 2
        assert after["speakers"] - before["speakers"] == 1


# ---------------------------------------------------------------------------
# get_task_stats / get_recent_tasks / get_file_timing_stats — task has NO FK
# dependents, so these clear the table for exact (non-delta) assertions.
# ---------------------------------------------------------------------------


class TestGetTaskStats:
    def test_empty_task_table_reports_all_zeros(self, db_session):
        db_session.query(Task).delete()

        result = get_task_stats(db_session)

        assert result == {
            "total": 0,
            "pending": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "success_rate": 0,
            "avg_processing_time": 0,
        }

    def test_counts_success_rate_and_avg_processing_time(self, db_session, normal_user):
        db_session.query(Task).delete()
        now = datetime.now(UTC)

        _make_task(
            db_session,
            normal_user.id,
            status=TASK_STATUS_COMPLETED,
            created_at=now - timedelta(seconds=300),
            completed_at=now - timedelta(seconds=200),  # 100s
        )
        _make_task(
            db_session,
            normal_user.id,
            status=TASK_STATUS_COMPLETED,
            created_at=now - timedelta(seconds=600),
            completed_at=now - timedelta(seconds=300),  # 300s
        )
        _make_task(db_session, normal_user.id, status=TASK_STATUS_PENDING)
        _make_task(db_session, normal_user.id, status=TASK_STATUS_IN_PROGRESS)
        _make_task(db_session, normal_user.id, status=TASK_STATUS_FAILED)

        result = get_task_stats(db_session)

        assert result["total"] == 5
        assert result["pending"] == 1
        assert result["running"] == 1
        assert result["completed"] == 2
        assert result["failed"] == 1
        assert result["success_rate"] == 40.0  # 2/5 * 100
        assert result["avg_processing_time"] == 200.0  # mean(100, 300)

    def test_a_completed_task_with_no_timestamps_is_excluded_from_the_average(
        self, db_session, normal_user
    ):
        """The AVG's own filter (completed_at/created_at not null) is narrower than the
        ``completed`` COUNT's filter (status alone) — a completed row with no timestamps
        must still count toward ``completed`` but not skew ``avg_processing_time``.
        """
        db_session.query(Task).delete()
        now = datetime.now(UTC)

        _make_task(
            db_session,
            normal_user.id,
            status=TASK_STATUS_COMPLETED,
            created_at=now - timedelta(seconds=120),
            completed_at=now - timedelta(seconds=20),  # 100s
        )
        # completed_at explicitly left NULL — must not raise and must not count in the avg.
        timeless = Task(
            id=f"stats-fixture-task-{uuid_pkg.uuid4().hex[:12]}",
            user_id=normal_user.id,
            task_type="transcription",
            status=TASK_STATUS_COMPLETED,
            created_at=now,
            completed_at=None,
        )
        db_session.add(timeless)
        db_session.commit()

        result = get_task_stats(db_session)

        assert result["completed"] == 2
        assert result["avg_processing_time"] == 100.0


class TestGetRecentTasks:
    def test_orders_by_created_at_descending_and_respects_limit(self, db_session, normal_user):
        db_session.query(Task).delete()
        now = datetime.now(UTC)

        t_old = _make_task(
            db_session, normal_user.id, status="completed", created_at=now - timedelta(minutes=10)
        )
        t_mid = _make_task(
            db_session, normal_user.id, status="completed", created_at=now - timedelta(minutes=5)
        )
        t_new = _make_task(db_session, normal_user.id, status="pending", created_at=now)

        result = get_recent_tasks(db_session, limit=2)

        assert [r["id"] for r in result] == [t_new.id, t_mid.id]
        assert t_old.id not in [r["id"] for r in result]

    def test_elapsed_time_uses_completed_at_when_present(self, db_session, normal_user):
        db_session.query(Task).delete()
        now = datetime.now(UTC)

        _make_task(
            db_session,
            normal_user.id,
            status="completed",
            created_at=now - timedelta(seconds=500),
            completed_at=now - timedelta(seconds=200),  # exactly 300s elapsed
        )

        result = get_recent_tasks(db_session, limit=10)

        assert len(result) == 1
        assert result[0]["elapsed"] == 300
        assert result[0]["status"] == "completed"
        assert result[0]["type"] == "transcription"
        assert result[0]["created_at"] is not None

    def test_elapsed_time_falls_back_to_now_when_not_completed(self, db_session, normal_user):
        db_session.query(Task).delete()
        now = datetime.now(UTC)

        _make_task(
            db_session, normal_user.id, status="pending", created_at=now - timedelta(seconds=42)
        )

        result = get_recent_tasks(db_session, limit=10)

        assert len(result) == 1
        # computed against "now" inside the function, which runs a beat after ours
        assert 40 <= result[0]["elapsed"] <= 50


class TestGetFileTimingStats:
    def test_only_completed_transcription_tasks_count(self, db_session, normal_user):
        db_session.query(Task).delete()
        now = datetime.now(UTC)

        _make_task(
            db_session,
            normal_user.id,
            task_type="transcription",
            status=TASK_STATUS_COMPLETED,
            created_at=now - timedelta(seconds=300),
            completed_at=now - timedelta(seconds=240),  # 60s
        )
        _make_task(
            db_session,
            normal_user.id,
            task_type="transcription",
            status=TASK_STATUS_COMPLETED,
            created_at=now - timedelta(seconds=600),
            completed_at=now - timedelta(seconds=480),  # 120s
        )
        _make_task(
            db_session,
            normal_user.id,
            task_type="transcription",
            status=TASK_STATUS_COMPLETED,
            created_at=now - timedelta(seconds=900),
            completed_at=now - timedelta(seconds=720),  # 180s
        )
        # Excluded: wrong task_type.
        _make_task(
            db_session,
            normal_user.id,
            task_type="summarization",
            status=TASK_STATUS_COMPLETED,
            created_at=now - timedelta(seconds=100),
            completed_at=now,
        )
        # Excluded: not completed.
        _make_task(
            db_session, normal_user.id, task_type="transcription", status=TASK_STATUS_PENDING
        )
        # Excluded: completed but no completed_at.
        _make_task(
            db_session,
            normal_user.id,
            task_type="transcription",
            status=TASK_STATUS_COMPLETED,
            completed_at=None,
        )

        result = get_file_timing_stats(db_session)

        assert result["files"] == 3
        assert result["avg_secs"] == 120
        assert result["min_secs"] == 60
        assert result["max_secs"] == 180
        assert result["avg_mins"] == 2.0

    def test_empty_reports_zeros(self, db_session):
        db_session.query(Task).delete()

        result = get_file_timing_stats(db_session)

        assert result == {"files": 0, "avg_secs": 0, "min_secs": 0, "max_secs": 0, "avg_mins": 0}


# ---------------------------------------------------------------------------
# get_throughput_stats — delta assertions against the shared media_file table
# ---------------------------------------------------------------------------


class TestGetThroughputStats:
    def test_counts_and_rates_reflect_newly_completed_files(self, db_session, normal_user):
        before = get_throughput_stats(db_session)
        now = datetime.now(UTC)

        # last_1h AND last_3h
        _make_media_file(db_session, normal_user.id, completed_at=now - timedelta(minutes=30))
        _make_media_file(db_session, normal_user.id, completed_at=now - timedelta(minutes=45))
        # last_3h only
        _make_media_file(db_session, normal_user.id, completed_at=now - timedelta(hours=2))
        # neither window
        _make_media_file(db_session, normal_user.id, completed_at=now - timedelta(hours=4))

        after = get_throughput_stats(db_session)

        assert after["total_completed"] - before["total_completed"] == 4
        assert after["last_1h"] - before["last_1h"] == 2
        assert after["last_3h"] - before["last_3h"] == 3
        assert after["rate_1h"] == after["last_1h"]
        assert after["rate_3h"] == round(after["last_3h"] / 3.0, 1)

    def test_non_completed_files_are_never_counted(self, db_session, normal_user):
        before = get_throughput_stats(db_session)
        now = datetime.now(UTC)

        pending = _make_media_file(db_session, normal_user.id, status="pending")
        pending.completed_at = now  # a completed_at with the "wrong" status must not count
        db_session.commit()

        after = get_throughput_stats(db_session)

        assert after["total_completed"] == before["total_completed"]
        assert after["last_1h"] == before["last_1h"]
        assert after["last_3h"] == before["last_3h"]


# ---------------------------------------------------------------------------
# get_processing_eta — live-DB delta test plus a fake-session branch test
# ---------------------------------------------------------------------------


class TestGetProcessingEta:
    def test_remaining_and_rate_reflect_newly_added_files(self, db_session, normal_user):
        now = datetime.now(UTC)
        three_hours_ago = now - timedelta(hours=3)

        completed_3h_before = (
            db_session.query(func.count())
            .select_from(MediaFile)
            .filter(
                MediaFile.status == "completed",
                MediaFile.completed_at.isnot(None),
                MediaFile.completed_at > three_hours_ago,
            )
            .scalar()
        ) or 0
        remaining_before = (
            db_session.query(func.count())
            .select_from(MediaFile)
            .filter(MediaFile.file_size > 0, MediaFile.status.in_(["pending", "processing"]))
            .scalar()
        ) or 0

        for _ in range(3):
            _make_media_file(db_session, normal_user.id, completed_at=now - timedelta(minutes=10))
        for _ in range(4):
            _make_media_file(db_session, normal_user.id, status="pending", file_size=100)
        for _ in range(3):
            _make_media_file(db_session, normal_user.id, status="processing", file_size=100)
        # Excluded: zero file_size, and a completed file (wrong status for "remaining").
        _make_media_file(db_session, normal_user.id, status="pending", file_size=0)
        _make_media_file(db_session, normal_user.id, status="completed", file_size=100)

        result = get_processing_eta(db_session)

        expected_completed_3h = completed_3h_before + 3
        expected_remaining = remaining_before + 7
        expected_files_per_hour = (
            round(expected_completed_3h / 3.0, 1) if expected_completed_3h > 0 else 0
        )
        expected_hours_remaining = (
            round(expected_remaining / expected_files_per_hour, 1)
            if expected_files_per_hour > 0
            else None
        )

        assert result["remaining"] == expected_remaining
        assert result["files_per_hour"] == expected_files_per_hour
        assert result["hours_remaining"] == expected_hours_remaining
        if expected_hours_remaining is not None:
            assert result["est_completion"] is not None
        else:
            assert result["est_completion"] is None

    # -- fake-session branch tests: exact arithmetic, independent of live dev data --

    class _FakeEtaQuery:
        def __init__(self, value: int) -> None:
            self._value = value

        def filter(self, *_args, **_kwargs):
            return self

        def scalar(self):
            return self._value

    class _FakeEtaSession:
        """Stands in for the exact ``db.query(func.count()).filter(...).scalar()``
        chain ``get_processing_eta`` calls, twice, in order: completed_3h then
        remaining. Not ``unittest.mock`` — a two-method fake mirroring the real
        interface, same style as ``test_audio_segment_utils.py``'s ``_fake_subprocess``.
        """

        def __init__(self, values: list[int]) -> None:
            self._values = list(values)

        def query(self, *_args, **_kwargs):
            return TestGetProcessingEta._FakeEtaQuery(self._values.pop(0))

    def test_zero_recent_throughput_yields_no_eta(self):
        fake_db = self._FakeEtaSession([0, 12])  # completed_3h=0, remaining=12

        result = get_processing_eta(fake_db)  # type: ignore[arg-type]  # duck-typed fake, see _FakeEtaSession's docstring

        assert result == {
            "remaining": 12,
            "files_per_hour": 0,
            "hours_remaining": None,
            "est_completion": None,
        }

    def test_positive_throughput_computes_hours_remaining_and_est_completion(self):
        fake_db = self._FakeEtaSession([9, 18])  # completed_3h=9 -> files_per_hour = 3.0

        before = datetime.now(UTC)
        result = get_processing_eta(fake_db)  # type: ignore[arg-type]  # duck-typed fake, see _FakeEtaSession's docstring
        after = datetime.now(UTC)

        assert result["remaining"] == 18
        assert result["files_per_hour"] == 3.0
        assert result["hours_remaining"] == 6.0  # 18 / 3.0

        assert result["est_completion"] is not None
        completion = datetime.fromisoformat(result["est_completion"])
        assert before + timedelta(hours=5.99) <= completion <= after + timedelta(hours=6.01)

    def test_zero_remaining_with_throughput_still_reports_zero_hours(self):
        fake_db = self._FakeEtaSession([6, 0])  # completed_3h=6 -> files_per_hour=2.0, remaining=0

        result = get_processing_eta(fake_db)  # type: ignore[arg-type]  # duck-typed fake, see _FakeEtaSession's docstring

        assert result["remaining"] == 0
        assert result["files_per_hour"] == 2.0
        assert result["hours_remaining"] == 0.0
        assert result["est_completion"] is not None


# ---------------------------------------------------------------------------
# get_queue_depths — no DB, patches app.core.celery.celery_app
# ---------------------------------------------------------------------------


class TestGetQueueDepths:
    def test_reports_llen_per_queue_and_a_total(self, monkeypatch):
        depths = {q: i for i, q in enumerate(CeleryQueues.ALL, start=1)}

        class _FakeRedis:
            def llen(self, name):
                return depths[name]

        fake_celery_app = SimpleNamespace(backend=SimpleNamespace(client=_FakeRedis()))
        monkeypatch.setattr("app.core.celery.celery_app", fake_celery_app)

        result = get_queue_depths()

        for q, n in depths.items():
            assert result[q] == n
        assert result["total"] == sum(depths.values())

    def test_a_single_queue_error_reports_zero_for_that_queue_only(self, monkeypatch):
        class _FlakyRedis:
            def llen(self, name):
                if name == CeleryQueues.GPU:
                    raise RuntimeError("redis hiccup")
                return 5

        fake_celery_app = SimpleNamespace(backend=SimpleNamespace(client=_FlakyRedis()))
        monkeypatch.setattr("app.core.celery.celery_app", fake_celery_app)

        result = get_queue_depths()

        assert result[CeleryQueues.GPU] == 0
        other_queues = [q for q in CeleryQueues.ALL if q != CeleryQueues.GPU]
        for q in other_queues:
            assert result[q] == 5
        assert result["total"] == 5 * len(other_queues)

    def test_a_broken_celery_app_falls_back_to_all_zero(self, monkeypatch):
        # celery_app.backend has no `.client` attribute -> AttributeError inside the try.
        monkeypatch.setattr(
            "app.core.celery.celery_app", SimpleNamespace(backend=SimpleNamespace())
        )

        result = get_queue_depths()

        assert result == {q: 0 for q in CeleryQueues.ALL} | {"total": 0}


# ---------------------------------------------------------------------------
# get_models_info — no DB, patches app.core.config.settings
# ---------------------------------------------------------------------------


class TestGetModelsInfo:
    def test_whisper_and_diarization_entries_are_always_present(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "WHISPER_MODEL", "large-v3-test")
        monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
        monkeypatch.setattr(settings, "LLM_PROVIDER", "")

        result = get_models_info()

        assert result["whisper"] == {
            "name": "large-v3-test",
            "description": "Whisper large-v3-test",
            "purpose": "Speech Recognition & Transcription",
        }
        assert result["diarization"]["purpose"] == "Speaker Identification & Segmentation"
        assert "pyannote" in result["diarization"]["name"]
        assert "search_embedding" not in result
        assert "llm" not in result

    def test_neural_search_entry_uses_the_short_model_name(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", True)
        monkeypatch.setattr(
            "app.services.search.settings_service.get_search_embedding_model",
            lambda: "sentence-transformers/all-MiniLM-L6-v2",
        )

        result = get_models_info()

        assert result["search_embedding"]["name"] == "all-MiniLM-L6-v2"
        assert "sentence-transformers/all-MiniLM-L6-v2" in result["search_embedding"]["description"]
        assert result["search_embedding"]["purpose"] == "Semantic Search & Vector Embeddings"

    def test_neural_search_falls_back_to_settings_when_the_db_lookup_fails(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", True)
        monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_MODEL", "fallback/model")

        def _boom():
            raise RuntimeError("db unavailable")

        monkeypatch.setattr(
            "app.services.search.settings_service.get_search_embedding_model", _boom
        )

        result = get_models_info()

        assert result["search_embedding"]["name"] == "model"
        assert "fallback/model" in result["search_embedding"]["description"]

    def test_no_neural_search_entry_when_disabled(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)

        result = get_models_info()

        assert "search_embedding" not in result

    def test_llm_entry_reads_the_provider_specific_model_name(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
        monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
        monkeypatch.setattr(settings, "OPENAI_MODEL_NAME", "gpt-4o")

        result = get_models_info()

        assert result["llm"] == {
            "name": "gpt-4o",
            "description": "Openai LLM Provider",
            "purpose": "Summarization & Speaker Identification",
        }

    def test_llm_entry_falls_back_to_user_configured_when_the_model_name_is_blank(
        self, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
        monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
        monkeypatch.setattr(settings, "ANTHROPIC_MODEL_NAME", "")

        result = get_models_info()

        assert result["llm"]["name"] == "User-configured"
        assert result["llm"]["description"] == "Anthropic LLM Provider"

    def test_no_llm_entry_when_provider_is_unset(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
        monkeypatch.setattr(settings, "LLM_PROVIDER", "")

        result = get_models_info()

        assert "llm" not in result
