"""`_persist_reply` bounds `msg_metadata` before it reaches the message row.

`ChatMessage.msg_metadata` is an unbounded JSONB column and a turn's diagnostics (query
plan, per-speaker resolution) can grow without limit, so the persisted blob goes through
`cap_msg_metadata`. These tests need no database: the session is a stand-in that records
what was added.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core.chat_metadata_budget import CAPPED_MARKER_KEY
from app.core.chat_metadata_budget import DEFAULT_MAX_METADATA_BYTES
from app.core.chat_metadata_budget import DROPPED_KEYS_KEY
from app.core.enums import LLMProvider
from app.services.chat.persistence import _persist_reply
from app.services.chat.turn import ChatTurn


class _FakeConfig:
    provider = LLMProvider.OPENAI
    model = "m"


class _FakeLLM:
    config = _FakeConfig()

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


def _persisted_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    turn = ChatTurn()
    turn.answer_parts.append("answer")
    turn.finish_reason = "stop"
    turn.metadata = metadata

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        title="t", last_message_at=None
    )

    @contextmanager
    def _scope():
        yield db

    llm = _FakeLLM()
    with patch("app.db.session_utils.session_scope", _scope):
        _persist_reply(
            conversation_id=1,
            assistant_message_uuid="0198c000-0000-7000-8000-000000000009",
            turn=turn,
            used_citations=[],
            total_tokens=1,
            llm=llm,
            is_first_exchange=False,
            question="q",
        )
    (message,) = db.add.call_args.args
    persisted: dict[str, Any] | None = message.msg_metadata
    return persisted


@pytest.mark.unit
def test_oversize_container_is_dropped_and_scalars_survive() -> None:
    result = _persisted_metadata(
        {"retrieved": 12, "plan": [{"step": "x" * 200}] * 200, "small": {"a": 1}}
    )

    assert result is not None
    assert result["retrieved"] == 12
    assert result["small"] == {"a": 1}
    assert "plan" not in result
    assert result[CAPPED_MARKER_KEY] is True
    assert result[DROPPED_KEYS_KEY] == ["plan"]


@pytest.mark.unit
def test_ordinary_metadata_is_persisted_unchanged() -> None:
    metadata = {"retrieved": 3, "legs": ["a", "b"]}

    assert _persisted_metadata(metadata) == metadata
    assert len(str(metadata)) < DEFAULT_MAX_METADATA_BYTES
