"""A provider failure reaches the user as a code + fixed sentence, never provider prose (#1049).

Observed live: a Bedrock ``ServiceUnavailableException`` during a capacity blip was
relayed verbatim into the chat thread ("Bedrock error: An error occurred
(ServiceUnavailableException) when calling the ConverseStream operation (reached max
retries: 4)...") and persisted on the message row, so it came back on every reload.

Driven through the REAL ``stream_reply`` (only retrieval, persistence and the LLM are
stubbed, as in ``test_chat_reasoning_sse_frames.py``), so the frame payloads and the
persisted turn fields come from the shipped code path.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest

from app.services.chat import service as chat_service
from app.services.chat.redactor import MaskedChunk
from app.services.chat.settings import ChatSettings
from app.services.llm_stream import LLMStreamEvent
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.search.chunk_retrieval import ChunkHit

#: The verbatim provider text from the issue. None of it may reach the user.
BEDROCK_503 = (
    "Bedrock error: An error occurred (ServiceUnavailableException) when calling the "
    "ConverseStream operation (reached max retries: 4): Bedrock is unable to process "
    "your request."
)
BEDROCK_VALIDATION = "Bedrock error: ValidationException: model id us.bogus is invalid"


def _chunk() -> MaskedChunk:
    return MaskedChunk(
        source=ChunkHit(
            file_uuid="11111111-1111-1111-1111-000000000001",
            file_id=1,
            chunk_index=1,
            content="we agreed on four buttons",
            title="Remote control review",
            speaker="Dana",
            start_time=60.0,
            end_time=90.0,
        ),
        content="we agreed on four buttons",
    )


class _FakeConfig:
    provider = "bedrock"
    model = "anthropic.claude-haiku-4-5-20251001-v1:0"


class _FailingLLM:
    """Emits one provider ``error`` event, as ``stream_converse`` does on a failed call."""

    def __init__(self, message: str, transient: bool):
        self.config = _FakeConfig()
        self.user_context_window = 32_000
        self.response_tokens = 4000
        self._event = LLMStreamEvent(type="error", message=message, transient=transient)

    def chat_completion_stream(self, messages, cancel_event=None, **_kwargs):
        yield self._event

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


@contextmanager
def _null_session():
    yield None


async def _run(monkeypatch, llm) -> tuple[list[tuple[str, dict]], Any]:
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    monkeypatch.setattr(
        chat_service,
        "_prepare_context",
        lambda *_a, **_k: (
            [_chunk()],
            {"retrieved": 1, "files_searched": "all"},
            None,
            None,
            "",
            "",
        ),
    )
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)
    monkeypatch.setattr(
        chat_service,
        "_resolve_output_policy",
        lambda _user_id, _organization_id=None: EffectiveRedactionConfig(enabled=False),
    )
    captured: dict[str, Any] = {}

    async def _fake_finalize(**kwargs):
        captured["turn"] = kwargs["turn"]

    monkeypatch.setattr(chat_service, "_finalize_turn", _fake_finalize)

    frames: list[tuple[str, dict]] = []
    async for raw in chat_service.ChatService.stream_reply(
        conversation_id=1,
        conversation_uuid="conv-uuid",
        user_id=1,
        organization_id=None,
        question="what did the team decide about the buttons?",
        history=[],
        file_uuids=None,
        speakers=[],
        settings=ChatSettings(),
        use_context=True,
        system_prompt="SYS",
        search_mode="hybrid",
        temperature=None,
        max_tokens=None,
        top_p=None,
        llm=llm,
        assistant_message_uuid="00000000-0000-0000-0000-0000000000aa",
        user_message_uuid="00000000-0000-0000-0000-0000000000bb",
        is_first_exchange=True,
    ):
        if raw.startswith(":"):
            continue
        name = raw.split("event: ", 1)[1].split("\n", 1)[0]
        frames.append((name, json.loads(raw.split("data: ", 1)[1].strip())))
    return frames, captured.get("turn")


def _error_frames(frames):
    return [payload for name, payload in frames if name == "error"]


@pytest.mark.asyncio
async def test_a_transient_provider_error_is_reported_as_provider_unavailable(monkeypatch):
    frames, turn = await _run(monkeypatch, _FailingLLM(BEDROCK_503, transient=True))

    assert _error_frames(frames) == [
        {"code": "provider_unavailable", "message": chat_service.PROVIDER_UNAVAILABLE_MESSAGE}
    ]
    assert turn is not None, "the turn was never finalized"
    assert turn.error_code == "provider_unavailable"
    # Persisted so a reloaded thread renders the same translated message.
    assert turn.metadata["error_code"] == "provider_unavailable"
    assert turn.error == chat_service.PROVIDER_UNAVAILABLE_MESSAGE


@pytest.mark.asyncio
async def test_a_non_transient_provider_error_is_a_generic_provider_error(monkeypatch):
    frames, turn = await _run(monkeypatch, _FailingLLM(BEDROCK_VALIDATION, transient=False))

    assert _error_frames(frames) == [
        {"code": "provider_error", "message": chat_service.PROVIDER_ERROR_MESSAGE}
    ]
    assert turn.error_code == "provider_error"
    assert turn.metadata["error_code"] == "provider_error"


@pytest.mark.parametrize(
    ("message", "transient"), [(BEDROCK_503, True), (BEDROCK_VALIDATION, False)]
)
@pytest.mark.asyncio
async def test_provider_prose_never_reaches_the_wire_or_the_row(
    monkeypatch, caplog, message, transient
):
    with caplog.at_level("WARNING", logger=chat_service.logger.name):
        frames, turn = await _run(monkeypatch, _FailingLLM(message, transient=transient))

    wire = json.dumps(frames)
    assert "Bedrock" not in wire and "ConverseStream" not in wire and "Validation" not in wire
    assert "Bedrock" not in (turn.error or ""), "provider prose persisted to the message row"
    # ...but the operator still gets it.
    assert message in caplog.text


@pytest.mark.asyncio
async def test_an_exception_in_the_stream_does_not_persist_its_text(monkeypatch):
    """The broad ``except`` already kept ``str(exc)`` off the wire, but stored it on the
    row that ``GET /chat/.../messages`` returns — the same leak one reload later."""

    class _ExplodingLLM(_FailingLLM):
        def chat_completion_stream(self, messages, cancel_event=None, **_kwargs):
            raise RuntimeError("secret internal detail /srv/app/boom.py")

    frames, turn = await _run(monkeypatch, _ExplodingLLM("", transient=False))

    assert _error_frames(frames) == [
        {"code": "provider_error", "message": chat_service.GENERATION_FAILED_MESSAGE}
    ]
    assert "secret internal detail" not in (turn.error or "")
    assert turn.metadata["error_code"] == "provider_error"
