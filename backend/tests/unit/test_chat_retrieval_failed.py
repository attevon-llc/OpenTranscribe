"""Threading a `retrieval_failed` signal out of `retrieve_chunks` (issue #438's open half).

Before this, a failed OpenSearch query and a genuinely empty library were
indistinguishable: `retrieve_chunks` swallowed every exception and returned `[]`, so
`chat/service.py`'s `no_context` warning covered "nothing matched", "search was down"
and "masking dropped everything" with no way to tell them apart.

Three layers, each pinned here:

1. `chunk_retrieval.retrieve_chunks(..., diagnostics=...)` sets
   ``diagnostics["retrieval_failed"] = True`` on the no-client and exception
   branches only — never on a legitimately empty result.
2. `chat.retrieval.retrieve_context` threads that onto `RetrievalResult.retrieval_failed`.
3. `chat/service.py`'s streaming generator emits the `retrieval_failed` warning code
   (not `no_context`) when that flag is set, driven through the real
   `ChatService.stream_reply` generator exactly as `test_chat_sources_frame.py` does —
   only retrieval and the provider are stubbed.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.chat import service as chat_service
from app.services.chat.settings import ChatSettings
from app.services.llm_stream import LLMStreamEvent
from app.services.search.chunk_retrieval import retrieve_chunks

# --------------------------------------------------------------------------- #
# Layer 1 — retrieve_chunks's diagnostics out-param
# --------------------------------------------------------------------------- #


def test_no_opensearch_client_sets_retrieval_failed():
    diagnostics: dict = {}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=None):
        assert retrieve_chunks("q", user_id=1, diagnostics=diagnostics) == []
    assert diagnostics == {"retrieval_failed": True}


def test_a_raising_search_sets_retrieval_failed():
    client = MagicMock()
    client.search.side_effect = RuntimeError("opensearch down")
    diagnostics: dict = {}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        assert retrieve_chunks("q", user_id=1, diagnostics=diagnostics) == []
    assert diagnostics == {"retrieval_failed": True}


def test_a_genuinely_empty_search_leaves_diagnostics_untouched():
    """The absence of the key is the 'ordinary empty search' signal — not `False`."""
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    diagnostics: dict = {}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        assert retrieve_chunks("q", user_id=1, diagnostics=diagnostics) == []
    assert diagnostics == {}


def test_a_blank_query_never_touches_diagnostics():
    """Short-circuits before any client is even fetched — not a retrieval failure."""
    diagnostics: dict = {}
    assert retrieve_chunks("   ", user_id=1, diagnostics=diagnostics) == []
    assert diagnostics == {}


def test_an_empty_resolved_scope_never_touches_diagnostics():
    diagnostics: dict = {}
    assert retrieve_chunks("q", user_id=1, file_uuids=[], diagnostics=diagnostics) == []
    assert diagnostics == {}


def test_diagnostics_is_optional_and_a_failure_still_degrades_to_empty():
    """The pre-existing #438 contract (fail soft, never raise) survives with no out-param."""
    client = MagicMock()
    client.search.side_effect = RuntimeError("opensearch down")
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        assert retrieve_chunks("q", user_id=1) == []


# --------------------------------------------------------------------------- #
# Layer 2 — chat.retrieval.retrieve_context threads it onto RetrievalResult
# --------------------------------------------------------------------------- #


def test_retrieve_context_surfaces_retrieval_failed():
    from app.services.chat.retrieval import retrieve_context

    client = MagicMock()
    client.search.side_effect = RuntimeError("opensearch down")
    with (
        patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client),
        patch("app.services.chat.retrieval_cache.get_cached", return_value=None),
        patch("app.services.chat.retrieval_cache.set_cached"),
    ):
        result = retrieve_context(
            query="q", user_id=1, organization_id=None, file_uuids=None, settings=ChatSettings()
        )

    assert result.chunks == []
    assert result.retrieved == 0
    assert result.retrieval_failed is True


def test_retrieve_context_leaves_retrieval_failed_false_on_a_genuine_miss():
    from app.services.chat.retrieval import retrieve_context

    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    with (
        patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client),
        patch("app.services.chat.retrieval_cache.get_cached", return_value=None),
        patch("app.services.chat.retrieval_cache.set_cached"),
    ):
        result = retrieve_context(
            query="q", user_id=1, organization_id=None, file_uuids=None, settings=ChatSettings()
        )

    assert result.retrieval_failed is False


# --------------------------------------------------------------------------- #
# Layer 3 — the real stream_reply generator emits the FAILED variant, not
# no_context, when OpenSearch is unreachable — driven exactly like
# test_chat_sources_frame.py's harness (only retrieval/provider stubbed).
# --------------------------------------------------------------------------- #


class _FakeProvider:
    value = "custom"


class _FakeConfig:
    provider = _FakeProvider()
    model = "test-model"


class _FakeLLM:
    def __init__(self, *, context_window: int = 32_000, response_tokens: int = 4000):
        self.config = _FakeConfig()
        self.user_context_window = context_window
        self.response_tokens = response_tokens

    def chat_completion_stream(self, messages, cancel_event=None, **_kwargs):
        yield LLMStreamEvent(type="delta", text="I don't have enough information.")
        yield LLMStreamEvent(type="done", finish_reason="stop")

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


@contextmanager
def _null_session():
    yield None


async def _collect(monkeypatch, *, meta: dict):
    """Run one turn with `_prepare_context` stubbed to report `meta`, exactly as

    `test_chat_sources_frame.py::_collect` does — that file's own docstring is the
    reference for why only retrieval/persistence/the provider are stubbed.
    """
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    monkeypatch.setattr(
        chat_service,
        "_prepare_context",
        lambda *_args, **_kwargs: ([], dict(meta), None, None),
    )
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)

    captured: dict = {}

    async def _fake_finalize(**kwargs):
        captured["turn"] = kwargs["turn"]

    monkeypatch.setattr(chat_service, "_finalize_turn", _fake_finalize)

    llm = _FakeLLM()
    frames: list[tuple[str, dict]] = []
    generator = chat_service.ChatService.stream_reply(
        conversation_id=1,
        conversation_uuid="conv-uuid",
        user_id=1,
        organization_id=None,
        question="What happened?",
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
    async for raw in generator:
        if raw.startswith(":"):
            continue
        name = raw.split("event: ", 1)[1].split("\n", 1)[0]
        payload = json.loads(raw.split("data: ", 1)[1].strip())
        frames.append((name, payload))

    return frames, captured["turn"]


def _frame(frames, name):
    return next((payload for event, payload in frames if event == name), None)


@pytest.mark.asyncio
async def test_a_search_backend_failure_emits_retrieval_failed_not_no_context(monkeypatch):
    """The FAILED variant is produced, not the empty-library variant (the brief's

    exact scenario: OpenSearch unreachable in the fixture).
    """
    frames, turn = await _collect(
        monkeypatch,
        meta={"retrieved": 0, "files_searched": "all", "retrieval_failed": True},
    )

    warning = _frame(frames, "warning")
    assert warning == {"code": "retrieval_failed", "retrieved": 0, "files_searched": "all"}
    assert turn.metadata["retrieval_failed"] is True
    assert "no_context" not in turn.metadata


@pytest.mark.asyncio
async def test_a_genuinely_empty_library_still_emits_no_context(monkeypatch):
    """The control: with the flag absent, the ordinary #438 behaviour is unchanged."""
    frames, turn = await _collect(monkeypatch, meta={"retrieved": 0, "files_searched": "all"})

    warning = _frame(frames, "warning")
    assert warning == {"code": "no_context", "retrieved": 0, "files_searched": "all"}
    assert turn.metadata["no_context"] is True
    assert "retrieval_failed" not in turn.metadata
