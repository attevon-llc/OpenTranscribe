"""Concurrent reservations can never take more tokens than the limit allows (issue #790).

Real PostgreSQL connections, one per thread, all released at once by a barrier: the check
and the increment are one conditional ``UPDATE``, so the row lock serialises the writers and
each re-evaluates the limit against the committed total (PostgreSQL docs, "Read Committed
Isolation Level"). A read-then-write implementation admits more than the limit here.

The counters live under a far-future month unique to each test and are deleted afterwards;
the limits are patched in-process rather than written to ``system_settings``, so tests on
other workers sharing the database never see a limit.

The racers get their own ``NullPool`` engine. Through the shared app engine every racer's
connection would stay pooled for the rest of the xdist worker's life (QueuePool keeps up to
``DB_POOL_SIZE`` idle), which under ``-n auto`` took the test database past
``max_connections`` and failed unrelated modules with "too many clients already".
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.db.base import SessionLocal
from app.db.base import engine as app_engine
from app.services import llm_budget


@pytest.fixture
def private_month(monkeypatch):
    """A month no other test or deployment uses, and the instance limit for it."""
    year = 2100 + uuid.uuid4().int % 800
    now = datetime(year, 6, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        llm_budget,
        "read_limits",
        lambda _db: llm_budget.BudgetLimits(instance_monthly_tokens=100),
    )
    racers = create_engine(app_engine.url, poolclass=NullPool)
    monkeypatch.setattr(llm_budget, "_session", sessionmaker(bind=racers))
    # The counter exists before the race, so the writers meet on the row itself rather than
    # queueing behind each other's first INSERT of it.
    with SessionLocal() as db:
        db.execute(
            text(
                "INSERT INTO llm_token_budget (scope, user_id, period, used) "
                "VALUES ('instance_month', NULL, :p, 0)"
            ),
            {"p": now.strftime("%Y-%m")},
        )
        db.commit()
    yield now
    racers.dispose()
    with SessionLocal() as db:
        db.execute(
            text("DELETE FROM llm_token_budget WHERE period = :p"), {"p": now.strftime("%Y-%m")}
        )
        db.commit()


def _race(now: datetime, threads: int, amount: int) -> tuple[int, int]:
    barrier = threading.Barrier(threads)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            llm_budget.reserve(user_id=None, amount=amount, now=now)
            outcome = "admitted"
        except llm_budget.LLMBudgetExceededError as refused:
            # Recorded, not asserted here: an assert in a thread only kills the thread.
            outcome = refused.code
        with lock:
            outcomes.append(outcome)

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join(timeout=60)
    with SessionLocal() as db:
        used = db.execute(
            text(
                "SELECT used FROM llm_token_budget WHERE scope = 'instance_month' AND period = :p"
            ),
            {"p": now.strftime("%Y-%m")},
        ).scalar()
    assert len(outcomes) == threads
    assert set(outcomes) <= {"admitted", llm_budget.CODE_EXCEEDED}, outcomes
    return outcomes.count("admitted"), int(used or 0)


def test_two_calls_cannot_both_take_the_last_tokens(private_month):
    admitted, used = _race(private_month, threads=2, amount=60)
    assert admitted == 1
    assert used == 60


def test_many_concurrent_calls_never_overshoot_the_limit(private_month):
    admitted, used = _race(private_month, threads=12, amount=30)
    assert admitted == 3
    assert used == 90
