"""Chat retention sweep (issue #52).

This task DELETES user data, and the setting that drives it defaults to 0
("keep forever"). Two failure modes are worth guarding above all others: a
sweep that runs while retention is disabled, and a cutoff that takes
conversations it should have spared. Both destroy data silently — nobody
notices a thread that quietly stopped existing.

Needs Postgres (the dev stack, or the throwaway instance in tests/CLAUDE.md).
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.core.security import get_password_hash
from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.models.user import User
from app.tasks import chat_retention


def _user(db) -> User:
    uid = uuid_pkg.uuid4().hex[:8]
    user = User(
        email=f"retention_{uid}@example.com",
        full_name="Retention user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _conversation(db, user, *, age_days: float | None, created_days_ago: float = 0.0):
    """Create a conversation aged by ``age_days``.

    ``age_days=None`` leaves ``last_message_at`` NULL — a chat opened but never
    sent, which the sweep must age off ``created_at`` instead.
    """
    now = datetime.now(UTC)
    conversation = ChatConversation(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        title="Retention test",
        created_at=now - timedelta(days=created_days_ago),
        last_message_at=None if age_days is None else now - timedelta(days=age_days),
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def _run_sweep(db, monkeypatch, *, retention_days: int):
    """Run the sweep against the test session with a pinned retention window."""
    from contextlib import contextmanager

    @contextmanager
    def _session_scope():
        # Mirrors the real session_scope, which COMMITS on clean exit. Without
        # it the sweep's db.delete() calls are never flushed and every deletion
        # assertion below would pass vacuously. Safe under the savepoint
        # harness: commit() is intercepted and rolled back after the test.
        yield db
        db.commit()

    monkeypatch.setattr("app.db.session_utils.session_scope", _session_scope)

    class _Settings:
        pass

    settings = _Settings()
    settings.retention_days = retention_days  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "app.services.chat.settings.get_chat_settings", lambda _db: settings, raising=True
    )
    return chat_retention.chat_retention_sweep.run()


def _exists(db, conversation_id: int) -> bool:
    return (
        db.query(ChatConversation).filter(ChatConversation.id == conversation_id).first()
        is not None
    )


@pytest.fixture(autouse=True)
def _only_this_tests_rows_are_sweepable(db_session):
    """Age every pre-existing conversation forward, so the sweep can only see ours.

    The sweep selects by AGE across the WHOLE table — no user filter, no test
    scoping — so on a shared database every assertion on ``result["deleted"]``
    silently counts whatever real conversations happen to be older than the
    window too. Against a dev stack that is not a hypothetical: this file failed
    three tests with ``assert 9 == 1`` and ``assert 8 == 0`` purely because the
    database held 8 aged conversations of its own, and the cap test's second
    sweep found more rows waiting than the 2 it created. CI never noticed because
    it gets a fresh Postgres; the failures land only on a developer's machine,
    for a reason that has nothing to do with the code under test.

    A baseline-delta assertion would fix the first two but not the cap test,
    whose whole subject is how a bounded run splits a KNOWN population across
    ticks. Normalising the population is what makes all three independent of what
    the database happens to contain.

    Safe, and it does not touch real data: ``db_session`` runs each test inside a
    savepoint that is rolled back at teardown, and ``_run_sweep`` points the sweep
    at that same session — so these writes live and die inside the test, exactly
    like the deletions the sweep itself performs.
    """
    now = datetime.now(UTC)
    db_session.query(ChatConversation).update(
        {ChatConversation.created_at: now, ChatConversation.last_message_at: now},
        synchronize_session=False,
    )
    db_session.commit()


# --- the safety property -----------------------------------------------------


def test_disabled_retention_deletes_nothing(db_session, monkeypatch):
    """retention_days=0 is the DEFAULT. A sweep here must be a strict no-op."""
    user = _user(db_session)
    ancient = _conversation(db_session, user, age_days=3650)

    result = _run_sweep(db_session, monkeypatch, retention_days=0)

    assert result["status"] == "disabled"
    assert _exists(db_session, ancient.id), "a disabled sweep must never delete"


@pytest.mark.parametrize("retention_days", [-1, -30])
def test_negative_retention_is_treated_as_disabled(db_session, monkeypatch, retention_days):
    """A bad setting must fail safe, not sweep everything."""
    user = _user(db_session)
    ancient = _conversation(db_session, user, age_days=3650)

    result = _run_sweep(db_session, monkeypatch, retention_days=retention_days)

    assert result["status"] == "disabled"
    assert _exists(db_session, ancient.id)


# --- the cutoff --------------------------------------------------------------


def test_conversation_older_than_the_window_is_deleted(db_session, monkeypatch):
    user = _user(db_session)
    old = _conversation(db_session, user, age_days=45)

    result = _run_sweep(db_session, monkeypatch, retention_days=30)

    assert result["deleted"] == 1
    assert not _exists(db_session, old.id)


def test_conversation_inside_the_window_is_spared(db_session, monkeypatch):
    user = _user(db_session)
    recent = _conversation(db_session, user, age_days=10)

    result = _run_sweep(db_session, monkeypatch, retention_days=30)

    assert result["deleted"] == 0
    assert _exists(db_session, recent.id)


def test_the_boundary_is_not_off_by_a_day(db_session, monkeypatch):
    """Just inside the window survives; just outside does not."""
    user = _user(db_session)
    just_inside = _conversation(db_session, user, age_days=29.5)
    just_outside = _conversation(db_session, user, age_days=30.5)

    _run_sweep(db_session, monkeypatch, retention_days=30)

    assert _exists(db_session, just_inside.id), "29.5 days old with a 30-day window must survive"
    assert not _exists(db_session, just_outside.id)


def test_never_answered_conversation_ages_off_created_at(db_session, monkeypatch):
    """last_message_at is NULL for a chat opened but never sent.

    Without the created_at fallback these accumulate forever, which is the
    opposite of what enabling retention is for.
    """
    user = _user(db_session)
    stale_empty = _conversation(db_session, user, age_days=None, created_days_ago=90)
    fresh_empty = _conversation(db_session, user, age_days=None, created_days_ago=1)

    _run_sweep(db_session, monkeypatch, retention_days=30)

    assert not _exists(db_session, stale_empty.id)
    assert _exists(db_session, fresh_empty.id)


# --- collateral --------------------------------------------------------------


def test_messages_go_with_their_conversation(db_session, monkeypatch):
    """Orphaned messages would keep transcript quotes alive past the window."""
    user = _user(db_session)
    old = _conversation(db_session, user, age_days=60)
    db_session.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(),
            conversation_id=old.id,
            role="user",
            content="what did we decide about the contract?",
        )
    )
    db_session.commit()
    conversation_id = old.id

    _run_sweep(db_session, monkeypatch, retention_days=30)

    remaining = (
        db_session.query(ChatMessage).filter(ChatMessage.conversation_id == conversation_id).count()
    )
    assert remaining == 0


def test_a_run_is_capped_and_reports_truncation(db_session, monkeypatch):
    """A first sweep on a busy deployment must not delete unbounded rows at once."""
    monkeypatch.setattr(chat_retention, "MAX_DELETIONS_PER_RUN", 3)
    user = _user(db_session)
    for _ in range(5):
        _conversation(db_session, user, age_days=60)

    result = _run_sweep(db_session, monkeypatch, retention_days=30)

    assert result["deleted"] == 3
    assert result["truncated"] is True

    # The remainder is picked up next tick rather than lost.
    second = _run_sweep(db_session, monkeypatch, retention_days=30)
    assert second["deleted"] == 2
    assert second["truncated"] is False
