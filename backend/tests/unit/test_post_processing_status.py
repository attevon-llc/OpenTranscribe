"""Per-file post-processing status: the signal that makes a "Complete" card true.

The notification card used to reach "All tasks complete" as soon as transcription and the
AI summary/topics finished, while speaker attributes, search indexing and the rest were
still running: those tasks either sent nothing, sent "done" when they were merely
DISPATCHED (search indexing), or were silent when a setting turned them off, so the card
could never wait for them. ``app/services/post_processing_status.py`` is now the one
reporter. Pinned here:

  1. Every :class:`PostProcessingTask` has a reporter and a tier on the card, from one enum.
  2. The decorator reports exactly one start and one outcome per run of a task body; a
     Celery retry reports nothing; a skip and a failure are reported as such.
  3. Frames go to the file's OWNER, carry only the file UUID, task keys, states and times,
     and are suppressed for a quarantined file (the real predicate, not a stub).
  4. Redis keeps last-write-wins per task and drops a previous run's entries on a new plan.
  5. The fan-out reports what it dispatched as one plan, including what a setting skipped.
  6. A hand-off is declared before its parent's terminal frame.
  7. ``GET /api/files/{uuid}/post-processing`` serves the stored states, tenant-gated.
"""

from __future__ import annotations

import re
import time
import uuid as uuid_pkg
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from celery.exceptions import Retry

from app.core.enums import FileStatus
from app.core.enums import PostProcessingState
from app.core.enums import PostProcessingTask
from app.models.media import MediaFile
from app.models.user import User
from app.services import post_processing_status as pps

_BACKEND = Path(__file__).resolve().parents[2]
_APP = _BACKEND / "app"
_FRONTEND_JOB = _BACKEND.parent / "frontend/src/lib/notifications/processingJob.ts"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _mk_user(db) -> User:
    from app.core.security import get_password_hash

    user = User(
        email=f"pps_{uuid_pkg.uuid4().hex[:10]}@example.com",
        full_name="Post-processing status test user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_file(db, owner: User, *, quarantined: bool = False) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    f = MediaFile(
        uuid=fuuid,
        filename=f"secret_name_{fuuid.hex[:8]}.wav",
        storage_path="",
        content_type="audio/wav",
        file_size=1000,
        user_id=owner.id,
        status=FileStatus.COMPLETED,
        is_quarantined=quarantined,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


@contextmanager
def _yield_session(db) -> Iterator[Any]:
    yield db


@pytest.fixture
def bridged(monkeypatch, db_session):
    """The reporter's own session (and takedown_service's) see the test's savepoint rows."""
    monkeypatch.setattr("app.db.session_utils.session_scope", lambda: _yield_session(db_session))
    return db_session


@pytest.fixture
def frames(monkeypatch) -> list[tuple[int, str, dict]]:
    """Every published WebSocket frame, captured at the raw publish primitive.

    Patched BELOW ``send_ws_event_for_file`` so the real quarantine predicate runs.
    """
    sent: list[tuple[int, str, dict]] = []

    def _publish(user_id: int, notification_type: str, data: dict) -> bool:
        sent.append((user_id, notification_type, data))
        return True

    monkeypatch.setattr("app.utils.websocket_notify.send_ws_event", _publish)
    # notification_service binds its own name at import (the batch auto_label_status row).
    monkeypatch.setattr("app.services.notification_service.send_ws_event", _publish)
    return sent


class _NoRedis:
    """A Redis stand-in that refuses everything: the frame must still go out."""

    def eval(self, *a: object, **k: object) -> None:
        raise ConnectionError("redis down")


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr("app.core.redis.get_redis", lambda: _NoRedis())


def _states(sent: list[tuple[int, str, dict]], task: PostProcessingTask) -> list[str]:
    return [
        data["tasks"][task.value]["state"]
        for _, ntype, data in sent
        if ntype == "post_processing_status" and task.value in data["tasks"]
    ]


# --------------------------------------------------------------------------- #
# 1. One vocabulary, every key reported and tiered
# --------------------------------------------------------------------------- #


def test_every_task_key_has_a_tier_on_the_card():
    source = _FRONTEND_JOB.read_text()
    match = re.search(r"export const POST_PROCESSING_TIERS[^=]*=\s*\{([^}]*)\}", source)
    assert match, f"POST_PROCESSING_TIERS not found in {_FRONTEND_JOB}"
    tiers = dict(re.findall(r"(\w+): '(finishing|ai)'", match.group(1)))
    assert set(tiers) == {t.value for t in PostProcessingTask}


def test_every_task_key_has_a_reporter():
    """Each key is reported by a decorated task, or by a step reporter inside another task."""
    reported: set[str] = set()
    decorated = re.compile(r"tracks_post_processing\(\s*PostProcessingTask\.(\w+)")
    step = re.compile(r"reporting_step\(\s*PostProcessingTask\.(\w+)")
    for path in _APP.rglob("*.py"):
        source = path.read_text()
        reported.update(decorated.findall(source))
        reported.update(step.findall(source))
    assert reported == {t.name for t in PostProcessingTask}


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "success"}, PostProcessingState.DONE),
        ({"status": "completed"}, PostProcessingState.DONE),
        ({"status": "done"}, PostProcessingState.DONE),
        (None, PostProcessingState.DONE),
        ({"status": "skipped"}, PostProcessingState.SKIPPED),
        ({"status": "not_found"}, PostProcessingState.SKIPPED),
        ({"status": "disabled"}, PostProcessingState.SKIPPED),
        ({"status": "error"}, PostProcessingState.FAILED),
        ({"status": "failed"}, PostProcessingState.FAILED),
    ],
)
def test_outcome_of_maps_task_returns(result, expected):
    assert pps.outcome_of(result) is expected


# --------------------------------------------------------------------------- #
# 2. The decorator: one start, one outcome, nothing on a retry
# --------------------------------------------------------------------------- #


def _decorated(behaviour, *, announce_start: bool = True):
    @pps.tracks_post_processing(
        PostProcessingTask.ANALYTICS, file_param="file_uuid", announce_start=announce_start
    )
    def body(self, file_uuid: str) -> object:
        return behaviour()

    return body


class TestDecorator:
    def test_success_reports_started_then_done_exactly_once(self, bridged, frames, no_redis):
        owner = _mk_user(bridged)
        f = _mk_file(bridged, owner)

        result = _decorated(lambda: {"status": "success"})(object(), str(f.uuid))

        assert result == {"status": "success"}
        assert _states(frames, PostProcessingTask.ANALYTICS) == ["started", "done"]
        assert {uid for uid, _, _ in frames} == {owner.id}

    def test_a_skipped_return_is_reported_as_skipped(self, bridged, frames, no_redis):
        f = _mk_file(bridged, _mk_user(bridged))
        _decorated(lambda: {"status": "skipped"})(object(), str(f.uuid))
        assert _states(frames, PostProcessingTask.ANALYTICS) == ["started", "skipped"]

    def test_an_exception_is_reported_failed_and_propagates(self, bridged, frames, no_redis):
        f = _mk_file(bridged, _mk_user(bridged))

        def boom() -> object:
            raise RuntimeError("worker fell over")

        with pytest.raises(RuntimeError, match="worker fell over"):
            _decorated(boom)(object(), str(f.uuid))
        assert _states(frames, PostProcessingTask.ANALYTICS) == ["started", "failed"]
        failed = frames[-1][2]["tasks"]["analytics"]
        assert failed["started_at"] <= failed["at"]

    def test_a_celery_retry_reports_no_outcome(self, bridged, frames, no_redis):
        f = _mk_file(bridged, _mk_user(bridged))

        def defer() -> object:
            raise Retry("later")

        with pytest.raises(Retry):
            _decorated(defer)(object(), str(f.uuid))
        assert _states(frames, PostProcessingTask.ANALYTICS) == ["started"]

    def test_deferred_start_reports_only_the_outcome(self, bridged, frames, no_redis):
        f = _mk_file(bridged, _mk_user(bridged))
        _decorated(lambda: {"status": "skipped"}, announce_start=False)(object(), str(f.uuid))
        assert _states(frames, PostProcessingTask.ANALYTICS) == ["skipped"]

    def test_a_missing_file_reports_nothing_and_still_runs(self, bridged, frames, no_redis):
        result = _decorated(lambda: {"status": "success"})(object(), str(uuid_pkg.uuid4()))
        assert result == {"status": "success"}
        assert frames == []


# --------------------------------------------------------------------------- #
# 3. Recipient, payload and quarantine
# --------------------------------------------------------------------------- #


class TestFrames:
    def test_frame_goes_to_the_owner_and_names_nothing_but_the_uuid(
        self, bridged, frames, no_redis
    ):
        owner = _mk_user(bridged)
        other = _mk_user(bridged)
        f = _mk_file(bridged, owner)

        assert pps.report(
            PostProcessingTask.SEARCH_INDEXING, PostProcessingState.QUEUED, file_id=f.id
        )

        assert len(frames) == 1
        user_id, ntype, data = frames[0]
        assert user_id == owner.id and user_id != other.id
        assert ntype == "post_processing_status"
        assert set(data) == {"file_id", "tasks"}
        assert data["file_id"] == str(f.uuid)
        assert set(data["tasks"]["search_indexing"]) == {"state", "at"}
        assert str(f.filename) not in repr(data)

    def test_quarantined_file_frame_is_suppressed(self, bridged, frames, no_redis):
        f = _mk_file(bridged, _mk_user(bridged), quarantined=True)

        assert not pps.report(
            PostProcessingTask.SEARCH_INDEXING, PostProcessingState.STARTED, file_id=f.id
        )
        assert frames == []

    def test_quarantined_file_plan_is_suppressed(self, bridged, frames, no_redis):
        f = _mk_file(bridged, _mk_user(bridged), quarantined=True)
        ref = pps.resolve_file(file_id=f.id)
        assert ref is not None

        assert not pps.report_plan(
            ref,
            {PostProcessingTask.SEARCH_INDEXING: PostProcessingState.QUEUED},
            run_at=time.time(),
        )
        assert frames == []

    def test_a_clean_file_plan_carries_run_at_and_every_decision(self, bridged, frames, no_redis):
        owner = _mk_user(bridged)
        f = _mk_file(bridged, owner)
        ref = pps.resolve_file(file_uuid=str(f.uuid))
        assert ref == pps.FileRef(id=f.id, uuid=str(f.uuid), owner_id=owner.id)

        pps.report_plan(
            ref,
            {
                PostProcessingTask.SEARCH_INDEXING: PostProcessingState.QUEUED,
                PostProcessingTask.SUMMARY: PostProcessingState.SKIPPED,
            },
            run_at=1000.5,
        )

        ((_, _, data),) = frames
        assert data["run_at"] == 1000.5
        assert data["tasks"] == {
            "search_indexing": {"state": "queued", "at": 1000.5},
            "summary": {"state": "skipped", "at": 1000.5},
        }


# --------------------------------------------------------------------------- #
# 4. The stored state (real Redis: the Lua is the thing under test)
# --------------------------------------------------------------------------- #


@pytest.mark.xdist_group("local_model_redis")
class TestStoredState:
    @pytest.fixture
    def redis_client(self, local_model_redis, monkeypatch):
        monkeypatch.setattr("app.core.redis.get_redis", lambda: local_model_redis)
        return local_model_redis

    @pytest.fixture
    def ref(self, redis_client, frames):
        file_id = 10_000_000 + int(uuid_pkg.uuid4().int % 1_000_000_000)
        monkeypatch_ref = pps.FileRef(id=file_id, uuid=str(uuid_pkg.uuid4()), owner_id=1)
        yield monkeypatch_ref
        redis_client.delete(f"post_processing:{file_id}")

    def test_last_write_wins_and_an_older_write_is_dropped(self, ref):
        t = PostProcessingTask.SEARCH_INDEXING
        pps.report(t, PostProcessingState.STARTED, ref=ref, at=100.0)
        pps.report(t, PostProcessingState.DONE, ref=ref, at=110.0, started_at=100.0)
        pps.report(t, PostProcessingState.STARTED, ref=ref, at=105.0)

        stored = pps.read_status(ref.id)
        assert stored["tasks"]["search_indexing"] == {
            "state": "done",
            "at": 110.0,
            "started_at": 100.0,
        }

    def test_a_new_plan_drops_the_previous_run_but_keeps_newer_reports(self, ref):
        pps.report(PostProcessingTask.ANALYTICS, PostProcessingState.DONE, ref=ref, at=50.0)
        # A task of the new run that started before the plan landed.
        pps.report(PostProcessingTask.FILE_FACTS, PostProcessingState.STARTED, ref=ref, at=201.0)

        pps.report_plan(
            ref,
            {
                PostProcessingTask.FILE_FACTS: PostProcessingState.QUEUED,
                PostProcessingTask.SUMMARY: PostProcessingState.SKIPPED,
            },
            run_at=200.0,
        )

        stored = pps.read_status(ref.id)
        assert stored["run_at"] == 200.0
        assert "analytics" not in stored["tasks"]
        assert stored["tasks"]["file_facts"]["state"] == "started"
        assert stored["tasks"]["summary"] == {"state": "skipped", "at": 200.0}

    def test_an_older_plan_cannot_reopen_a_newer_run(self, ref):
        pps.report_plan(ref, {PostProcessingTask.TOPICS: PostProcessingState.QUEUED}, run_at=300.0)
        pps.report_plan(
            ref, {PostProcessingTask.REDACTION: PostProcessingState.QUEUED}, run_at=250.0
        )
        pps.report(PostProcessingTask.ANALYTICS, PostProcessingState.DONE, ref=ref, at=260.0)

        stored = pps.read_status(ref.id)
        assert stored["run_at"] == 300.0
        assert set(stored["tasks"]) == {"topics"}

    def test_the_entry_expires(self, ref, redis_client):
        pps.report(PostProcessingTask.TOPICS, PostProcessingState.QUEUED, ref=ref, at=1.0)
        ttl = redis_client.ttl(f"post_processing:{ref.id}")
        assert 0 < ttl <= 2 * 24 * 3600

    def test_nothing_stored_reads_as_no_run(self, ref):
        assert pps.read_status(ref.id) == {"run_at": None, "tasks": {}}


# --------------------------------------------------------------------------- #
# 5. The fan-out reports one plan
# --------------------------------------------------------------------------- #


class TestPlan:
    @pytest.fixture
    def fanout(self, monkeypatch):
        from app.tasks.transcription import postprocess

        plans: list[tuple[int, dict, float]] = []
        monkeypatch.setattr(
            postprocess.post_processing_status,
            "resolve_file",
            lambda file_id: pps.FileRef(id=file_id, uuid="uuid-1", owner_id=7),
        )
        monkeypatch.setattr(
            postprocess.post_processing_status,
            "report_plan",
            lambda ref, decisions, run_at: plans.append((ref.id, dict(decisions), run_at)),
        )
        monkeypatch.setattr(
            postprocess, "_index_transcript", lambda *a, **k: PostProcessingState.QUEUED
        )
        monkeypatch.setattr(
            "app.tasks.ingest_artifacts_task.dispatch_file_facts", lambda *a, **k: True
        )
        monkeypatch.setattr(
            postprocess, "_dispatch_redaction", lambda *a, **k: PostProcessingState.SKIPPED
        )
        monkeypatch.setattr(
            "app.tasks.transcription.core.trigger_automatic_summarization",
            lambda *a, **k: {
                PostProcessingTask.ANALYTICS: PostProcessingState.QUEUED,
                PostProcessingTask.SUMMARY: PostProcessingState.SKIPPED,
                PostProcessingTask.TOPICS: PostProcessingState.SKIPPED,
            },
        )
        monkeypatch.setattr(
            "app.tasks.speaker_attribute_task.dispatch_speaker_attribute_pipeline",
            lambda *a, **k: "attributes",
        )
        monkeypatch.setattr(
            "app.tasks.speaker_clustering.cluster_speakers_for_file.delay",
            lambda *a, **k: SimpleNamespace(id="x"),
        )
        return postprocess, plans

    def test_every_dispatch_decision_lands_in_one_plan(self, fanout):
        postprocess, plans = fanout
        before = time.time()

        postprocess.enrich_and_dispatch(file_id=42, file_uuid="uuid-1", user_id=7)

        ((file_id, decisions, run_at),) = plans
        assert file_id == 42
        assert before <= run_at <= time.time()
        assert decisions == {
            PostProcessingTask.SEARCH_INDEXING: PostProcessingState.QUEUED,
            PostProcessingTask.FILE_FACTS: PostProcessingState.QUEUED,
            PostProcessingTask.REDACTION: PostProcessingState.SKIPPED,
            PostProcessingTask.ANALYTICS: PostProcessingState.QUEUED,
            PostProcessingTask.SUMMARY: PostProcessingState.SKIPPED,
            PostProcessingTask.TOPICS: PostProcessingState.SKIPPED,
            PostProcessingTask.SPEAKER_ATTRIBUTES: PostProcessingState.QUEUED,
            PostProcessingTask.SPEAKER_CLUSTERING: PostProcessingState.QUEUED,
        }

    def test_a_failed_dispatch_is_in_the_plan_as_failed(self, fanout, monkeypatch):
        postprocess, plans = fanout

        def broken(*a: object, **k: object) -> None:
            raise ConnectionError("broker down")

        monkeypatch.setattr(
            "app.tasks.speaker_attribute_task.dispatch_speaker_attribute_pipeline", broken
        )
        monkeypatch.setattr("app.tasks.speaker_clustering.cluster_speakers_for_file.delay", broken)

        postprocess.enrich_and_dispatch(file_id=42, file_uuid="uuid-1", user_id=7)

        decisions = plans[0][1]
        assert decisions[PostProcessingTask.SPEAKER_ATTRIBUTES] is PostProcessingState.FAILED
        assert decisions[PostProcessingTask.SPEAKER_CLUSTERING] is PostProcessingState.FAILED
        # One failed dispatch did not stop the rest from being planned.
        assert decisions[PostProcessingTask.SEARCH_INDEXING] is PostProcessingState.QUEUED

    def test_detection_off_plans_attributes_as_skipped(self, fanout, monkeypatch):
        postprocess, plans = fanout
        monkeypatch.setattr(
            "app.tasks.speaker_attribute_task.dispatch_speaker_attribute_pipeline",
            lambda *a, **k: "llm_only",
        )
        postprocess.enrich_and_dispatch(file_id=42, file_uuid="uuid-1", user_id=7)
        assert plans[0][1][PostProcessingTask.SPEAKER_ATTRIBUTES] is PostProcessingState.SKIPPED


class TestDownstreamDecisions:
    @pytest.fixture
    def downstream_env(self, db_session, monkeypatch):
        from app.tasks.transcription import downstream

        @contextmanager
        def scope() -> Iterator[Any]:
            yield db_session
            db_session.commit()

        monkeypatch.setattr(downstream, "session_scope", scope)
        monkeypatch.setattr(downstream, "_get_collection_prompt_uuid", lambda *a, **k: None)
        for target in (
            "app.tasks.analytics.analyze_transcript_task.delay",
            "app.tasks.topic_extraction.extract_topics_task.delay",
            "app.tasks.summarization.summarize_transcript_task.delay",
        ):
            monkeypatch.setattr(target, lambda *a, **k: SimpleNamespace(id="x"))
        return downstream

    def _file(self, db, user) -> MediaFile:
        return _mk_file(db, user)

    def test_no_llm_plans_summary_and_topics_as_skipped(
        self, db_session, normal_user, downstream_env, monkeypatch
    ):
        monkeypatch.setattr(
            "app.services.llm_service.LLMService.is_configured_for_user",
            staticmethod(lambda db, user_id: False),
        )
        mf = self._file(db_session, normal_user)

        decisions = downstream_env.trigger_automatic_summarization(mf.id, str(mf.uuid))

        assert decisions == {
            PostProcessingTask.ANALYTICS: PostProcessingState.QUEUED,
            PostProcessingTask.SUMMARY: PostProcessingState.SKIPPED,
            PostProcessingTask.TOPICS: PostProcessingState.SKIPPED,
        }

    def test_with_an_llm_summary_and_topics_are_queued(
        self, db_session, normal_user, downstream_env, monkeypatch
    ):
        monkeypatch.setattr(
            "app.services.llm_service.LLMService.is_configured_for_user",
            staticmethod(lambda db, user_id: True),
        )
        mf = self._file(db_session, normal_user)

        decisions = downstream_env.trigger_automatic_summarization(mf.id, str(mf.uuid))

        assert decisions[PostProcessingTask.SUMMARY] is PostProcessingState.QUEUED
        assert decisions[PostProcessingTask.TOPICS] is PostProcessingState.QUEUED

    def test_auto_topics_off_is_skipped_not_absent(
        self, db_session, normal_user, downstream_env, monkeypatch
    ):
        from app.models.prompt import UserSetting

        monkeypatch.setattr(
            "app.services.llm_service.LLMService.is_configured_for_user",
            staticmethod(lambda db, user_id: True),
        )
        db_session.add(
            UserSetting(
                user_id=normal_user.id, setting_key="auto_topic_extraction", setting_value="false"
            )
        )
        db_session.commit()
        mf = self._file(db_session, normal_user)

        decisions = downstream_env.trigger_automatic_summarization(mf.id, str(mf.uuid))

        assert decisions[PostProcessingTask.TOPICS] is PostProcessingState.SKIPPED


# --------------------------------------------------------------------------- #
# 6. A hand-off is declared before the parent's outcome
# --------------------------------------------------------------------------- #


def test_attribute_detection_declares_the_llm_step_before_its_own_outcome(
    bridged, frames, no_redis, monkeypatch
):
    from datetime import UTC
    from datetime import datetime

    from app.models.media import Speaker
    from app.tasks import speaker_attribute_task as sat

    owner = _mk_user(bridged)
    f = _mk_file(bridged, owner)
    # The engine already classified every speaker: the task's early-skip path, which must
    # still hand off to the LLM step (dispatch itself is no-op'd by the SKIP_CELERY fixture).
    bridged.add(
        Speaker(
            uuid=str(uuid_pkg.uuid4()),
            media_file_id=f.id,
            user_id=owner.id,
            name="SPEAKER_00",
            attributes_predicted_at=datetime.now(UTC),
        )
    )
    bridged.commit()
    monkeypatch.setattr(sat, "session_scope", lambda: _yield_session(bridged))

    sat.detect_speaker_attributes_task.push_request(id="task-attr", retries=0)
    try:
        result = sat.detect_speaker_attributes_task.run(str(f.uuid), owner.id)
    finally:
        sat.detect_speaker_attributes_task.pop_request()

    assert result["status"] == "skipped"
    sequence = [
        (key, entry["state"])
        for _, ntype, data in frames
        if ntype == "post_processing_status"
        for key, entry in data["tasks"].items()
    ]
    assert sequence == [
        ("speaker_attributes", "started"),
        ("speaker_identification", "queued"),
        ("speaker_attributes", "skipped"),
    ]


def test_the_llm_step_is_reported_skipped_when_the_owner_turned_it_off(
    bridged, frames, no_redis, monkeypatch
):
    from app.tasks import speaker_attribute_task as sat

    owner = _mk_user(bridged)
    f = _mk_file(bridged, owner)
    monkeypatch.setattr(sat, "_auto_speaker_identification_enabled", lambda user_id: False)

    sat._dispatch_llm_speaker_identification(str(f.uuid), owner.id)

    assert _states(frames, PostProcessingTask.SPEAKER_IDENTIFICATION) == ["skipped"]


def test_a_no_llm_summary_reports_skipped_and_never_started(bridged, frames, no_redis, monkeypatch):
    from app.tasks import summarization

    owner = _mk_user(bridged)
    f = _mk_file(bridged, owner)
    monkeypatch.setattr(
        summarization,
        "_load_summarization_inputs",
        lambda file_uuid, task_id, force: {"file_id": f.id, "user_id": owner.id, "filename": "x"},
    )
    monkeypatch.setattr(summarization, "_llm_is_configured", lambda user_id: False)
    monkeypatch.setattr(summarization, "session_scope", lambda: _yield_session(bridged))
    monkeypatch.setattr("app.utils.task_utils.update_task_status", lambda *a, **k: None)

    summarization.summarize_transcript_task.push_request(id="task-sum")
    try:
        result = summarization.summarize_transcript_task.run(str(f.uuid))
    finally:
        summarization.summarize_transcript_task.pop_request()

    assert result["status"] == "skipped"
    assert _states(frames, PostProcessingTask.SUMMARY) == ["skipped"]


# --------------------------------------------------------------------------- #
# 7. The REST fallback
# --------------------------------------------------------------------------- #


class TestEndpoint:
    def test_owner_reads_the_stored_states(
        self, client, user_token_headers, normal_user, db_session, monkeypatch
    ):
        f = _mk_file(db_session, normal_user)
        seen: list[int] = []

        def stored(file_id: int) -> dict:
            seen.append(file_id)
            return {
                "run_at": 10.0,
                "tasks": {"search_indexing": {"state": "started", "at": 11.0}},
            }

        monkeypatch.setattr("app.api.endpoints.files.post_processing.read_status", stored)

        response = client.get(f"/api/files/{f.uuid}/post-processing", headers=user_token_headers)

        assert response.status_code == 200
        assert seen == [f.id]
        assert response.json() == {
            "file_id": str(f.uuid),
            "run_at": 10.0,
            "tasks": {"search_indexing": {"state": "started", "at": 11.0, "started_at": None}},
        }

    def test_an_unreadable_store_answers_no_run_known(
        self, client, user_token_headers, normal_user, db_session, monkeypatch
    ):
        f = _mk_file(db_session, normal_user)

        def broken(file_id: int) -> dict:
            raise ConnectionError("redis down")

        monkeypatch.setattr("app.api.endpoints.files.post_processing.read_status", broken)

        response = client.get(f"/api/files/{f.uuid}/post-processing", headers=user_token_headers)

        assert response.status_code == 200
        assert response.json() == {"file_id": str(f.uuid), "run_at": None, "tasks": {}}

    def test_another_users_file_is_refused(
        self, client, other_user_auth_headers, normal_user, db_session
    ):
        f = _mk_file(db_session, normal_user)
        response = client.get(
            f"/api/files/{f.uuid}/post-processing", headers=other_user_auth_headers
        )
        assert response.status_code == 403

    def test_a_quarantined_file_is_not_found_for_its_owner(
        self, client, user_token_headers, normal_user, db_session
    ):
        f = _mk_file(db_session, normal_user, quarantined=True)
        response = client.get(f"/api/files/{f.uuid}/post-processing", headers=user_token_headers)
        assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 8. Auto-labeling: one batch row for the run, the step on each file's card
# --------------------------------------------------------------------------- #


@pytest.mark.xdist_group("local_model_redis")
def test_retroactive_auto_label_reports_each_file_and_keeps_one_batch_row(
    bridged, frames, local_model_redis, monkeypatch
):
    from app.models.topic import TopicSuggestion
    from app.tasks import auto_labeling

    owner = _mk_user(bridged)
    files = [_mk_file(bridged, owner) for _ in range(3)]
    for f in files:
        bridged.add(
            TopicSuggestion(
                media_file_id=f.id,
                user_id=owner.id,
                suggested_tags=[],
                suggested_collections=[],
                status="pending",
            )
        )
    bridged.commit()

    monkeypatch.setattr("app.core.redis.get_redis", lambda: local_model_redis)
    monkeypatch.setattr(auto_labeling, "session_scope", lambda: _yield_session(bridged))
    # The coordinator dispatches batch workers; run each one inline, in order.
    monkeypatch.setattr(
        auto_labeling.auto_label_batch_task,
        "apply_async",
        lambda args, priority: auto_labeling.auto_label_batch_task.run(*args),
    )
    try:
        result = auto_labeling.retroactive_auto_label_task.run(owner.id)
    finally:
        local_model_redis.delete(*[f"post_processing:{f.id}" for f in files])

    assert result["total"] == 3
    for f in files:
        states = [
            data["tasks"]["auto_label"]["state"]
            for _, ntype, data in frames
            if ntype == "post_processing_status" and data["file_id"] == str(f.uuid)
        ]
        assert states == ["queued", "started", "done"], f.uuid
    batch_rows = {data["file_id"] for _, ntype, data in frames if ntype == "auto_label_status"}
    assert batch_rows == {"retroactive_apply"}
    assert all(str(f.filename) not in repr(frames) for f in files)


def test_auto_label_step_reports_failed_and_propagates(bridged, frames, no_redis):
    f = _mk_file(bridged, _mk_user(bridged))
    ref = pps.resolve_file(file_id=f.id)
    assert ref is not None

    with (
        pytest.raises(ValueError, match="bad suggestion"),
        pps.reporting_step(PostProcessingTask.AUTO_LABEL, ref),
    ):
        raise ValueError("bad suggestion")

    assert _states(frames, PostProcessingTask.AUTO_LABEL) == ["started", "failed"]
