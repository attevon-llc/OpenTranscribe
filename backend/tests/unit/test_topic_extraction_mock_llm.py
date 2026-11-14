"""Topic extraction, end to end, against the mock LLM's ``mock-topics-json`` scenario.

``TopicExtractionService.extract_topics`` is run for real: the read phase, the real ``LLMService``
sending the real prompt over HTTP, the response parser, and the write phase into Postgres. Only the
model's answer is canned, so the stored ``TopicSuggestion`` row is whatever the app made of it.

Wiring only: the mock's reply is a constant, so none of this measures suggestion quality.

No GPU and no container (``mock_llm_url`` starts a subprocess). DB-backed, so run through
``scripts/run-backend-tests.sh --throwaway-db``.
"""

from __future__ import annotations

import hashlib
import uuid as uuid_mod
from types import SimpleNamespace

import pytest
import requests
from urllib3.util import Retry

from app.core.config import settings
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.topic import TopicSuggestion
from app.services.llm_service import LLMService
from app.services.llm_types import LLMConfig
from app.services.llm_types import LLMProvider
from app.services.topic_extraction_service import TopicExtractionService
from tests.unit._session_lifetime_trackers import ScopeTracker

pytestmark = pytest.mark.unit

TRANSCRIPT_LINES = (
    "We reviewed the quarterly budget and agreed to move the launch date.",
    "Hiring for the platform team starts next month.",
)


@pytest.fixture(autouse=True)
def _allow_the_loopback_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSRF guard refuses a private address, and the mock is always on loopback."""
    monkeypatch.setattr(settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", True)


@pytest.fixture
def extraction(db_session, normal_user, monkeypatch: pytest.MonkeyPatch, mock_llm_url: str):
    """A transcribed file, the service's seams pointed at the test session, and a clean mock log."""
    root = mock_llm_url.removesuffix("/v1")
    requests.post(f"{root}/_mock/requests/clear", timeout=10)
    tracker = ScopeTracker(db_session)
    model = {"name": "mock-topics-json"}

    monkeypatch.setattr("app.services.topic_extraction_service.session_scope", tracker.scope)
    # The usage ledger opens its own scope; give it the test session rather than a second one.
    monkeypatch.setattr("app.db.session_utils.session_scope", tracker.scope)

    def make_service(*, user_id):
        service = LLMService(
            LLMConfig(
                provider=LLMProvider.CUSTOM,
                model=model["name"],
                base_url=mock_llm_url,
                api_key="mock-key-not-secret",
            )
        )
        # Not what is under test, and the real policy backs off for seconds on every 5xx.
        service._retry_strategy = Retry(total=0)
        return service

    monkeypatch.setattr(LLMService, "create_from_settings", staticmethod(make_service))

    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=normal_user.id,
        filename="standup.wav",
        storage_path=f"test/standup-{uuid_mod.uuid4().hex}.wav",
        content_type="audio/wav",
        file_size=1000,
        duration=120.0,
        status=FileStatus.COMPLETED,
    )
    db_session.add(media_file)
    db_session.flush()
    speaker = Speaker(
        uuid=str(uuid_mod.uuid4()),
        media_file_id=media_file.id,
        user_id=normal_user.id,
        name="SPEAKER_00",
    )
    db_session.add(speaker)
    db_session.flush()
    for index, text in enumerate(TRANSCRIPT_LINES):
        db_session.add(
            TranscriptSegment(
                uuid=str(uuid_mod.uuid4()),
                media_file_id=media_file.id,
                speaker_id=speaker.id,
                start_time=index * 10.0,
                end_time=index * 10.0 + 8.0,
                text=text,
            )
        )
    db_session.flush()

    def rows() -> list[TopicSuggestion]:
        db_session.expire_all()
        return list(
            db_session.query(TopicSuggestion)
            .filter(TopicSuggestion.media_file_id == media_file.id)
            .all()
        )

    def calls() -> list[dict]:
        return list(requests.get(f"{root}/_mock/requests", timeout=10).json()["requests"])

    return SimpleNamespace(
        media_file=media_file,
        model=model,
        tracker=tracker,
        rows=rows,
        calls=calls,
        service=lambda: TopicExtractionService(db_session),
    )


def test_the_real_prompt_reaches_the_provider_and_the_reply_is_stored(extraction):
    result = extraction.service().extract_topics(extraction.media_file.id)

    assert result is not None
    assert (result.status, result.tag_count, result.collection_count) == ("completed", 2, 1)
    sent = extraction.calls()
    assert len(sent) == 1 and sent[0]["model"] == "mock-topics-json"
    system, user = (m["content"] for m in sent[0]["messages"][:2])
    assert system == TopicExtractionService.SYSTEM_PROMPT_TEMPLATE.format(language_instruction="")
    assert sent[0]["system_sha256"] == hashlib.sha256(system.encode()).hexdigest()
    # The read phase built the transcript the provider was sent (stopwords are stripped for
    # topics, so check a content word rather than a sentence).
    assert "budget" in user and "hiring" in user.lower()

    (suggestion,) = extraction.rows()
    assert suggestion.status == "pending"
    assert [t["name"] for t in suggestion.suggested_tags] == ["mock-planning", "mock-schedule"]
    assert [c["name"] for c in suggestion.suggested_collections] == ["Mock Planning Sessions"]
    assert extraction.tracker.depth == 0, "a session scope survived the call"


def test_a_stored_suggestion_is_not_asked_for_again_unless_forced(extraction):
    service = extraction.service()
    service.extract_topics(extraction.media_file.id)

    again = service.extract_topics(extraction.media_file.id)
    assert again is not None and again.status == "existing"
    assert len(extraction.calls()) == 1, "the stored suggestion was re-requested"

    forced = service.extract_topics(extraction.media_file.id, force_regenerate=True)
    assert forced is not None and forced.status == "completed"
    assert len(extraction.calls()) == 2
    assert len(extraction.rows()) == 1, "regeneration must update the row, not add a second"


@pytest.mark.parametrize(
    "model, why",
    [
        ("mock-error", "the provider rejected the call"),
        # Not mock-gpt: it answers a prompt that spells out the topic schema with that JSON,
        # as a real model does (#1308). mock-reasoning still replies with the markdown chat text.
        ("mock-reasoning", "the reply is markdown with no JSON in it"),
        ("mock-empty", "the reply is empty"),
        ("mock-summary-json", "the reply is JSON of the wrong shape"),
    ],
)
def test_a_reply_that_cannot_be_used_stores_nothing(extraction, model, why):
    extraction.model["name"] = model

    result = extraction.service().extract_topics(extraction.media_file.id)

    assert result is None, why
    assert extraction.rows() == [], why
    assert [c["model"] for c in extraction.calls()] == [model]
