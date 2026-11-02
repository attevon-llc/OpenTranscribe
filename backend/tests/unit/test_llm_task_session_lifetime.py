# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (a fake session, a recording Path) to
# signatures that declare Session/Path. Declared once here rather than as a cast at
# every call site: a cast buries the thing being asserted, and widening a production
# signature to suit a test is worse. Same convention as
# tests/unit/test_proxy_identity_consistency.py.
"""LLM tasks (summary, speaker identification, topic extraction) hold no DB transaction across the provider call.

See ``tests/unit/_session_lifetime_trackers.py`` for the depth-tracking scope stand-in
and the measured incident behind this family of tests.
"""

import uuid as uuid_mod
from types import SimpleNamespace

import pytest
from celery.exceptions import Retry

from app.models.media import MediaFile
from app.models.media import Task as TaskModel
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.topic import TopicSuggestion
from app.tasks import speaker_identification_task as _sid_module
from tests.unit._session_lifetime_trackers import ScopeTracker
from tests.unit._session_lifetime_trackers import leak
from tests.unit._session_lifetime_trackers import make_transcribed_file

# Captured before any fixture swaps it for a fake.
_REAL_CREATE_LLM_SERVICE = _sid_module._create_llm_service


# --------------------------------------------------------------------------- #
# 6. ai.generate_summary — an LLM completion over the WHOLE transcript
#
#    This is one of the two that were found holding a live transaction: the NLP
#    worker sat idle-in-transaction for 1 h 26 m with the transcript_segment
#    SELECT below as its last statement.
# --------------------------------------------------------------------------- #
class _FakeLLMConfig:
    provider = "fake"
    model = "fake-1"


class _FakeLLMService:
    """Stands in for the provider client. Records what it was actually SENT."""

    user_context_window = 32768
    config = _FakeLLMConfig()

    def __init__(self, tracker, recorded, *, boom: bool = False):
        self._tracker = tracker
        self._recorded = recorded
        self._boom = boom
        self.closed = False

    def generate_summary(self, **kwargs):
        # A multi-minute HTTP round trip against an external provider.
        self._tracker.observe("llm_generate_summary")
        self._recorded["summary_kwargs"] = kwargs
        if self._boom:
            raise RuntimeError("provider returned 502")
        return {"bluf": "They agreed.", "brief_summary": "A meeting.", "metadata": {}}

    def identify_speakers(self, **kwargs):
        self._tracker.observe("llm_identify_speakers")
        self._recorded["identify_kwargs"] = kwargs
        if self._boom:
            raise RuntimeError("provider returned 502")
        return {
            "speaker_predictions": [
                {"speaker_label": "SPEAKER_00", "predicted_name": "Ada", "confidence": 0.9},
                {"speaker_label": "SPEAKER_01", "predicted_name": "Grace", "confidence": 0.2},
            ],
            "overall_confidence": "medium",
        }

    def close(self):
        self.closed = True


@pytest.fixture
def summarization_env(db_session, monkeypatch):
    """Patch summarization's LLM and notification seams; keep the DB real.

    There is no OpenSearch seam to patch any more: the task writes
    ``media_file.summary_data`` and nothing else since ``transcript_summaries``
    was retired (#67).
    """
    from app.tasks import summarization as summ

    tracker = ScopeTracker(db_session)
    recorded: dict = {}
    monkeypatch.setattr(summ, "session_scope", tracker.scope)
    monkeypatch.setattr("app.tasks.summarization_inputs.session_scope", tracker.scope)
    monkeypatch.setattr("app.tasks.summarization_inputs.resolve_llm_masking", lambda *a: None)
    monkeypatch.setattr(
        summ,
        "send_summary_notification",
        lambda *a, **kw: recorded.setdefault("notifications", []).append(a),
    )
    service = _FakeLLMService(tracker, recorded)

    class _Factory:
        @staticmethod
        def create_from_user_settings(user_id):
            recorded["llm_user_id"] = user_id
            return service

        @staticmethod
        def create_from_system_settings():
            return service

    monkeypatch.setattr(summ, "LLMService", _Factory)

    tracker.recorded = recorded
    tracker.llm_service = service  # type: ignore[attr-defined]
    return tracker


def test_summarization_calls_the_llm_outside_the_session(
    db_session, normal_user, summarization_env
):
    """The regression: the provider round trip must run with zero scopes open."""
    from app.tasks import summarization as summ

    tracker = summarization_env
    media_file, _ = make_transcribed_file(db_session, normal_user)

    result = summ.summarize_transcript_task.apply(args=[str(media_file.uuid)]).get()

    assert result["status"] == "success", result

    observed = tracker.seen
    assert observed["llm_generate_summary"] == 0, leak(tracker, "llm_generate_summary")

    assert tracker.opened >= 2, f"expected a read scope and a write scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0

    # The read phase really did produce the transcript the provider was sent,
    # so "zero scopes open" cannot be satisfied by sending nothing.
    sent = tracker.recorded["summary_kwargs"]
    assert "hello there" in sent["transcript"]
    assert sent["user_id"] == normal_user.id
    assert tracker.llm_service.closed is True

    # And the result landed in Postgres.
    db_session.expire_all()
    refreshed = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).first()
    assert refreshed.summary_status == "completed"
    assert refreshed.summary_data["bluf"] == "They agreed."


def test_summarization_read_phase_returns_plain_data(db_session, normal_user, summarization_env):
    """``_load_summarization_inputs`` must not hand back live ORM instances."""
    from app.tasks import summarization as summ

    tracker = summarization_env
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)

    inputs = summ._load_summarization_inputs(
        str(media_file.uuid), str(uuid_mod.uuid4()), force_regenerate=False
    )

    assert tracker.depth == 0
    assert inputs["file_id"] == media_file.id
    assert inputs["user_id"] == normal_user.id
    assert isinstance(inputs["full_transcript"], str) and inputs["full_transcript"]
    assert isinstance(inputs["speaker_stats"], dict) and inputs["speaker_stats"]
    for value in inputs.values():
        assert not isinstance(value, (MediaFile, Speaker, TranscriptSegment))


def test_summarization_releases_the_scope_when_the_provider_fails(
    db_session, normal_user, summarization_env, monkeypatch
):
    """A provider failure must not leave a scope open — and must mark the file failed."""
    from app.tasks import summarization as summ

    tracker = summarization_env
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)
    tracker.llm_service._boom = True  # type: ignore[attr-defined]

    result = summ.summarize_transcript_task.apply(args=[str(media_file.uuid)]).get()

    assert result["status"] == "error", result
    assert tracker.seen["llm_generate_summary"] == 0, leak(tracker, "llm_generate_summary")
    assert tracker.depth == 0, "a session scope survived the failure"

    db_session.expire_all()
    refreshed = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).first()
    assert refreshed.summary_status == "failed"


# --------------------------------------------------------------------------- #
# 7. ai.identify_speakers — the other one that was actively wedging the DB
# --------------------------------------------------------------------------- #
@pytest.fixture
def identification_env(db_session, monkeypatch):
    from app.tasks import speaker_identification_task as sid

    tracker = ScopeTracker(db_session)
    recorded: dict = {}

    monkeypatch.setattr(sid, "session_scope", tracker.scope)
    monkeypatch.setattr(sid, "resolve_llm_masking", lambda db, media_file: None)
    # The post-processing status report resolves the file and the quarantine check
    # through their OWN session_scope() on a real connection, which cannot see this
    # test's savepoint-isolated MediaFile — stub the report rather than bridge a second
    # session, keeping this suite focused on scope depth.
    monkeypatch.setattr("app.services.post_processing_status.report", lambda *a, **kw: False)
    monkeypatch.setattr(
        "app.services.takedown_service.is_notification_suppressed",
        lambda *a, **kw: False,
    )

    service = _FakeLLMService(tracker, recorded)
    monkeypatch.setattr(sid, "_create_llm_service", lambda user_id: service)

    tracker.recorded = recorded
    tracker.llm_service = service  # type: ignore[attr-defined]
    return tracker


def test_speaker_identification_calls_the_llm_outside_the_session(
    db_session, normal_user, identification_env
):
    from app.tasks import speaker_identification_task as sid

    tracker = identification_env
    media_file, speakers = make_transcribed_file(db_session, normal_user)

    result = sid.identify_speakers_llm_task.apply(args=[str(media_file.uuid)]).get()

    assert result["status"] == "success", result
    assert result["predictions_count"] == 2

    assert tracker.seen["llm_identify_speakers"] == 0, leak(tracker, "llm_identify_speakers")
    assert tracker.opened >= 2, f"expected a read scope and a write scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0

    # The read phase produced real content for the provider.
    sent = tracker.recorded["identify_kwargs"]
    assert "hello there" in sent["transcript"]
    assert len(sent["speaker_segments"]) == 4
    assert tracker.llm_service.closed is True

    # ...and the confident suggestion was written back, the weak one skipped.
    db_session.expire_all()
    rows = {
        s.name: s.suggested_name
        for s in db_session.query(Speaker).filter(Speaker.media_file_id == media_file.id).all()
    }
    assert rows[speakers[0].name] == "Ada"
    assert rows[speakers[1].name] is None  # confidence 0.2 < 0.5


def test_speaker_identification_read_phase_returns_plain_data(
    db_session, normal_user, identification_env
):
    from app.tasks import speaker_identification_task as sid

    tracker = identification_env
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)

    inputs = sid._load_identification_inputs(str(media_file.uuid), str(uuid_mod.uuid4()))

    assert tracker.depth == 0
    assert inputs["file_id"] == media_file.id
    assert isinstance(inputs["full_transcript"], str) and inputs["full_transcript"]
    assert all(isinstance(seg, dict) for seg in inputs["speaker_segments"])
    for value in inputs.values():
        assert not isinstance(value, (MediaFile, Speaker, TranscriptSegment))


def test_speaker_identification_skips_quietly_without_an_llm(
    db_session, normal_user, identification_env, monkeypatch, caplog
):
    """No provider configured is a deployment choice, not a failure (issue #1004).

    It must skip the way ai.extract_topics does — no ERROR, no traceback — and still
    close out the task record so the file's enrichment doesn't look stuck.
    """
    from app.tasks import speaker_identification_task as sid

    # Undo the fixture's fake so the REAL resolution runs, with no provider behind it.
    monkeypatch.setattr(sid, "_create_llm_service", _REAL_CREATE_LLM_SERVICE)
    monkeypatch.setattr(sid.LLMService, "create_from_user_settings", lambda user_id: None)
    media_file, _ = make_transcribed_file(db_session, normal_user)

    with caplog.at_level("DEBUG"):
        result = sid.identify_speakers_llm_task.apply(args=[str(media_file.uuid)]).get()

    assert result["status"] == "skipped", result
    assert result["reason"] == "LLM not configured"
    errors = [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")]
    assert errors == [], [r.getMessage() for r in errors]
    assert not any(r.exc_info for r in caplog.records), "a traceback was logged"
    assert identification_env.depth == 0

    db_session.expire_all()
    task_row = db_session.query(TaskModel).filter(TaskModel.media_file_id == media_file.id).one()
    assert task_row.status == "completed"
    assert task_row.error_message is None


# --------------------------------------------------------------------------- #
# 13. ai.extract_topics — the SEVENTH LLM-in-transaction instance
#
#     Same shape as ai.generate_summary, and it could not be fixed from
#     app/tasks alone: ``TopicExtractionService`` was constructed with ``db=``
#     and both READ the transcript and WROTE the ``TopicSuggestion`` row through
#     it, so one ``session_scope`` in the task spanned the whole provider round
#     trip. The split therefore lives in the SERVICE, and this fixture patches
#     ``session_scope`` in BOTH modules so a scope opened at either end is seen.
# --------------------------------------------------------------------------- #
_TOPIC_LLM_JSON = """<thinking>ok</thinking>
<answer>
{"suggested_collections": [{"name": "Team Standups", "confidence": 0.9, "rationale": "r"}],
 "suggested_tags": [{"name": "budget", "confidence": 0.9, "rationale": "r"},
                    {"name": "hiring", "confidence": 0.8, "rationale": "r"}]}
</answer>"""


class _FakeTopicLLMService:
    """Stands in for the provider client. Records the prompt it was actually SENT."""

    user_context_window = 32768
    config = _FakeLLMConfig()

    def __init__(self, tracker, recorded, *, boom: bool = False):
        self._tracker = tracker
        self._recorded = recorded
        self._boom = boom

    def chat_completion(self, messages, **kwargs):
        # A multi-minute HTTP round trip against an external provider.
        self._tracker.observe("llm_chat_completion")
        self._recorded["messages"] = messages
        if self._boom:
            raise RuntimeError("provider returned 502")
        return SimpleNamespace(content=_TOPIC_LLM_JSON)


@pytest.fixture
def topic_extraction_env(db_session, monkeypatch):
    """Patch topic extraction's LLM/notification seams; keep the DB real."""
    from app.services import topic_extraction_service as tes
    from app.tasks import topic_extraction as tex

    tracker = ScopeTracker(db_session)
    recorded: dict = {}

    # BOTH ends: the task's identity/masking reads and the service's read+write
    # phases. Patching only the task would let a service-side scope go unseen.
    monkeypatch.setattr(tex, "session_scope", tracker.scope)
    monkeypatch.setattr(tes, "session_scope", tracker.scope)
    monkeypatch.setattr(tex, "resolve_llm_masking", lambda db, media_file: None)
    monkeypatch.setattr(
        tex,
        "send_topic_extraction_notification",
        lambda **kw: recorded.setdefault("notifications", []).append(kw),
    )

    service = _FakeTopicLLMService(tracker, recorded)

    class _Factory:
        @staticmethod
        def create_from_settings(user_id=None):
            recorded["llm_user_id"] = user_id
            return service

    monkeypatch.setattr(tes, "LLMService", _Factory)

    tracker.recorded = recorded
    tracker.llm_service = service  # type: ignore[attr-defined]
    return tracker


def test_topic_extraction_calls_the_llm_outside_the_session(
    db_session, normal_user, topic_extraction_env
):
    """The regression: the provider round trip must run with zero scopes open."""
    from app.tasks import topic_extraction as tex

    tracker = topic_extraction_env
    media_file, _ = make_transcribed_file(db_session, normal_user)

    result = tex.extract_topics_task.apply(args=[str(media_file.uuid)]).get()

    assert result["status"] == "completed", result
    assert result["tag_count"] == 2
    assert result["collection_count"] == 1

    assert tracker.seen["llm_chat_completion"] == 0, leak(tracker, "llm_chat_completion")
    assert tracker.opened >= 2, f"expected a read scope and a write scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0

    # The read phase really did produce the transcript the provider was sent, so
    # "zero scopes open" cannot be satisfied by sending nothing.
    prompt = tracker.recorded["messages"][-1]["content"]
    assert "hello" in prompt
    assert tracker.recorded["llm_user_id"] == normal_user.id

    # ...and the suggestion landed in Postgres.
    db_session.expire_all()
    stored = (
        db_session.query(TopicSuggestion)
        .filter(TopicSuggestion.media_file_id == media_file.id)
        .first()
    )
    assert stored is not None
    assert str(stored.uuid) == result["suggestion_id"]
    assert {t["name"] for t in stored.suggested_tags} == {"budget", "hiring"}

    # A run that did the work is tracked, and its row is closed (issue #1017 moved
    # the record creation behind the provider check; this pins that it still runs).
    task_row = (
        db_session.query(TaskModel)
        .filter(
            TaskModel.media_file_id == media_file.id,
            TaskModel.task_type == "topic_extraction",
        )
        .one()
    )
    assert task_row.status == "completed"


def test_topic_extraction_read_phase_returns_plain_data(
    db_session, normal_user, topic_extraction_env
):
    """``_load_extraction_inputs`` must not hand back live ORM instances."""
    from app.services.topic_extraction_service import TopicExtractionService

    tracker = topic_extraction_env
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)

    inputs = TopicExtractionService()._load_extraction_inputs(
        int(media_file.id), force_regenerate=False, redaction_cfg=None
    )

    assert tracker.depth == 0, "the read scope was still open on return"
    assert tracker.opened == 1
    assert inputs is not None
    assert inputs["user_id"] == normal_user.id
    assert isinstance(inputs["transcript"], str) and "hello there" in inputs["transcript"]
    for value in inputs.values():
        assert not isinstance(value, (MediaFile, Speaker, TranscriptSegment, TopicSuggestion))


def test_topic_extraction_releases_the_scope_when_the_provider_fails(
    db_session, normal_user, topic_extraction_env
):
    """A provider failure must not leave a scope open — and must report failure."""
    from app.tasks import topic_extraction as tex

    tracker = topic_extraction_env
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)
    tracker.llm_service._boom = True  # type: ignore[attr-defined]

    result = tex.extract_topics_task.apply(args=[str(media_file.uuid)]).get()

    assert result["status"] == "failed", result
    assert tracker.seen["llm_chat_completion"] == 0, leak(tracker, "llm_chat_completion")
    assert tracker.depth == 0, "a session scope survived the failure"
    assert tracker.opened >= 1


def test_topic_extraction_defers_for_redaction_with_no_session_open(
    db_session, normal_user, topic_extraction_env, monkeypatch
):
    """``defer_for_redaction`` dispatches a Celery task — never inside a transaction."""
    from app.services.redaction.llm_guard import RedactionNotReadyError
    from app.tasks import topic_extraction as tex

    tracker = topic_extraction_env
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)

    def _not_ready(db, mf):
        raise RedactionNotReadyError("spans pending", retryable=True, file_id=int(mf.id))

    monkeypatch.setattr(tex, "resolve_llm_masking", _not_ready)

    deferred: list[int] = []

    def _defer(task, exc, **kw):
        deferred.append(tracker.depth)
        raise Retry()

    monkeypatch.setattr(tex, "defer_for_redaction", _defer)

    with pytest.raises(Retry):
        tex.extract_topics_task.apply(args=[str(media_file.uuid)], throw=True).get()

    assert deferred == [0], f"deferral ran with {deferred} scope(s) open"
    assert tracker.depth == 0
