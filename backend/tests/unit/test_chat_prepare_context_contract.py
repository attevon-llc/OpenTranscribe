"""``_prepare_context``'s two contracts with its callers, both of which broke silently.

Neither of these is a hypothetical. Both shapes cost real debugging time on the branch
that introduced them, and both are invisible at the point of failure:

1. **``assistant_message_uuid`` defaults to ``""``**, so a caller that omits it type-checks,
   runs, and quietly performs no cancellation check at all. The default exists for the
   many tests that drive this function directly (masking, routing, the speaker map) and
   have no turn id to thread through — see the parameter's own docstring in
   ``service.py``. It must never become how the *production* caller behaves, because
   ``POST /cancel`` stopping the extra LLM calls mid-fan-out depends on the real id
   reaching ``_check_cancelled`` at every phase boundary and leg submission.

2. **The return is a SIX-tuple** and six test modules stub it with a lambda that has to
   copy that arity by hand. A stale double does **not** surface as an error: the unpack
   raises inside ``stream_reply``, whose broad ``except`` converts it into a
   ``provider_error`` frame, so the test under it fails as *"the warning frame was never
   emitted"* or *"the redactor produced empty output"* — pointing at the streaming path,
   the SSE contract, or the output redactor, none of which are wrong. That misdirection is
   what these tests exist to stop: a mismatched double now fails HERE, by name.

Metering deliberately does **not** appear in (1). The planner/enrichment/rewrite tokens
reach ``record_chat_usage`` through ``meta["extra_llm_prompt_tokens"]`` /
``meta["extra_llm_completion_tokens"]``, which ``stream_reply`` folds onto the turn — the
uuid is not the carrier, so an omitted uuid cannot lose usage recording. That is asserted
below rather than asserted *about*, because it is the half of the "is a default safe?"
question that is easy to get wrong from the parameter name alone.
"""

from __future__ import annotations

import ast
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.services.chat import service as chat_service
from app.services.chat.redactor import MaskedChunk
from app.services.chat.settings import ChatSettings
from app.services.llm_stream import LLMStreamEvent
from app.services.search.chunk_retrieval import ChunkHit

_BACKEND = Path(__file__).resolve().parents[2]
_SERVICE = _BACKEND / "app" / "services" / "chat" / "service.py"
_TESTS = _BACKEND / "tests"

#: The turn id the production caller must thread through. Deliberately not the empty
#: string and not a value any other test uses.
_TURN_UUID = "00000000-0000-0000-0000-0000000000cc"


@contextmanager
def _null_session():
    yield None


class _FakeProvider:
    value = "custom"


class _FakeConfig:
    provider = _FakeProvider()
    model = "test-model"


class _FakeLLM:
    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.user_context_window = 32000
        self.response_tokens = 4000

    def chat_completion_stream(self, messages, cancel_event=None, **_kwargs):
        yield LLMStreamEvent(type="delta", text="An answer citing [1].")
        yield LLMStreamEvent(type="done", finish_reason="stop")

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


def _chunk() -> MaskedChunk:
    return MaskedChunk(
        source=ChunkHit(
            file_uuid="11111111-1111-1111-1111-000000000001",
            file_id=1,
            chunk_index=1,
            content="we agreed on four buttons",
            title="Recording 1",
            speaker="Dana",
            start_time=60.0,
            end_time=90.0,
        ),
        content="we agreed on four buttons",
    )


# ---------------------------------------------------------------------------
# Contract 1: the production caller passes a REAL id
# ---------------------------------------------------------------------------


async def _run_turn(monkeypatch) -> dict:
    """Drive the real ``stream_reply`` and return the kwargs ``_prepare_context`` saw."""
    seen: dict = {}
    extra_tokens = {"extra_llm_prompt_tokens": 11, "extra_llm_completion_tokens": 7}

    def _capture(*_args, **kwargs):
        seen.update(kwargs)
        meta = {"retrieved": 1, "files_searched": "all", **extra_tokens}
        return [_chunk()], meta, None, None, "", ""

    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    monkeypatch.setattr(chat_service, "_prepare_context", _capture)
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)

    async def _fake_finalize(**kwargs):
        seen["turn"] = kwargs["turn"]

    monkeypatch.setattr(chat_service, "_finalize_turn", _fake_finalize)

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
        llm=_FakeLLM(),
        assistant_message_uuid=_TURN_UUID,
        user_message_uuid="00000000-0000-0000-0000-0000000000bb",
        is_first_exchange=True,
    )
    frames = []
    async for raw in generator:
        if raw.startswith(":"):
            continue
        name = raw.split("event: ", 1)[1].split("\n", 1)[0]
        frames.append((name, json.loads(raw.split("data: ", 1)[1].strip())))
    seen["frames"] = frames
    return seen


@pytest.mark.asyncio
async def test_the_real_streaming_caller_passes_the_turns_assistant_message_uuid(monkeypatch):
    """THE pin the ``= ""`` default needs: production never takes the default.

    Without this, ``_prepare_context``'s cancellation checks silently degrade to reading a
    Redis key no turn will ever set, and ``POST /cancel`` stops aborting the planner and
    enrichment legs — with no error, no log line, and a turn that still answers.
    """
    seen = await _run_turn(monkeypatch)

    assert "assistant_message_uuid" in seen, (
        "stream_reply no longer passes assistant_message_uuid at all — it is now taking "
        "the test-only default, so every phase-boundary cancel check reads a dead key."
    )
    assert seen["assistant_message_uuid"] == _TURN_UUID
    assert seen["assistant_message_uuid"] != "", "the production caller took the default"
    # The frames still went out: the capture above is a real turn, not a broken one.
    assert any(name == "done" for name, _ in seen["frames"])


@pytest.mark.asyncio
async def test_the_uuid_is_not_how_extra_llm_usage_reaches_metering(monkeypatch):
    """The control for the decision to default the parameter.

    "It is needed for cancellation *and* metering" is the reading that makes a default
    look unsafe. Metering does not travel on the uuid — the planner/enrichment/rewrite
    tokens ride ``meta``, which ``stream_reply`` folds onto the turn before
    ``_finalize_turn`` calls ``record_chat_usage``. So an omitted uuid can cost a
    cancellation check and cannot cost a usage record.
    """
    seen = await _run_turn(monkeypatch)
    turn = seen["turn"]

    assert turn.extra_prompt_tokens == 11
    assert turn.extra_completion_tokens == 7


def test_omitting_the_uuid_fails_open_rather_than_reading_a_real_turns_cancel_flag(monkeypatch):
    """What the default actually does, executed rather than described.

    ``limits.is_cancelled("")`` is the whole safety argument for the default: an omitted
    id can only ever probe a key no turn will ever write, which fails OPEN exactly like a
    Redis outage does elsewhere in this module. If a refactor made the empty id collide
    with a real key — a prefix-only lookup, say — this goes red.
    """
    probed: list[str] = []

    def _record(message_uuid: str) -> bool:
        probed.append(message_uuid)
        return False

    monkeypatch.setattr(chat_service.limits, "is_cancelled", _record)
    chat_service._check_cancelled("")

    assert probed == [""], "the default no longer probes the empty key"


# ---------------------------------------------------------------------------
# Contract 2: every hand-written double copies the real arity
# ---------------------------------------------------------------------------


def _real_return_arity() -> int:
    """Element count of ``_prepare_context``'s annotated ``tuple[...]`` return.

    Read from the source with ``ast`` rather than from ``typing.get_type_hints``: the
    annotation names dataclasses from three modules, and a resolution failure would make
    this guard skip rather than fail — the silent-zero shape ``scripts/audit-tests.py``
    exists to catch.
    """
    tree = ast.parse(_SERVICE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_prepare_context":
            annotation = node.returns
            assert isinstance(annotation, ast.Subscript), (
                "_prepare_context's return annotation is no longer a subscripted tuple[...]"
            )
            elements = annotation.slice
            assert isinstance(elements, ast.Tuple), "return annotation is not a tuple[...]"
            return len(elements.elts)
    raise AssertionError("_prepare_context not found in service.py")


def _doubles() -> list[tuple[str, int, int]]:
    """``(location, tuple_length, line)`` for every stubbed ``_prepare_context`` in tests.

    Matches ``monkeypatch.setattr(..., "_prepare_context", <lambda returning a tuple>)``
    in either of the two spellings the tree uses (three positional args, or the two-arg
    form with the target module bound first).
    """
    found: list[tuple[str, int, int]] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        if '"_prepare_context"' not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            names = [
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and arg.value == "_prepare_context"
            ]
            if not names:
                continue
            for arg in node.args:
                if isinstance(arg, ast.Lambda) and isinstance(arg.body, ast.Tuple):
                    rel = path.relative_to(_TESTS).as_posix()
                    found.append((rel, len(arg.body.elts), arg.lineno))
    return found


def test_the_double_sweep_finds_the_known_stubs() -> None:
    """Guard on the guard: a sweep matching nothing would pass every arity."""
    locations = {location for location, _, _ in _doubles()}
    for expected in (
        "unit/test_chat_sources_frame.py",
        "unit/test_chat_output_redaction.py",
        "unit/test_chat_reasoning_sse_frames.py",
        "unit/test_chat_retrieval_failed.py",
        "unit/test_chat_speaker_resolver_wiring.py",
        "test_chat_language_scope.py",
    ):
        assert expected in locations, (
            f"the sweep no longer reaches the _prepare_context double in {expected} — "
            "either the stub moved to a shape this AST match does not cover (in which "
            "case widen it), or the module stopped stubbing it (drop the entry)."
        )


def test_every_prepare_context_double_returns_the_real_arity() -> None:
    """A stale double is invisible at the failure site — make it visible here.

    ``stream_reply``'s broad ``except`` turns the unpack error into a ``provider_error``
    frame, so the test that actually goes red reports a missing warning frame or an empty
    redacted answer. Two lanes chased that misdirection into the streaming path and the
    output redactor before finding a four-tuple lambda.
    """
    expected = _real_return_arity()
    wrong = [
        f"{location}:{line} returns {length} values, _prepare_context returns {expected}"
        for location, length, line in _doubles()
        if length != expected
    ]
    assert not wrong, (
        "These test doubles no longer match _prepare_context's return arity. A mismatch "
        "does NOT fail where it is written — the unpack raises inside stream_reply and "
        "is swallowed into a provider_error frame: " + "; ".join(wrong)
    )
