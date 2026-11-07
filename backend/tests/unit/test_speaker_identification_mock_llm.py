"""LLM speaker identification, end to end, against the mock LLM's ``mock-speakers-json`` scenario.

``identify_speakers_llm_task`` is run for real: the read phase, the real ``LLMService`` sending
the real prompt over HTTP, the parser, and the write phase into Postgres. Only the model's answer
is canned. The suggestions land on the speaker rows and are **never applied**: the speaker's name
and display name are exactly what they were, because an LLM guess is something a person verifies.

Wiring only: the mock's reply is a constant, so none of this measures identification quality.

No GPU and no container (``mock_llm_url`` starts a subprocess). DB-backed, so run through
``scripts/run-backend-tests.sh --throwaway-db``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests
from urllib3.util import Retry

from app.core.config import settings
from app.models.media import Task as TaskModel
from app.models.speaker import Speaker
from app.services.llm_service import LLMService
from app.services.llm_types import LLMConfig
from app.services.llm_types import LLMProvider
from app.utils.speaker_labels import canonical_speaker_label_for_row
from tests.unit._session_lifetime_trackers import ScopeTracker
from tests.unit._session_lifetime_trackers import make_transcribed_file

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _allow_the_loopback_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSRF guard refuses a private address, and the mock is always on loopback."""
    monkeypatch.setattr(settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", True)


@pytest.fixture
def identify(db_session, normal_user, monkeypatch: pytest.MonkeyPatch, mock_llm_url: str):
    """Run the real task against the mock; returns ``(run, calls)``."""
    from app.tasks import speaker_identification_task as sid

    root = mock_llm_url.removesuffix("/v1")
    requests.post(f"{root}/_mock/requests/clear", timeout=10)
    tracker = ScopeTracker(db_session)
    model = {"name": "mock-speakers-json"}

    monkeypatch.setattr(sid, "session_scope", tracker.scope)
    monkeypatch.setattr(sid, "resolve_llm_masking", lambda db, media_file: None)
    # The usage ledger opens its own scope; give it the test session rather than a second one.
    monkeypatch.setattr("app.db.session_utils.session_scope", tracker.scope)
    # The status report and quarantine check resolve the file through their OWN session, which
    # cannot see this test's savepointed rows.
    monkeypatch.setattr("app.services.post_processing_status.report", lambda *a, **kw: False)
    monkeypatch.setattr(
        "app.services.file_availability.is_notification_suppressed", lambda *a, **kw: False
    )
    # Nothing here reindexes: the rename tracker's flush would dispatch Celery work.
    monkeypatch.setattr(
        "app.services.speaker_rename_tracker.SpeakerRenameTracker.flush", lambda self, db: None
    )

    def make_service(user_id):
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

    monkeypatch.setattr(sid, "_create_llm_service", make_service)

    def run(media_file) -> dict:
        result = sid.identify_speakers_llm_task.apply(args=[str(media_file.uuid)]).get()
        db_session.expire_all()
        return dict(result)

    def calls() -> list[dict]:
        return list(requests.get(f"{root}/_mock/requests", timeout=10).json()["requests"])

    return SimpleNamespace(run=run, calls=calls, model=model, tracker=tracker)


def _speakers(db_session, media_file) -> dict[str, Speaker]:
    rows = db_session.query(Speaker).filter(Speaker.media_file_id == media_file.id).all()
    return {str(s.name): s for s in rows}


def test_the_real_prompt_reaches_the_provider_and_the_predictions_become_suggestions(
    db_session, normal_user, identify
):
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=2)

    result = identify.run(media_file)

    assert result["status"] == "success", result
    assert result["predictions_count"] == 2
    sent = identify.calls()
    assert len(sent) == 1 and sent[0]["model"] == "mock-speakers-json"
    system, user = (m["content"] for m in sent[0]["messages"][:2])
    assert system.startswith("You are an expert linguist and conversation analyst")
    assert "hello there" in user, "the transcript the read phase built must be what was sent"
    assert "CURRENT SPEAKER LABELS" in user

    speakers = _speakers(db_session, media_file)
    first, second = speakers["SPEAKER_00"], speakers["SPEAKER_01"]
    assert (first.suggested_name, first.suggestion_source) == ("Alex Morgan", "llm_analysis")
    assert first.confidence == pytest.approx(0.85)
    assert (second.suggested_name, second.suggestion_source) == ("Priya Natarajan", "llm_analysis")
    assert second.confidence == pytest.approx(0.7)
    assert identify.tracker.depth == 0, "a session scope survived the task"


def test_an_llm_suggestion_is_never_applied_to_the_speaker(db_session, normal_user, identify):
    """The standing rule: a guess is surfaced for a person to verify, never written as the name."""
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=2)

    identify.run(media_file)

    speakers = _speakers(db_session, media_file)
    assert set(speakers) == {"SPEAKER_00", "SPEAKER_01"}
    for label, speaker in speakers.items():
        assert speaker.suggested_name, f"{label} got no suggestion, so this test proves nothing"
        assert speaker.name == label
        assert not speaker.display_name
        assert not speaker.verified
        assert speaker.profile_id is None
        # The name every index and export writer prints is still the diarizer's label.
        assert canonical_speaker_label_for_row(speaker) == label


def test_a_prediction_for_a_label_the_file_does_not_have_is_dropped_quietly(
    db_session, normal_user, identify
):
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)

    result = identify.run(media_file)

    assert result["status"] == "success", result
    speakers = _speakers(db_session, media_file)
    assert list(speakers) == ["SPEAKER_00"], "a speaker was invented for the unknown label"
    assert speakers["SPEAKER_00"].suggested_name == "Alex Morgan"


def test_a_provider_that_errors_fails_the_task_with_an_error_code(
    db_session, normal_user, identify
):
    """A provider fault is a failed run (recorded with a code), not "succeeded with 0"."""
    identify.model["name"] = "mock-error"
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=2)

    result = identify.run(media_file)

    assert result["status"] == "error", result
    assert result["error_code"] == "llm_identification_failed"
    task_row = db_session.query(TaskModel).filter(TaskModel.media_file_id == media_file.id).one()
    assert task_row.status == "failed"
    assert task_row.error_message.startswith("Identification process failed")
    assert [c["model"] for c in identify.calls()] == ["mock-error"]
    assert all(s.suggested_name is None for s in _speakers(db_session, media_file).values())


def test_a_reply_that_is_not_json_writes_no_suggestion(db_session, normal_user, identify):
    """``mock-gpt`` answers in markdown: the parser must reject it rather than invent names."""
    identify.model["name"] = "mock-gpt"
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=2)

    result = identify.run(media_file)

    assert result["status"] == "error", result
    assert result["error_code"] == "llm_identification_failed"
    assert all(s.suggested_name is None for s in _speakers(db_session, media_file).values())
