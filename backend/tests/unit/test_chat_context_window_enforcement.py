"""The chat runtime path enforces the MEASURED context window, not just the DECLARED one.

Issue #533 built the probe: a real, opt-in measurement of a model's actual context
window (vLLM ``gemma-4-e4b`` -> 60000 via ``max_model_len``; Ollama ``qwen3.8`` ->
262144 via ``qwen35.context_length``), stored in ``SystemSettings`` keyed by a
fingerprint of (provider, base_url, model). Its only consumers were the settings-page
endpoints -- nothing on the chat/summarization runtime path read it, so a declared
``max_tokens`` above the real ceiling still produced either a hard 400
(vLLM/OpenAI-compatible) or silent front-of-context truncation (Ollama sends the
declared value as ``num_ctx``, dropping the system prompt first). Issue #873 raising
Ollama's declared default to 128000 made this the common case, not the rare one.

Issue #833 is what applies the measurement: ``llm_context_window.effective_window``
narrows ``LLMConfig.max_tokens`` at the three ``LLMService.create_from_*`` factories,
so every downstream budget computation (response token sizing, the Ollama ``num_ctx``
payload, ``resolve_answer_tokens``/``build_messages`` in ``chat/prompting.py``) is
narrowed automatically through ``self.user_context_window``.

Part A tests the resolution rule in isolation. Part B tests that the factories
actually apply it. Part C tests that the chat prompt budget actually shrinks, and
that the effective window is attributed on the persisted turn.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

from app.core.enums import ContextWindowStatus
from app.models.prompt import UserSetting
from app.models.user_llm_settings import UserLLMSettings
from app.services import llm_context_window
from app.services.chat import service as chat_service
from app.services.chat.prompting import _CHARS_PER_TOKEN
from app.services.chat.prompting import _CONTEXT_SAFETY_MARGIN_TOKENS
from app.services.chat.prompting import build_messages
from app.services.chat.redactor import MaskedChunk
from app.services.chat.service import resolve_answer_tokens
from app.services.chat.settings import ChatSettings
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService
from app.services.llm_stream import LLMStreamEvent
from app.services.search.chunk_retrieval import ChunkHit


class _NoCloseSession:
    """The fixture session, with close() neutered -- the factory closes its own."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        pass


def _config(
    *,
    model: str = "model-a",
    base_url: str | None = "http://llm:8000/v1",
    provider: LLMProvider = LLMProvider.OLLAMA,
) -> LLMConfig:
    return LLMConfig(provider=provider, model=model, base_url=base_url, api_key="")


def _record(db, *, model="model-a", base_url="http://llm:8000/v1", status, context_window=None):
    llm_context_window.record(
        db,
        _config(model=model, base_url=base_url),
        llm_context_window.ContextWindowProbeResult(status=status, context_window=context_window),
    )


# --------------------------------------------------------------------------- #
# Part A -- the resolution rule (effective_window), against db_session directly
# --------------------------------------------------------------------------- #


def test_a_measured_window_below_the_declared_one_is_the_one_used(db_session):
    """The must-fire case."""
    _record(db_session, status=ContextWindowStatus.MEASURED, context_window=8192)

    result = llm_context_window.effective_window(
        db_session,
        provider="ollama",
        base_url="http://llm:8000/v1",
        model="model-a",
        declared=128_000,
    )
    assert result == 8192


def test_an_unprobed_config_keeps_the_declared_window(db_session):
    """Control 1: no measurement record exists at all."""
    result = llm_context_window.effective_window(
        db_session,
        provider="ollama",
        base_url="http://llm:8000/v1",
        model="never-probed",
        declared=128_000,
    )
    assert result == 128_000


def test_a_measured_window_above_the_declared_one_never_raises_it(db_session):
    """Control 2: without this, "always return measured" would pass test 1."""
    _record(
        db_session,
        model="model-b",
        status=ContextWindowStatus.MEASURED,
        context_window=262_144,
    )

    result = llm_context_window.effective_window(
        db_session,
        provider="ollama",
        base_url="http://llm:8000/v1",
        model="model-b",
        declared=32_768,
    )
    assert result == 32_768


@pytest.mark.parametrize(
    "status",
    [
        ContextWindowStatus.UNSUPPORTED,
        ContextWindowStatus.UNREACHABLE,
        ContextWindowStatus.NOT_FOUND,
    ],
)
def test_a_non_measured_verdict_keeps_the_declared_window(db_session, status):
    _record(db_session, model=f"model-{status.value}", status=status, context_window=None)

    result = llm_context_window.effective_window(
        db_session,
        provider="ollama",
        base_url="http://llm:8000/v1",
        model=f"model-{status.value}",
        declared=64_000,
    )
    assert result == 64_000


def test_a_measurement_for_another_model_does_not_apply(db_session):
    _record(db_session, model="model-a", status=ContextWindowStatus.MEASURED, context_window=8192)

    result = llm_context_window.effective_window(
        db_session,
        provider="ollama",
        base_url="http://llm:8000/v1",
        model="model-b",
        declared=128_000,
    )
    assert result == 128_000


def test_a_measurement_for_another_endpoint_does_not_apply(db_session):
    _record(
        db_session,
        model="model-a",
        base_url="http://llm:8000/v1",
        status=ContextWindowStatus.MEASURED,
        context_window=8192,
    )

    result = llm_context_window.effective_window(
        db_session,
        provider="ollama",
        base_url="http://llm:9999/v1",
        model="model-a",
        declared=128_000,
    )
    assert result == 128_000


def test_the_narrowing_is_logged_with_both_numbers(db_session, caplog):
    _record(db_session, status=ContextWindowStatus.MEASURED, context_window=8192)

    with caplog.at_level(logging.WARNING):
        llm_context_window.effective_window(
            db_session,
            provider="ollama",
            base_url="http://llm:8000/v1",
            model="model-a",
            declared=128_000,
        )

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "128000" in text
    assert "8192" in text


# --------------------------------------------------------------------------- #
# Part B -- the service actually drives the narrowed window
# --------------------------------------------------------------------------- #


@pytest.fixture
def _narrowed_config(db_session, normal_user):
    """A real UserLLMSettings row declaring 128000, measured at 8192."""
    config = UserLLMSettings(
        user_id=normal_user.id,
        name="narrowed-config",
        provider="ollama",
        model_name="qwen3.8:latest",
        base_url="http://ollama:11434/v1",
        max_tokens=128_000,
        temperature="0.3",
    )
    db_session.add(config)
    db_session.commit()
    db_session.refresh(config)

    llm_context_window.record(
        db_session,
        LLMConfig(
            provider=LLMProvider.OLLAMA,
            model="qwen3.8:latest",
            base_url="http://ollama:11434/v1",
        ),
        llm_context_window.ContextWindowProbeResult(
            status=ContextWindowStatus.MEASURED, context_window=8192
        ),
    )
    return config


@pytest.fixture
def _unprobed_config(db_session, normal_user):
    """A sibling row with the SAME declared value but never measured."""
    config = UserLLMSettings(
        user_id=normal_user.id,
        name="unprobed-config",
        provider="ollama",
        model_name="never-probed-model",
        base_url="http://ollama:11434/v1",
        max_tokens=128_000,
        temperature="0.3",
    )
    db_session.add(config)
    db_session.commit()
    db_session.refresh(config)
    return config


def test_create_from_config_id_drives_the_measured_window(
    monkeypatch, db_session, normal_user, _narrowed_config
):
    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _NoCloseSession(db_session))

    svc = LLMService.create_from_config_id(normal_user.id, _narrowed_config.id)

    assert svc is not None
    assert svc.user_context_window == 8192
    assert svc.config.max_tokens == 8192


def test_the_derived_response_budget_follows_the_narrowed_window(
    monkeypatch, db_session, normal_user, _narrowed_config, _unprobed_config
):
    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _NoCloseSession(db_session))

    narrowed = LLMService.create_from_config_id(normal_user.id, _narrowed_config.id)
    unnarrowed = LLMService.create_from_config_id(normal_user.id, _unprobed_config.id)

    assert narrowed is not None
    assert unnarrowed is not None
    # max(4000, min(16384, 8192 // 4)) == 4000
    assert narrowed.response_tokens == 4000
    # Control: without the narrowing, the same math off 128000 derives 16384.
    assert unnarrowed.response_tokens == 16384


def test_the_ollama_payload_sends_the_measured_num_ctx(
    monkeypatch, db_session, normal_user, _narrowed_config
):
    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _NoCloseSession(db_session))

    svc = LLMService.create_from_config_id(normal_user.id, _narrowed_config.id)
    assert svc is not None

    payload = svc._prepare_ollama_payload([{"role": "user", "content": "hi"}])
    assert payload["options"]["num_ctx"] == 8192


def test_an_unprobed_config_still_drives_its_declared_window(
    monkeypatch, db_session, normal_user, _unprobed_config
):
    """Control: same shape, no measurement -> the declared value stands."""
    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _NoCloseSession(db_session))

    svc = LLMService.create_from_config_id(normal_user.id, _unprobed_config.id)

    assert svc is not None
    assert svc.user_context_window == 128_000
    assert svc.config.max_tokens == 128_000


def test_create_from_user_settings_drives_the_measured_window(
    monkeypatch, db_session, normal_user, _narrowed_config
):
    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _NoCloseSession(db_session))

    active = UserSetting(
        user_id=normal_user.id,
        setting_key="active_llm_config_id",
        setting_value=str(_narrowed_config.id),
    )
    db_session.add(active)
    db_session.commit()

    svc = LLMService.create_from_user_settings(normal_user.id)

    assert svc is not None
    assert svc.user_context_window == 8192
    assert svc.config.max_tokens == 8192


# --------------------------------------------------------------------------- #
# Part C -- the budget the chat path actually computes
# --------------------------------------------------------------------------- #


def _excerpt_chunk(index: int) -> MaskedChunk:
    content = f"chunk {index} " + "word " * 400
    return MaskedChunk(
        source=ChunkHit(
            file_uuid=f"11111111-1111-1111-1111-00000000000{index}",
            file_id=index,
            chunk_index=index,
            content=content,
            title=f"Recording {index}",
            speaker="Dana",
            start_time=float(index * 60),
            end_time=float(index * 60 + 30),
        ),
        content=content,
    )


def test_the_excerpt_budget_shrinks_to_the_measured_window():
    chunks = [_excerpt_chunk(i) for i in range(1, 15)]

    narrowed = LLMService(
        LLMConfig(provider=LLMProvider.OLLAMA, model="m", base_url="http://x/v1", max_tokens=8192)
    )
    declared = LLMService(
        LLMConfig(
            provider=LLMProvider.OLLAMA, model="m", base_url="http://x/v1", max_tokens=128_000
        )
    )

    narrowed_tokens = resolve_answer_tokens(
        requested=None,
        tenant_ceiling=None,
        default_tokens=narrowed.response_tokens,
        context_window=narrowed.user_context_window,
    )
    d_narrowed: dict = {}
    build_messages(
        system_prompt="SYS",
        chunks=chunks,
        history=[],
        question="What did the team decide?",
        context_window=narrowed.user_context_window,
        response_tokens=narrowed_tokens,
        diagnostics=d_narrowed,
    )

    declared_tokens = resolve_answer_tokens(
        requested=None,
        tenant_ceiling=None,
        default_tokens=declared.response_tokens,
        context_window=declared.user_context_window,
    )
    d_declared: dict = {}
    build_messages(
        system_prompt="SYS",
        chunks=chunks,
        history=[],
        question="What did the team decide?",
        context_window=declared.user_context_window,
        response_tokens=declared_tokens,
        diagnostics=d_declared,
    )

    assert d_narrowed["budget_chars"] < d_declared["budget_chars"]
    assert (
        d_narrowed["budget_chars"]
        <= (8192 - narrowed_tokens - _CONTEXT_SAFETY_MARGIN_TOKENS) * _CHARS_PER_TOKEN
    )


class _FakeProvider:
    value = "custom"


class _FakeConfig:
    provider = _FakeProvider()
    model = "test-model"


class _FakeTurnLLM:
    """Minimal stand-in for LLMService -- only what stream_reply reads."""

    def __init__(self, *, context_window: int, response_tokens: int = 4000):
        self.config = _FakeConfig()
        self.user_context_window = context_window
        self.response_tokens = response_tokens

    def chat_completion_stream(self, messages, cancel_event=None, **_kwargs):
        yield LLMStreamEvent(type="delta", text="An answer.")
        yield LLMStreamEvent(type="done", finish_reason="stop")

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


@contextmanager
def _null_session():
    yield None


async def _run_turn(monkeypatch, *, context_window: int):
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    chunk = _excerpt_chunk(1)
    diagnostics = {"retrieved": 1, "files_searched": "all"}
    monkeypatch.setattr(
        chat_service,
        "_prepare_context",
        lambda *_a, **_kw: ([chunk], dict(diagnostics), None, None, "", ""),
    )
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)

    captured: dict = {}

    async def _fake_finalize(**kwargs):
        captured["turn"] = kwargs["turn"]

    monkeypatch.setattr(chat_service, "_finalize_turn", _fake_finalize)

    llm = _FakeTurnLLM(context_window=context_window)
    generator = chat_service.ChatService.stream_reply(
        conversation_id=1,
        conversation_uuid="conv-uuid",
        user_id=1,
        organization_id=None,
        question="What did the team decide?",
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
    )
    async for _ in generator:
        pass

    return captured["turn"], llm


@pytest.mark.asyncio
async def test_a_turn_records_the_window_its_budget_was_computed_against(monkeypatch):
    turn, llm = await _run_turn(monkeypatch, context_window=8192)
    assert turn.metadata["context_window"] == llm.user_context_window == 8192
