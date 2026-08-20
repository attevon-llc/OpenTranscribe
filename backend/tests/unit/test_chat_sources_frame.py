"""The ``sources`` SSE frame must describe the prompt, not the retrieval result.

Issue #384. The frame used to be emitted immediately after retrieval, *before*
``build_messages`` computed the excerpt budget, so the UI could render clickable
citations for excerpts that were dropped and never reached the model. The answer
then looked sourced while being grounded in nothing.

These tests drive the real ``ChatService.stream_reply`` generator — retrieval and
the provider are the only things stubbed — and assert the invariant end to end:
**every citation put on the wire corresponds to an excerpt in the prompt.**
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from app.services.chat import service as chat_service
from app.services.chat.redactor import MaskedChunk
from app.services.chat.settings import ChatSettings
from app.services.llm_stream import LLMStreamEvent
from app.services.search.chunk_retrieval import ChunkHit


def _chunk(index: int, content: str) -> MaskedChunk:
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


class _FakeProvider:
    value = "custom"


class _FakeConfig:
    provider = _FakeProvider()
    model = "test-model"


class _FakeLLM:
    """Minimal stand-in for LLMService, recording the prompt it was given."""

    def __init__(self, *, context_window: int, response_tokens: int = 4000):
        self.config = _FakeConfig()
        self.user_context_window = context_window
        self.response_tokens = response_tokens
        self.sent_messages: list[dict[str, str]] | None = None

    def chat_completion_stream(self, messages, cancel_event=None, **_kwargs):
        self.sent_messages = messages
        yield LLMStreamEvent(type="delta", text="An answer citing [1].")
        yield LLMStreamEvent(type="done", finish_reason="stop")

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


@contextmanager
def _null_session():
    yield None


async def _collect(
    monkeypatch, *, chunks, context_window, history=None, use_context=True, meta=None
):
    """Run one turn and return (parsed frames, the ChatTurn, the fake LLM).

    Only retrieval, persistence and the provider are stubbed. Prompt assembly,
    budgeting, citation construction and the frame ordering all run for real.

    ``meta`` overrides the retrieval diagnostics ``_prepare_context`` would have
    produced. It defaults to the consistent pair (one chunk retrieved per chunk
    returned, scope "all"); pass it explicitly to model the case where retrieval
    found chunks and masking then dropped every one of them.
    """
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    diagnostics = {"retrieved": len(chunks), "files_searched": "all"} if meta is None else meta
    monkeypatch.setattr(
        chat_service,
        "_prepare_context",
        # Six values since #403 W2.6: the counted-aggregation result and the
        # map-reduce overview stay None for a turn the router left on the chunk
        # plane — which is every turn these tests drive — and the fan-out's
        # `<synthesis>`/`<recurrence>` blocks stay empty for the same reason
        # (that tier never engages on the serial pipeline).
        lambda *_args, **_kwargs: (list(chunks), dict(diagnostics), None, None, "", ""),
    )
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)

    captured: dict = {}

    async def _fake_finalize(**kwargs):
        captured["turn"] = kwargs["turn"]
        captured["messages"] = kwargs["messages"]

    monkeypatch.setattr(chat_service, "_finalize_turn", _fake_finalize)

    llm = _FakeLLM(context_window=context_window)
    frames: list[tuple[str, dict]] = []
    generator = chat_service.ChatService.stream_reply(
        conversation_id=1,
        conversation_uuid="conv-uuid",
        user_id=1,
        organization_id=None,
        question="What did the team decide?",
        history=history or [],
        file_uuids=None,
        speakers=[],
        settings=ChatSettings(),
        use_context=use_context,
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
        if raw.startswith(":"):  # keepalive comment
            continue
        name = raw.split("event: ", 1)[1].split("\n", 1)[0]
        payload = json.loads(raw.split("data: ", 1)[1].strip())
        frames.append((name, payload))

    return frames, captured["turn"], llm


def _frame(frames, name):
    return next((payload for event, payload in frames if event == name), None)


@pytest.mark.asyncio
async def test_citations_match_the_excerpts_that_reached_the_prompt(monkeypatch):
    """The pin issue #384 asks for: len(offered_citations) == chunks_used."""
    chunks = [_chunk(i, f"chunk {i} " + "word " * 400) for i in range(1, 15)]
    frames, turn, llm = await _collect(monkeypatch, chunks=chunks, context_window=8192)

    sources = _frame(frames, "sources")
    assert sources is not None
    assert len(sources["citations"]) == turn.metadata["chunks_used"]
    # The budget must actually have bitten, or the assertion above proves nothing.
    assert 0 < turn.metadata["chunks_used"] < len(chunks)

    prompt = "".join(m["content"] for m in llm.sent_messages)
    for citation in sources["citations"]:
        assert f'<excerpt id="{citation["id"]}"' in prompt


@pytest.mark.asyncio
async def test_no_citation_is_offered_for_a_dropped_excerpt(monkeypatch):
    """The specific regression: a dropped excerpt must not appear as a source."""
    chunks = [_chunk(i, f"chunk {i} " + "word " * 400) for i in range(1, 15)]
    frames, turn, llm = await _collect(monkeypatch, chunks=chunks, context_window=8192)

    prompt = "".join(m["content"] for m in llm.sent_messages)
    offered_ids = {c["id"] for c in _frame(frames, "sources")["citations"]}
    dropped_ids = set(range(1, len(chunks) + 1)) - offered_ids

    assert dropped_ids, "expected the budget to drop at least one excerpt"
    for excerpt_id in dropped_ids:
        assert f'<excerpt id="{excerpt_id}"' not in prompt


@pytest.mark.asyncio
async def test_sources_frame_is_emitted_after_the_budget_is_known(monkeypatch):
    """Ordering is the fix: sources cannot precede prompt assembly.

    ``generating`` is emitted immediately after ``build_messages``, so a
    ``sources`` frame that still precedes it proves the budget was applied first.
    """
    chunks = [_chunk(1, "short and relevant")]
    frames, _turn, _llm = await _collect(monkeypatch, chunks=chunks, context_window=32_000)

    order = [name for name, _payload in frames]
    generating_at = next(
        i for i, (n, p) in enumerate(frames) if n == "status" and p["stage"] == "generating"
    )
    retrieving_at = next(
        i for i, (n, p) in enumerate(frames) if n == "status" and p["stage"] == "retrieving"
    )
    # sources sits between retrieval and generation — after the prompt is built,
    # before the first token, so the UI still shows what is being consulted.
    assert retrieving_at < order.index("sources") < generating_at
    assert order.index("sources") < order.index("delta")


@pytest.mark.asyncio
async def test_dropping_every_excerpt_warns_instead_of_answering_silently(monkeypatch):
    """Retrieved-but-unusable context is reported, not absorbed.

    A window this small leaves no budget once the system prompt, history and
    question are paid for, so nothing reaches the prompt. The user must be told
    rather than shown a normal-looking answer.
    """
    chunks = [_chunk(i, f"chunk {i} " + "word " * 400) for i in range(1, 4)]
    history = [
        {"role": "user", "content": "earlier question " * 200},
        {"role": "assistant", "content": "earlier answer " * 200},
    ]
    frames, turn, llm = await _collect(
        monkeypatch, chunks=chunks, context_window=2048, history=history
    )

    assert turn.metadata["chunks_used"] == 0
    assert turn.metadata["context_dropped"] is True
    # The two codes name different defects and must never both fire: excerpts
    # existed here, so "nothing was searched" would be a lie.
    assert "no_context" not in turn.metadata
    assert _frame(frames, "sources")["citations"] == []

    warning = _frame(frames, "warning")
    assert warning == {"code": "context_dropped", "retrieved": len(chunks)}

    prompt = "".join(m["content"] for m in llm.sent_messages)
    assert "<excerpt" not in prompt


@pytest.mark.asyncio
async def test_no_warning_when_every_excerpt_fits(monkeypatch):
    """The warning must stay rare enough to mean something."""
    chunks = [_chunk(1, "short and relevant"), _chunk(2, "also short")]
    frames, turn, _llm = await _collect(monkeypatch, chunks=chunks, context_window=32_000)

    assert turn.metadata["chunks_used"] == 2
    assert "context_dropped" not in turn.metadata
    assert "no_context" not in turn.metadata
    assert _frame(frames, "warning") is None
    assert len(_frame(frames, "sources")["citations"]) == 2


# ---------------------------------------------------------------------------
# Zero excerpts: an answer built from nothing must not look like a grounded one
# (issue #438)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_answer_built_from_no_excerpts_warns(monkeypatch):
    """The #438 defect: retrieval returned nothing and nobody was told.

    ``retrieve_chunks`` degrades to ``[]`` on **any** failure — an OpenSearch
    503 mid-reindex produced exactly this, and the model's "I don't have enough
    information" was indistinguishable from a grounded negative over a corpus
    full of matching material. The counters are the only evidence available, so
    they have to leave the server.
    """
    frames, turn, llm = await _collect(
        monkeypatch,
        chunks=[],
        context_window=32_000,
        meta={"retrieved": 0, "files_searched": "all"},
    )

    assert turn.metadata["chunks_used"] == 0
    assert turn.metadata["no_context"] is True
    assert _frame(frames, "sources")["citations"] == []
    assert _frame(frames, "warning") == {
        "code": "no_context",
        "retrieved": 0,
        "files_searched": "all",
    }
    # It is a warning about the answer, not a refusal to produce one.
    assert "<excerpt" not in "".join(m["content"] for m in llm.sent_messages)
    assert turn.answer


@pytest.mark.asyncio
async def test_the_warning_distinguishes_an_empty_search_from_fail_closed_masking(monkeypatch):
    """``retrieved`` separates the two ways a turn can reach zero excerpts.

    Masking fails CLOSED, so a chunk that cannot be masked contributes ``""``
    and is dropped — retrieval found material, and none of it was usable. That
    is a redaction-configuration problem, not an empty index, and a single
    undifferentiated "no context" would send the reader after the wrong one.
    """
    frames, turn, _llm = await _collect(
        monkeypatch,
        chunks=[],
        context_window=32_000,
        meta={
            "retrieved": 5,
            "files_searched": 3,
            "chunks_dropped_empty_after_masking": 5,
        },
    )

    assert turn.metadata["no_context"] is True
    assert _frame(frames, "warning") == {
        "code": "no_context",
        "retrieved": 5,
        "files_searched": 3,
    }


@pytest.mark.asyncio
async def test_no_context_mode_emits_neither_sources_nor_warning(monkeypatch):
    """Pure-LLM chat has no excerpts to drop, so neither frame applies."""
    frames, turn, _llm = await _collect(
        monkeypatch, chunks=[], context_window=32_000, use_context=False
    )

    assert _frame(frames, "sources") is None
    assert _frame(frames, "warning") is None
    assert turn.metadata["chunks_used"] == 0
