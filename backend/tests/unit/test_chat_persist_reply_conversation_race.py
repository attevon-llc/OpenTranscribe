"""A conversation deleted mid-stream must not surface as an unhandled error.

The e2e chat suite's `cleanup_conversations` fixture deletes every conversation
a test created as soon as the test itself finishes — it does not wait for a
still-in-flight SSE stream to settle. That produced
`chat_message_conversation_id_fkey` ForeignKeyViolation errors in the backend
log: `_persist_reply` unconditionally built and inserted a `ChatMessage`
against the now-deleted `conversation_id`.

App-side fix, not a test-side wait: a stream outliving the conversation it
writes to is also reachable through ordinary concurrent user action (a second
tab deleting the conversation mid-answer), so the persistence layer — the side
that actually owns the invariant "a message needs a conversation to belong
to" — is the one that should tolerate it, not every caller that can trigger
the race.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.services.chat.service import ChatTurn
from app.services.chat.service import _persist_reply


def _fake_llm() -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(provider=SimpleNamespace(value="openai"), model="test-model")
    )


def _turn(answer: str = "the answer") -> ChatTurn:
    turn = ChatTurn()
    turn.answer_parts.append(answer)
    turn.finish_reason = "stop"
    return turn


@pytest.fixture
def bridge_session_scope(db_session):
    """Point `_persist_reply`'s internal `session_scope()` at the test session.

    Mirrors `tests/test_chat_endpoints.py::stub_llm`'s bridge for the same
    reason: `_persist_reply` opens its own session rather than accepting one,
    and a genuinely separate connection under the savepoint harness cannot see
    rows this test has not committed to the outer transaction.
    """

    @contextmanager
    def _test_session_scope():
        yield db_session
        db_session.commit()

    with patch("app.db.session_utils.session_scope", _test_session_scope):
        yield db_session


@pytest.mark.unit
class TestConversationAlreadyGoneBeforePersistStarts:
    """The common case: the existence check finds nothing and skips the insert
    entirely, never reaching the database's own FK enforcement at all.
    """

    def test_returns_none_without_raising(self, bridge_session_scope):
        result = _persist_reply(
            conversation_id=999_999_999,  # never created in this test
            assistant_message_uuid="0198c000-0000-7000-8000-000000000001",
            turn=_turn(),
            used_citations=[],
            total_tokens=42,
            llm=_fake_llm(),
            is_first_exchange=True,
            question="does this survive a vanished conversation?",
        )

        assert result is None

    def test_writes_no_message_row(self, bridge_session_scope):
        _persist_reply(
            conversation_id=999_999_999,
            assistant_message_uuid="0198c000-0000-7000-8000-000000000002",
            turn=_turn(),
            used_citations=[],
            total_tokens=42,
            llm=_fake_llm(),
            is_first_exchange=True,
            question="does this survive a vanished conversation?",
        )

        assert (
            bridge_session_scope.query(ChatMessage)
            .filter_by(uuid="0198c000-0000-7000-8000-000000000002")
            .first()
            is None
        )


@pytest.mark.unit
class TestConversationDeletedInTheNarrowWindowBetweenCheckAndCommit:
    """The TOCTOU case: the existence check still finds the row (a real,
    persisted ChatConversation), but it is gone before the INSERT's own
    transaction commits — a REAL IntegrityError from Postgres, not a mocked
    one, proving the `except IntegrityError` branch actually catches the FK
    violation `chat_message_conversation_id_fkey` and degrades gracefully.
    """

    def test_a_real_foreign_key_violation_degrades_to_none(self, bridge_session_scope, monkeypatch):
        """The existence check itself cannot be raced within a single test
        process (a second real connection cannot see this savepoint-isolated
        session's uncommitted row at all, and racing two threads against one
        session is not supported by SQLAlchemy). So the check is stubbed to
        report "found" — standing in for a legitimate read that ran a moment
        before the delete — while the `conversation_id` it hands back names a
        row that was NEVER created. The `ChatMessage` INSERT then hits
        Postgres's real `chat_message_conversation_id_fkey` constraint: this
        proves the `except IntegrityError` branch actually catches that
        specific, real violation and degrades gracefully, not a mocked stand-in
        for one.
        """
        nonexistent_conversation_id = 999_999_998
        fake_conversation = SimpleNamespace(
            id=nonexistent_conversation_id, title=None, last_message_at=None
        )

        real_query = bridge_session_scope.query

        def _query_stub(model, *args, **kwargs):
            if model is ChatConversation:
                return SimpleNamespace(
                    filter=lambda *a, **k: SimpleNamespace(first=lambda: fake_conversation)
                )
            return real_query(model, *args, **kwargs)

        monkeypatch.setattr(bridge_session_scope, "query", _query_stub)

        result = _persist_reply(
            conversation_id=nonexistent_conversation_id,
            assistant_message_uuid="0198c000-0000-7000-8000-000000000003",
            turn=_turn(),
            used_citations=[],
            total_tokens=42,
            llm=_fake_llm(),
            is_first_exchange=True,
            question="does a mid-persist delete degrade gracefully?",
        )

        assert result is None
