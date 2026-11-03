"""Every LLM-backed task run records exactly one usage_event row.

Drives the REAL ``LLMService.chat_completion`` (only the HTTP send is stubbed) inside
each task's real wrapper, against the real ``usage_event`` table. The rows are
read back from Postgres, so a test here fails if the row is missing, doubled,
mis-tenanted or mis-keyed.
"""

from __future__ import annotations

import contextlib
import json
import uuid as uuid_module
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.prompt import SummaryPrompt
from app.models.usage_event import UsageEvent
from app.services import llm_usage
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService

_REAL_DEFAULT_RUN_ID = llm_usage._default_run_id
TASK_ID = "celery-task-0001"
RUN_ID = f"{TASK_ID}:0"  # task id + attempt (request.retries)


def _body(content: str = "ok", prompt: int = 120, completion: int = 30, usage: bool = True):
    data: dict[str, Any] = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
    if usage:
        data["usage"] = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }
    return data


_URL = "https://api.example.invalid/v1/chat/completions"


class _StubbedService(LLMService):
    """The real service with only the network send replaced."""

    body: Any = None
    side_effect: Exception | None = None

    def _resolve_endpoint(self) -> str:
        return _URL

    def _get_headers(self) -> dict[str, str]:
        return {}

    def _send_llm_request(self, *_a: Any, **_k: Any) -> Any:
        if self.side_effect is not None:
            raise self.side_effect
        return self.body if self.body is not None else _body()


def _service(body: Any = None, side_effect: Exception | None = None) -> LLMService:
    svc = _StubbedService(
        LLMConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-test",
            api_key="k",
            base_url="https://api.example.invalid/v1",
        )
    )
    svc.body = body
    svc.side_effect = side_effect
    return svc


@pytest.fixture
def org_file(db_session, normal_user):
    org = Organization(name=f"usage-{uuid_module.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    mf = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=normal_user.id,
        organization_id=org.id,
        filename="clip.mp4",
        storage_path=f"user_{normal_user.id}/{uuid_module.uuid4().hex}.mp4",
        file_size=1024,
        content_type="video/mp4",
        status=FileStatus.COMPLETED,
        duration=60.0,
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


@pytest.fixture(autouse=True)
def _system_default_summary_prompt(db_session, monkeypatch):
    """Guarantee the system default summary prompt the summary task resolves.

    Production seeds it at startup (``initial_data``); a database that only ran
    ``alembic upgrade head`` (CI) has none, and the summary task then raises before it
    reaches the LLM. A seeded database already has one, so only create it when absent.
    The row is rolled back with the test, so ``prompt_manager`` (which opens its own
    ``SessionLocal`` when handed no session) is pointed at the test session, otherwise it
    could not see a row this test has not committed to the real database.
    """
    exists = (
        db_session.query(SummaryPrompt)
        .filter(SummaryPrompt.is_system_default, SummaryPrompt.is_active)
        .first()
    )
    if exists is None:
        db_session.add(
            SummaryPrompt(
                name="Universal Summary (test)",
                prompt_text="Summarize the transcript.",
                is_system_default=True,
                is_active=True,
                content_type="general",
                user_id=None,
            )
        )
        db_session.commit()

    class _NonClosing:
        def __init__(self) -> None:
            self._session = db_session

        def __getattr__(self, name: str) -> Any:
            return getattr(self._session, name)

        def close(self) -> None:  # the caller closes a session it opened; this one is ours
            pass

    monkeypatch.setattr("app.utils.prompt_manager.SessionLocal", _NonClosing)


class _SavepointSession:
    """The test session, but ``commit``/``rollback`` only touch one savepoint.

    ``record_event`` rolls back on a duplicate key; on the shared test session that would
    also discard the rows the test seeded, which production's dedicated session never does.
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        self._savepoint = session.begin_nested()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def commit(self) -> None:
        self._savepoint.commit()

    def rollback(self) -> None:
        self._savepoint.rollback()


@pytest.fixture(autouse=True)
def _own_session_is_the_test_session(db_session, monkeypatch):
    """``llm_usage`` opens its own session; point it at the rolled-back test one."""

    @contextlib.contextmanager
    def scope() -> Iterator[Any]:
        yield _SavepointSession(db_session)

    monkeypatch.setattr("app.db.session_utils.session_scope", scope)
    monkeypatch.setattr(llm_usage, "_default_run_id", lambda: RUN_ID)


def _meta(row: UsageEvent) -> dict[str, Any]:
    assert row.event_metadata is not None
    return row.event_metadata


def _rows(db_session, mf: MediaFile, event_type: str) -> list[UsageEvent]:
    query = db_session.query(UsageEvent).filter(
        UsageEvent.file_id == mf.id, UsageEvent.event_type == event_type
    )
    return list(query.all())


def _assert_one_row(db_session, mf, event_type, *, prompt=120, completion=30):
    rows = _rows(db_session, mf, event_type)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == mf.user_id
    assert row.organization_id == mf.organization_id
    assert row.unit == "tokens"
    assert int(row.quantity) == prompt + completion
    meta = _meta(row)
    assert meta["prompt_tokens"] == prompt
    assert meta["completion_tokens"] == completion
    assert meta["model"] == "gpt-test"
    assert meta["provider"] == "openai"
    assert meta["tokens_estimated"] is False
    assert row.idempotency_key == f"llm:{meta['stage']}:{mf.id}:{RUN_ID}"
    return row


# ---------------------------------------------------------------- summarization


def _summary_inputs(mf: MediaFile) -> dict[str, Any]:
    return {
        "file_id": mf.id,
        "user_id": mf.user_id,
        "filename": "clip.mp4",
        "full_transcript": "SPEAKER_00: hello there\nSPEAKER_01: hi",
        "speaker_stats": {},
        "output_language": "en",
        "organization_context": "",
        "source_fingerprint": "fp",
    }


def _run_summary(mf: MediaFile, svc: LLMService):
    from app.tasks import summarization

    with patch.object(LLMService, "create_from_user_settings", return_value=svc):
        return summarization._generate_llm_summary(_summary_inputs(mf))


def test_summary_run_records_one_row(db_session, org_file):
    _run_summary(org_file, _service(_body(content=json.dumps({"bluf": "x"}))))
    row = _assert_one_row(db_session, org_file, "summary.tokens")
    assert row.event_type == "summary.tokens"


def test_summary_redelivery_does_not_double_count(db_session, org_file):
    for _ in range(2):  # same task id delivered twice
        _run_summary(org_file, _service(_body(content=json.dumps({"bluf": "x"}))))
    assert len(_rows(db_session, org_file, "summary.tokens")) == 1


def test_summary_failure_still_records_spent_tokens(db_session, org_file):
    svc = _service(side_effect=RuntimeError("boom"))
    with pytest.raises(Exception, match="LLM summarization failed"):
        _run_summary(org_file, svc)
    rows = _rows(db_session, org_file, "summary.tokens")
    assert len(rows) == 1
    assert _meta(rows[0])["success"] is False
    assert _meta(rows[0])["tokens_estimated"] is True  # no usage came back


# ---------------------------------------------------------------- speaker id


def _run_speaker(mf: MediaFile, svc: LLMService):
    from app.tasks import speaker_identification_task as sit

    with patch.object(sit, "_create_llm_service", return_value=svc):
        return sit._generate_predictions(
            mf.id,
            mf.user_id,
            "SPEAKER_00: I am Alice",
            [{"speaker": "SPEAKER_00", "text": "I am Alice"}],
            [],
        )


def test_speaker_id_run_records_one_row(db_session, org_file):
    _run_speaker(org_file, _service(_body(content=json.dumps({"speaker_predictions": []}))))
    row = _assert_one_row(db_session, org_file, "speaker_id.tokens")
    assert row.event_type == "speaker_id.tokens"


def test_speaker_id_redelivery_does_not_double_count(db_session, org_file):
    for _ in range(2):
        _run_speaker(org_file, _service(_body(content=json.dumps({"speaker_predictions": []}))))
    assert len(_rows(db_session, org_file, "speaker_id.tokens")) == 1


def test_speaker_id_failure_still_records(db_session, org_file):
    _run_speaker(org_file, _service(side_effect=RuntimeError("boom")))
    rows = _rows(db_session, org_file, "speaker_id.tokens")
    assert len(rows) == 1
    assert _meta(rows[0])["success"] is False


# ---------------------------------------------------------------- topics


def _run_topics(mf: MediaFile, svc: LLMService):
    from app.tasks import topic_extraction as te

    class FakeExtraction:
        """Stands in for the DB-heavy extractor; makes one real provider call."""

        def extract_topics(self, **_: Any):
            svc.chat_completion([{"role": "user", "content": "topics please"}])
            return None  # task reports "failed" - the run must still be recorded

    ctx = {"file_id": mf.id, "user_id": mf.user_id, "upload_batch_id": None}
    with (
        patch.object(te, "refusal_for_file", create=True, return_value=None),
        patch("app.legal.processing_gate.refusal_for_file", return_value=None),
        patch.object(te, "_load_file_context", return_value=ctx),
        patch.object(
            te.TopicExtractionService, "create_from_settings", return_value=FakeExtraction()
        ),
        patch.object(te, "_record_task_start"),
        patch.object(te, "announce_started"),
        patch.object(te, "_resolve_masking", return_value=None),
        patch.object(te, "send_topic_extraction_notification"),
        patch.object(te, "_mark_task_status"),
        patch("app.services.post_processing_status.resolve_file", return_value=None),
        patch("app.services.post_processing_status.report"),
    ):
        te.extract_topics_task.apply(args=(str(mf.uuid),), task_id=TASK_ID)


def test_topics_run_records_one_row(db_session, org_file):
    _run_topics(org_file, _service())
    row = _assert_one_row(db_session, org_file, "topics.tokens")
    assert row.event_type == "topics.tokens"


def test_topics_redelivery_does_not_double_count(db_session, org_file):
    _run_topics(org_file, _service())
    _run_topics(org_file, _service())
    assert len(_rows(db_session, org_file, "topics.tokens")) == 1


# ---------------------------------------------------------------- redaction


def _run_redaction_detector(db_session, mf: MediaFile, svc: LLMService):
    from app.services.redaction import service as rs

    with (
        patch.object(rs.RedactionService, "_owner_wants_llm", return_value=True),
        patch.object(LLMService, "create_from_settings", return_value=svc),
    ):
        rs.RedactionService.detect_and_store(db_session, mf.id)


def test_redaction_llm_detector_records_one_row(db_session, org_file):
    from app.models.media import TranscriptSegment

    db_session.add(
        TranscriptSegment(
            media_file_id=org_file.id, start_time=0.0, end_time=1.0, text="call Bob", words=[]
        )
    )
    db_session.commit()
    _run_redaction_detector(db_session, org_file, _service(_body(content="[]")))
    row = _assert_one_row(db_session, org_file, "redaction.tokens")
    assert row.event_type == "redaction.tokens"


# ---------------------------------------------------------------- the helper itself


def test_provider_without_usage_is_estimated_and_flagged(db_session, org_file):
    svc = _service(_body(content="x" * 400, usage=False))
    with llm_usage.llm_usage_run("summary", file_id=org_file.id):
        svc.chat_completion([{"role": "user", "content": "y" * 800}])
    row = _rows(db_session, org_file, "summary.tokens")[0]
    assert _meta(row)["tokens_estimated"] is True
    assert _meta(row)["prompt_tokens"] == 200
    assert _meta(row)["completion_tokens"] == 100


def test_multi_call_run_is_one_summed_row(db_session, org_file):
    svc = _service()
    with llm_usage.llm_usage_run("summary", file_id=org_file.id):
        svc.chat_completion([{"role": "user", "content": "a"}])
        svc.chat_completion([{"role": "user", "content": "b"}])
    row = _rows(db_session, org_file, "summary.tokens")[0]
    assert int(row.quantity) == 300
    assert _meta(row)["llm_calls"] == 2


def test_run_without_a_call_writes_nothing(db_session, org_file):
    with llm_usage.llm_usage_run("summary", file_id=org_file.id):
        pass
    assert _rows(db_session, org_file, "summary.tokens") == []


def test_chat_completion_outside_a_run_is_untouched(db_session, org_file):
    _service().chat_completion([{"role": "user", "content": "a"}])
    assert db_session.query(UsageEvent).filter(UsageEvent.file_id == org_file.id).count() == 0


def test_recording_failure_never_fails_the_task(org_file, caplog):
    with (
        patch("app.services.usage_service.record_event", side_effect=RuntimeError("db down")),
        caplog.at_level("ERROR"),
    ):
        with llm_usage.llm_usage_run("summary", file_id=org_file.id):
            result = _service().chat_completion([{"role": "user", "content": "a"}])
    assert result.content == "ok"
    assert "Failed to record summary LLM usage" in caplog.text


def test_bedrock_split_is_recorded(db_session, org_file):
    cfg = LLMConfig(provider=LLMProvider.BEDROCK, model="anthropic.test", api_key=None)
    svc = LLMService(cfg)

    def fake_converse(**kw):
        kw["usage_out"].update(prompt_tokens=11, completion_tokens=7)
        return "hi", 18, "end_turn"

    with (
        patch("app.services.llm_bedrock.converse", side_effect=fake_converse),
        llm_usage.llm_usage_run("topics", file_id=org_file.id),
    ):
        svc.chat_completion([{"role": "user", "content": "a"}])
    row = _rows(db_session, org_file, "topics.tokens")[0]
    assert (_meta(row)["prompt_tokens"], _meta(row)["completion_tokens"]) == (11, 7)


def test_unknown_stage_is_rejected():
    with pytest.raises(ValueError, match="Unknown LLM usage stage"):
        with llm_usage.llm_usage_run("nope", file_id=None):
            pass


def test_default_run_identity_is_task_id_plus_attempt():
    from types import SimpleNamespace

    for retries, expected in ((0, f"{TASK_ID}:0"), (2, f"{TASK_ID}:2")):
        task = SimpleNamespace(request=SimpleNamespace(id=TASK_ID, retries=retries))
        with patch("celery.current_task", task):
            assert _REAL_DEFAULT_RUN_ID() == expected


def test_retry_that_spends_tokens_is_its_own_row_but_redelivery_is_not(db_session, org_file):
    def attempt(n: int) -> None:
        with llm_usage.llm_usage_run("summary", file_id=org_file.id, run_id=f"{TASK_ID}:{n}"):
            _service().chat_completion([{"role": "user", "content": "a"}])

    attempt(0)
    attempt(1)  # Celery retry: real spend, second row
    attempt(1)  # redelivery of attempt 1: still the same row
    rows = _rows(db_session, org_file, "summary.tokens")
    assert sorted(str(r.idempotency_key) for r in rows) == [
        f"llm:summary:{org_file.id}:{TASK_ID}:0",
        f"llm:summary:{org_file.id}:{TASK_ID}:1",
    ]
