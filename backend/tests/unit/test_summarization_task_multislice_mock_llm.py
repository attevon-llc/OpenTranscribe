"""The summarization task, end to end, over a transcript that needs several slices.

``summarize_transcript_task`` is run for real: the read phase, the slice calls, the merge call,
the persist phase and the notifications. Only the model is canned, by the mock LLM server's
``mock-summary-json`` scenario, so the real ``LLMService`` builds each payload and sends it over
HTTP. The mock records every request it receives, which is how these tests count slice and merge
calls and read what each carried.

This proves wiring, not quality. The mock's reply is a constant, so nothing here says anything
about whether a summary is any good (that is what ``tests/eval`` is for).

No GPU and no container: ``mock_llm_url`` starts a subprocess, so these run in CI. DB-backed, so
run them through ``scripts/run-backend-tests.sh --throwaway-db``.
"""

from __future__ import annotations

import json
import uuid as uuid_mod
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from urllib3.util import Retry

from app.core.config import settings
from app.core.default_prompts import UNIVERSAL_CONTENT_ANALYZER_PROMPT
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.schemas.summary import SummaryData
from app.services.llm_service import LLMService
from app.services.llm_types import LLMConfig
from app.services.llm_types import LLMProvider
from tests.unit._session_lifetime_trackers import ScopeTracker

pytestmark = pytest.mark.unit

MERGE_HEADER = "SECTION SUMMARIES TO COMBINE"
ECHO_FIELD = "mock_system_sha256"
#: One marker per slice, so a test can see which slice's text a call carried.
MARKERS = ("ALPHA", "BRAVO", "CHARLIE")
#: Each turn is sized so that two never fit in one slice at this window, and one always does.
CONTEXT_WINDOW = 8192
TURN_TOKENS = 1500


@pytest.fixture(autouse=True)
def _allow_the_loopback_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSRF guard refuses a private address, and the mock is always on loopback."""
    monkeypatch.setattr(settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", True)


def _service(url: str, model: str) -> LLMService:
    service = LLMService(
        LLMConfig(
            provider=LLMProvider.CUSTOM,
            model=model,
            base_url=url,
            api_key="mock-key-not-secret",
            max_tokens=CONTEXT_WINDOW,
        )
    )
    # Not what is under test, and the real policy backs off for seconds on every 5xx.
    service._retry_strategy = Retry(total=0)
    return service


def _turn_text(service: LLMService, marker: str) -> str:
    sentence = f"{marker} the group reviewed one more item on the plan and discussed the schedule. "
    text = sentence
    while service._estimate_tokens(text) < TURN_TOKENS:
        text += sentence
    return text


class _Calls:
    """The chat requests the mock saw during one test, split into slice and merge calls."""

    def __init__(self, root: str) -> None:
        logged = requests.get(f"{root}/_mock/requests", timeout=10).json()["requests"]
        self.all: list[dict[str, Any]] = logged
        self.merge = [c for c in logged if MERGE_HEADER in _user_text(c)]
        self.slices = [c for c in logged if MERGE_HEADER not in _user_text(c)]


def _user_text(call: dict[str, Any]) -> str:
    return "\n".join(m["content"] for m in call["messages"] if m["role"] == "user")


def _merged_sections(merge_call: dict[str, Any]) -> list[dict[str, Any]]:
    """The slice summaries exactly as the merge prompt carried them."""
    text = _user_text(merge_call)
    start = text.index("[", text.index(MERGE_HEADER))
    sections, _ = json.JSONDecoder().raw_decode(text[start:])
    return list(sections)


@pytest.fixture
def world(db_session, normal_user, monkeypatch: pytest.MonkeyPatch, mock_llm_url: str):
    """A transcribed file, the task's seams pointed at the test session, and a clean mock log."""
    from app.tasks import search_file_field_tasks
    from app.tasks import summarization as summ

    root = mock_llm_url.removesuffix("/v1")
    requests.post(f"{root}/_mock/requests/clear", timeout=10)

    tracker = ScopeTracker(db_session)
    notifications: list[tuple[str, int, str]] = []
    indexed: list[int] = []
    state = SimpleNamespace(model="mock-summary-json")

    monkeypatch.setattr(summ, "session_scope", tracker.scope)
    monkeypatch.setattr("app.tasks.summarization_inputs.session_scope", tracker.scope)
    monkeypatch.setattr("app.tasks.summarization_inputs.resolve_llm_masking", lambda *a: None)
    # The usage ledger opens its own scope; give it the test session rather than a second one.
    monkeypatch.setattr("app.db.session_utils.session_scope", tracker.scope)

    def record_notification(
        user_id, file_id, status, message, progress=0, summary_data=None, **kw
    ) -> bool:
        notifications.append((status, progress, message))
        return True

    monkeypatch.setattr(summ, "send_summary_notification", record_notification)
    monkeypatch.setattr(search_file_field_tasks.index_file_summary, "delay", indexed.append)
    monkeypatch.setattr(
        "app.utils.prompt_manager.get_user_active_prompt",
        lambda *a, **kw: UNIVERSAL_CONTENT_ANALYZER_PROMPT,
    )

    class _Factory:
        """A fresh real service per call, as the task's availability probe closes the one it gets."""

        @staticmethod
        def create_from_user_settings(user_id):
            return _service(mock_llm_url, state.model)

        @staticmethod
        def create_from_system_settings():
            return _service(mock_llm_url, state.model)

    monkeypatch.setattr(summ, "LLMService", _Factory)

    def make_file(turns: int) -> MediaFile:
        probe = _service(mock_llm_url, state.model)
        media_file = MediaFile(
            uuid=str(uuid_mod.uuid4()),
            user_id=normal_user.id,
            filename="meeting.mp4",
            storage_path=f"test/meeting-{uuid_mod.uuid4().hex}.mp4",
            content_type="video/mp4",
            file_size=1000,
        )
        db_session.add(media_file)
        db_session.flush()
        speakers = []
        for label in ("SPEAKER_00", "SPEAKER_01"):
            speaker = Speaker(
                uuid=str(uuid_mod.uuid4()),
                media_file_id=media_file.id,
                user_id=normal_user.id,
                name=label,
            )
            db_session.add(speaker)
            speakers.append(speaker)
        db_session.flush()
        for index in range(turns):
            db_session.add(
                TranscriptSegment(
                    uuid=str(uuid_mod.uuid4()),
                    media_file_id=media_file.id,
                    speaker_id=speakers[index % 2].id,
                    start_time=index * 60.0,
                    end_time=index * 60.0 + 30.0,
                    text=_turn_text(probe, MARKERS[index]),
                )
            )
        db_session.flush()
        return media_file

    def run(media_file: MediaFile) -> dict[str, Any]:
        result = summ.summarize_transcript_task.apply(args=[str(media_file.uuid)]).get()
        db_session.expire_all()
        return dict(result)

    return SimpleNamespace(
        root=root,
        tracker=tracker,
        notifications=notifications,
        indexed=indexed,
        state=state,
        make_file=make_file,
        run=run,
        calls=lambda: _Calls(root),
        reload=lambda mf: db_session.query(MediaFile).filter(MediaFile.id == mf.id).one(),
    )


# ------------------------------------------------------------------ (i) three slices, all fine


def test_three_slices_are_summarized_separately_then_merged_once(world):
    media_file = world.make_file(turns=3)

    result = world.run(media_file)

    assert result["status"] == "success", result
    calls = world.calls()
    assert len(calls.slices) == 3, f"expected 3 slice calls, saw {len(calls.slices)}"
    assert len(calls.merge) == 1, f"expected 1 merge call, saw {len(calls.merge)}"
    # Each slice call carries exactly its own slice of the recording...
    for marker in MARKERS:
        carrying = [c for c in calls.slices if marker in _user_text(c)]
        assert len(carrying) == 1, f"{marker} reached {len(carrying)} slice calls"
    # ...and the merge sees their summaries, never the transcript itself.
    assert not any(marker in _user_text(calls.merge[0]) for marker in MARKERS)
    # Slices and merge are told different things, which is the point of two prompts.
    merge_prompt = calls.merge[0]["system_sha256"]
    assert all(c["system_sha256"] != merge_prompt for c in calls.slices)


def test_the_persisted_summary_is_the_merge_reply_and_is_valid(world):
    media_file = world.make_file(turns=3)

    world.run(media_file)

    persisted = world.reload(media_file)
    assert persisted.summary_status == "completed"
    data = SummaryData.model_validate(persisted.summary_data)
    assert data.bluf and data.brief_summary
    # Which prompt produced the stored summary: the merge's, not a slice's.
    merge_prompt = world.calls().merge[0]["system_sha256"]
    assert persisted.summary_data[ECHO_FIELD] == merge_prompt
    metadata = persisted.summary_data["metadata"]
    assert metadata["sections_processed"] == 3
    assert metadata["processing_method"] == "multi-section"
    assert metadata["model"] == "mock-summary-json"
    assert "source_fingerprint" in metadata


def test_the_user_is_told_in_order_and_the_index_is_queued_after_the_write(world):
    media_file = world.make_file(turns=3)

    world.run(media_file)

    statuses = [(status, progress) for status, progress, _ in world.notifications]
    assert statuses == [
        ("processing", 10),
        ("processing", 30),
        ("processing", 50),
        ("completed", 100),
    ]
    assert world.indexed == [media_file.id]
    assert world.tracker.depth == 0, "a session scope survived the task"


# --------------------------------------------------------------- (ii) one slice fails in-process


@pytest.fixture
def slice_two_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    original = LLMService._summarize_section

    def flaky(self, chunk, section_num, *args, **kwargs):
        if section_num == 2:
            raise TimeoutError("provider at http://internal-llm.invalid:8000 timed out")
        return original(self, chunk, section_num, *args, **kwargs)

    monkeypatch.setattr(LLMService, "_summarize_section", flaky)


def test_a_failed_slice_reaches_the_merge_flagged_and_without_provider_detail(
    world, slice_two_raises
):
    media_file = world.make_file(turns=3)

    result = world.run(media_file)

    assert result["status"] == "success", result
    calls = world.calls()
    assert len(calls.slices) == 2, "the failed slice never reached the provider"
    assert len(calls.merge) == 1
    sections = _merged_sections(calls.merge[0])
    assert [s["section"] for s in sections] == [1, 2, 3]
    assert [bool(s["summary"].get("_error")) for s in sections] == [False, True, False]
    assert sections[1]["summary"]["key_points"] == ["Section 2: Processing failed (TimeoutError)"]
    # The merge prompt is egressed to the provider and can be echoed into the stored summary.
    assert "internal-llm.invalid" not in _user_text(calls.merge[0])


def test_a_failed_slice_still_leaves_a_stored_summary_and_a_completed_notification(
    world, slice_two_raises
):
    media_file = world.make_file(turns=3)

    world.run(media_file)

    persisted = world.reload(media_file)
    assert persisted.summary_status == "completed"
    assert SummaryData.model_validate(persisted.summary_data).bluf
    assert "internal-llm.invalid" not in json.dumps(persisted.summary_data)
    final = world.notifications[-1]
    assert final[:2] == ("completed", 100)
    assert "internal-llm.invalid" not in " ".join(m for _, _, m in world.notifications)


# --------------------------------------------------------------- (iii) the provider fails every call


def test_a_provider_that_fails_a_single_slice_summary_marks_the_file_failed_once(world):
    world.state.model = "mock-error"
    media_file = world.make_file(turns=1)

    result = world.run(media_file)

    assert result["status"] == "error", result
    assert world.reload(media_file).summary_status == "failed"
    failures = [m for status, _, m in world.notifications if status == "failed"]
    assert len(failures) == 1, f"the user was told {len(failures)} times: {failures}"
    assert failures[0].startswith("AI summary generation failed: ")
    assert result["message"].startswith("LLM summarization failed: ")
    assert result["message"].endswith("No fallback summary will be generated.")
    # What the provider said (its body, its address) must not reach the user through either
    # channel: the user gets one of a few fixed sentences, never the exception text.
    for shown in (result["message"], failures[0]):
        assert "Mock provider failure" not in shown
        assert "127.0.0.1" not in shown
    assert world.indexed == [], "nothing was written, so nothing is indexed"


def test_a_provider_that_fails_every_call_of_a_multi_slice_summary_records_why(world):
    world.state.model = "mock-error"
    media_file = world.make_file(turns=3)

    world.run(media_file)

    persisted = world.reload(media_file)
    metadata = persisted.summary_data["metadata"]
    assert metadata["error"].startswith("Section combining failed (")
    assert metadata["sections_processed"] == 3
    assert "127.0.0.1" not in json.dumps(persisted.summary_data)
    calls = world.calls()
    assert len(calls.slices) == 3 and len(calls.merge) == 1
