"""The LLM token limits (issue #790, D13) are enforced at the shared LLM call seam.

Drives the REAL ``LLMService`` (only the network send is stubbed) through each feature's
real wrapper against the real ``llm_token_budget`` and ``system_settings`` tables. A limit
is set the way an admin sets it (a ``system_settings`` row), so a feature path that skips
the seam reaches the stubbed provider and fails here.
"""

from __future__ import annotations

import contextlib
import json
import uuid as uuid_module
from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.organization import Organization
from app.models.prompt import SummaryPrompt
from app.models.system_settings import SystemSettings
from app.models.usage_event import UsageEvent
from app.services import llm_usage
from app.services.llm_budget import LLMBudgetExceededError
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService
from tests.llm_stream_helpers import drain

TASK_ID = "celery-task-limit-0001"
EXCEEDED = "llm_budget_exceeded"
DAILY_KEY = "llm.budget.user_daily_tokens"
MONTHLY_KEY = "llm.budget.instance_monthly_tokens"
LOCAL_KEY = "llm.budget.local_unlimited"


def _body(content: str = "ok", prompt: int = 120, completion: int = 30) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


class _StubbedService(LLMService):
    """The real service with only the network replaced; records every send."""

    body: Any = None
    sent: list[str]

    def _resolve_endpoint(self) -> str:
        return "https://api.example.invalid/v1/chat/completions"

    def _get_headers(self) -> dict[str, str]:
        return {}

    def _send_llm_request(self, *_a: Any, **_k: Any) -> Any:
        self.sent.append("send")
        return self.body if self.body is not None else _body()

    def _endpoint_session(self, url: str) -> Any:
        self.sent.append("stream")
        raise AssertionError("the provider must not be dialled")


def _service(
    body: Any = None,
    *,
    provider: LLMProvider = LLMProvider.OPENAI,
    model: str = "gpt-test",
    base_url: str = "https://api.example.invalid/v1",
) -> _StubbedService:
    svc = _StubbedService(LLMConfig(provider=provider, model=model, api_key="k", base_url=base_url))
    svc.body = body
    svc.sent = []
    return svc


class _SavepointSession:
    """The test session, but ``commit``/``rollback`` only touch one savepoint."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._savepoint = session.begin_nested()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def commit(self) -> None:
        self._savepoint.commit()

    def rollback(self) -> None:
        self._savepoint.rollback()

    def close(self) -> None:  # the test owns the session
        pass


@pytest.fixture(autouse=True)
def _own_sessions_are_the_test_session(db_session, monkeypatch):
    """The seam and the usage recorder open their own sessions; point them at the test one."""

    @contextlib.contextmanager
    def scope() -> Iterator[Any]:
        yield _SavepointSession(db_session)

    monkeypatch.setattr("app.db.session_utils.session_scope", scope)
    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _SavepointSession(db_session))
    monkeypatch.setattr(
        "app.utils.prompt_manager.SessionLocal", lambda: _SavepointSession(db_session)
    )
    monkeypatch.setattr(llm_usage, "_default_run_id", lambda: f"{TASK_ID}:0")
    if (
        db_session.query(SummaryPrompt)
        .filter(SummaryPrompt.is_system_default, SummaryPrompt.is_active)
        .first()
        is None
    ):
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


def _set(db_session, **values: Any) -> None:
    for key, value in values.items():
        row = db_session.query(SystemSettings).filter(SystemSettings.key == key).first()
        stored = str(value).lower() if isinstance(value, bool) else str(value)
        if row is None:
            db_session.add(SystemSettings(key=key, value=stored))
        else:
            row.value = stored
    db_session.commit()


@pytest.fixture
def org_file(db_session, normal_user):
    org = Organization(name=f"limits-{uuid_module.uuid4().hex[:8]}")
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
    db_session.add(
        TranscriptSegment(media_file=mf, start_time=0.0, end_time=1.0, text="call Bob", words=[])
    )
    db_session.commit()
    db_session.refresh(mf)
    return mf


# ---------------------------------------------------------------- every feature path


def _summary(mf: MediaFile, svc: LLMService) -> str | None:
    from app.tasks import summarization

    inputs = {
        "file_id": mf.id,
        "user_id": mf.user_id,
        "filename": "clip.mp4",
        "full_transcript": "SPEAKER_00: hello there\nSPEAKER_01: hi",
        "speaker_stats": {},
        "output_language": "en",
        "organization_context": "",
        "source_fingerprint": "fp",
    }
    with (
        patch.object(LLMService, "create_from_user_settings", return_value=svc),
        patch.object(summarization, "send_summary_notification") as notify,
    ):
        try:
            summarization._generate_llm_summary(inputs)
        except Exception as exc:  # the task's own handler records it; read what it would
            failure = summarization._handle_task_error(exc, str(mf.uuid), mf.id, TASK_ID)
            notified = notify.call_args.kwargs.get("extra", {}) if notify.call_args else {}
            assert notified.get("error_code") == failure.get("error_code")
            return failure.get("error_code")
    return None


def _speaker_id(mf: MediaFile, svc: LLMService) -> str | None:
    from app.tasks import speaker_identification_task as sit

    with patch.object(sit, "_create_llm_service", return_value=svc):
        predictions = sit._generate_predictions(
            mf.id,
            mf.user_id,
            "SPEAKER_00: I am Alice",
            [{"speaker": "SPEAKER_00", "text": "I am Alice"}],
            [],
        )
    assert predictions is not None
    code = predictions.get("error_code")
    return str(code) if code is not None else None


def _topics(mf: MediaFile, svc: LLMService) -> str | None:
    from app.tasks import topic_extraction as te

    class SwallowingExtraction:
        """Like the real extractor, it catches a failed call and returns None."""

        def extract_topics(self, **_: Any) -> None:
            with contextlib.suppress(Exception):
                svc.chat_completion([{"role": "user", "content": "topics please"}])

    ctx = {"file_id": mf.id, "user_id": mf.user_id, "upload_batch_id": None}
    with (
        patch("app.legal.processing_gate.refusal_for_file", return_value=None),
        patch.object(te, "_load_file_context", return_value=ctx),
        patch.object(
            te.TopicExtractionService, "create_from_settings", return_value=SwallowingExtraction()
        ),
        patch.object(te, "_record_task_start"),
        patch.object(te, "announce_started"),
        patch.object(te, "_resolve_masking", return_value=None),
        patch.object(te, "send_topic_extraction_notification"),
        patch.object(te, "_mark_task_status") as mark,
        patch("app.services.post_processing_status.resolve_file", return_value=None),
        patch("app.services.post_processing_status.report"),
    ):
        result = te.extract_topics_task.apply(args=(str(mf.uuid),), task_id=TASK_ID).result
    assert mark.call_args.kwargs.get("error_message") == result.get("error")
    error = result.get("error")
    return str(error) if error is not None else None


def _redaction(mf: MediaFile, svc: LLMService, db_session) -> str | None:
    from app.services.redaction import service as rs

    with (
        patch.object(rs.RedactionService, "_owner_wants_llm", return_value=True),
        patch.object(LLMService, "create_from_settings", return_value=svc),
    ):
        result = rs.RedactionService.detect_and_store(db_session, mf.id)
    # The detector did not examine the text: the scan completes with "llm" missing from its
    # coverage (the fail-closed gap record), never as a clean LLM pass.
    covered = str(result.get("detectors", "")).split(",")
    return EXCEEDED if result.get("status") == "done" and "llm" not in covered else None


def _chat(mf: MediaFile, svc: LLMService) -> str | None:
    svc.budget_user_id = mf.user_id
    events = drain(svc.chat_completion_stream([{"role": "user", "content": "hello"}]))
    assert len(events) == 1 and events[0].type == "error"
    return getattr(events[0], "code", None)


FEATURES = {
    "summary": lambda mf, svc, db: _summary(mf, svc),
    "speaker_id": lambda mf, svc, db: _speaker_id(mf, svc),
    "topics": lambda mf, svc, db: _topics(mf, svc),
    "redaction": _redaction,
    "chat": lambda mf, svc, db: _chat(mf, svc),
}


@pytest.mark.parametrize("feature", sorted(FEATURES))
@pytest.mark.parametrize("limit_key", [DAILY_KEY, MONTHLY_KEY])
def test_a_reached_limit_refuses_every_feature_before_the_provider(
    db_session, org_file, feature, limit_key
):
    _set(db_session, **{limit_key: 10})
    svc = _service(_body(content=json.dumps({"bluf": "x", "speaker_predictions": []})))

    code = FEATURES[feature](org_file, svc, db_session)

    assert svc.sent == [], f"{feature} reached the provider past a reached limit"
    assert code == EXCEEDED
    usage = db_session.query(UsageEvent).filter(UsageEvent.file_id == org_file.id).count()
    assert usage == 0, "a refused call spent nothing and must record nothing"


def test_an_unpriced_provider_cannot_spend_past_a_token_limit(db_session, org_file):
    """Dollars would fail open here (no rate); tokens do not."""
    _set(db_session, **{DAILY_KEY: 10})
    svc = _service(provider=LLMProvider.OPENROUTER, model="nobody/prices-this")
    svc.budget_user_id = org_file.user_id
    with pytest.raises(LLMBudgetExceededError) as refused:
        svc.chat_completion([{"role": "user", "content": "hi"}])
    assert refused.value.code == EXCEEDED
    assert svc.sent == []


def test_a_daily_limit_admits_calls_until_the_next_would_pass_it(db_session, normal_user):
    _set(db_session, **{DAILY_KEY: 400})
    svc = _service(_body(prompt=120, completion=30))
    svc.budget_user_id = normal_user.id
    message = [{"role": "user", "content": "x" * 40}]  # 10 tokens + 100 reply budget

    for _ in range(2):  # each settles at the 150 the provider reported
        svc.chat_completion(message, max_tokens=100)
    with pytest.raises(LLMBudgetExceededError) as refused:
        svc.chat_completion(message, max_tokens=100)  # 300 + 110 > 400

    assert refused.value.code == EXCEEDED
    assert refused.value.scope == "user_day"
    assert len(svc.sent) == 2
    used = db_session.execute(
        text("SELECT used FROM llm_token_budget WHERE scope = 'user_day' AND user_id = :u"),
        {"u": normal_user.id},
    ).scalar()
    assert used == 300


def test_no_limit_means_no_refusal_and_no_counter(db_session, normal_user):
    svc = _service()
    svc.budget_user_id = normal_user.id
    for _ in range(3):
        svc.chat_completion([{"role": "user", "content": "x" * 4000}], max_tokens=10_000)
    assert len(svc.sent) == 3
    rows = db_session.execute(
        text("SELECT count(*) FROM llm_token_budget WHERE user_id = :u"),
        {"u": normal_user.id},
    ).scalar()
    assert rows == 0


def _local_service(db_session, user_id: int, *, exempt: bool) -> _StubbedService:
    _set(db_session, **{DAILY_KEY: 10, LOCAL_KEY: exempt})
    svc = _service(provider=LLMProvider.VLLM, base_url="http://localhost:8000/v1")
    svc.budget_user_id = user_id
    return svc


def test_local_models_are_counted_by_default(db_session, normal_user):
    svc = _local_service(db_session, normal_user.id, exempt=False)
    with pytest.raises(LLMBudgetExceededError) as refused:
        svc.chat_completion([{"role": "user", "content": "hi"}])
    assert refused.value.code == EXCEEDED
    assert svc.sent == []


def test_an_admin_may_exempt_local_models(db_session, normal_user):
    svc = _local_service(db_session, normal_user.id, exempt=True)
    svc.chat_completion([{"role": "user", "content": "hi"}])
    assert svc.sent == ["send"]


def test_the_connection_test_is_not_refused(db_session):
    """A diagnostic must not read a reached limit as a broken connection."""
    _set(db_session, **{MONTHLY_KEY: 1})
    anthropic_body = {
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 8, "output_tokens": 2},
    }
    svc = _service(anthropic_body, provider=LLMProvider.ANTHROPIC, model="claude-haiku-4-5")
    ok, _ = svc.validate_connection()
    assert ok is True
    assert svc.sent == ["send"]


def test_a_system_service_counts_against_the_file_owner(db_session, org_file):
    """No per-user factory: the open run's file owner is whose daily limit applies."""
    _set(db_session, **{DAILY_KEY: 10})
    svc = _service()
    assert svc.budget_user_id is None
    with pytest.raises(LLMBudgetExceededError) as at_exit:
        with llm_usage.llm_usage_run("summary", file_id=org_file.id):
            with pytest.raises(LLMBudgetExceededError) as refused:
                svc.chat_completion([{"role": "user", "content": "hi"}])
    assert refused.value.scope == "user_day"
    # A feature that swallowed the refused call still ends refused when its run closes.
    assert at_exit.value is refused.value


# ---------------------------------------------------------------- periods and seeding


def test_the_day_and_month_roll_over(db_session, normal_user):
    from app.services import llm_budget

    _set(db_session, **{DAILY_KEY: 100, MONTHLY_KEY: 150})
    last_day = datetime(2031, 1, 31, 23, 59, tzinfo=UTC)
    next_day = datetime(2031, 2, 1, 0, 0, 1, tzinfo=UTC)

    llm_budget.reserve(user_id=normal_user.id, amount=90, now=last_day)
    with pytest.raises(llm_budget.LLMBudgetExceededError) as day_full:
        llm_budget.reserve(user_id=normal_user.id, amount=20, now=last_day)
    assert day_full.value.scope == "user_day"
    assert day_full.value.retry_after_seconds == 60

    llm_budget.reserve(user_id=normal_user.id, amount=20, now=next_day)  # new day, new month
    llm_budget.reserve(user_id=None, amount=100, now=next_day)
    with pytest.raises(llm_budget.LLMBudgetExceededError) as month_full:
        llm_budget.reserve(user_id=None, amount=40, now=next_day)
    assert month_full.value.scope == "instance_month"


def test_a_limit_set_mid_day_counts_the_day_so_far(db_session, normal_user):
    from app.services import llm_budget

    db_session.add(
        UsageEvent(
            user_id=normal_user.id,
            event_type="summary.tokens",
            quantity=900,
            unit="tokens",
            created_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    _set(db_session, **{DAILY_KEY: 1000})
    with pytest.raises(llm_budget.LLMBudgetExceededError):
        llm_budget.reserve(user_id=normal_user.id, amount=200)
    llm_budget.reserve(user_id=normal_user.id, amount=100)


def test_settling_moves_the_reservation_to_the_measured_count(db_session):
    from app.services import llm_budget

    _set(db_session, **{MONTHLY_KEY: 10_000})
    now = datetime(2032, 3, 3, tzinfo=UTC)
    held = llm_budget.reserve(user_id=None, amount=5000, now=now)
    llm_budget.settle(held, 1200)
    used = db_session.execute(
        text(
            "SELECT used FROM llm_token_budget WHERE scope = 'instance_month' AND period = '2032-03'"
        )
    ).scalar()
    assert used == 1200


def test_unreadable_limits_refuse_with_their_own_code(db_session):
    from app.services import llm_budget

    with (
        patch.object(llm_budget, "read_limits", side_effect=RuntimeError("db down")),
        pytest.raises(llm_budget.LLMBudgetExceededError) as refused,
    ):
        llm_budget.reserve(user_id=None, amount=1)
    assert refused.value.code == llm_budget.CODE_UNAVAILABLE


def test_the_chat_event_type_is_the_one_chat_records():
    from app.services import llm_budget
    from app.services.chat.usage import EVENT_TYPE_CHAT_TOKENS

    assert EVENT_TYPE_CHAT_TOKENS in llm_budget.TOKEN_EVENT_TYPES
    assert set(llm_usage.STAGE_EVENT_TYPES.values()) <= set(llm_budget.TOKEN_EVENT_TYPES)
