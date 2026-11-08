"""The LLM budget check is one indexed UPDATE once a period's counter exists (audit M6).

Every reservation used to run ``INSERT ... SELECT SUM(quantity) FROM usage_event ...
ON CONFLICT DO NOTHING`` before its conditional UPDATE: PostgreSQL evaluates the SELECT
before it discovers the conflict, so every LLM call on a deployment with a limit set
summed a whole month of usage events. The UPDATE now runs first and the seed runs only
when there is no counter yet.

Real PostgreSQL, a far-future month unique to each test, limits patched in-process (the
same isolation as ``test_llm_token_limits_race.py``).
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.db.base import SessionLocal
from app.db.base import engine as app_engine
from app.models.usage_event import UsageEvent
from app.services import llm_budget
from tests.query_budget import count_statements

LIMIT = 100


@pytest.fixture
def month(monkeypatch):
    year = 2100 + uuid.uuid4().int % 800
    now = datetime(year, 6, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        llm_budget,
        "read_limits",
        lambda _db: llm_budget.BudgetLimits(instance_monthly_tokens=LIMIT),
    )
    engine = create_engine(app_engine.url, poolclass=NullPool)
    monkeypatch.setattr(llm_budget, "_session", sessionmaker(bind=engine))
    yield now, engine
    engine.dispose()
    start, end = llm_budget.period_bounds(llm_budget.SCOPE_INSTANCE_MONTH, now)
    with SessionLocal() as db:
        db.execute(
            text("DELETE FROM llm_token_budget WHERE period = :p"), {"p": now.strftime("%Y-%m")}
        )
        db.execute(
            text("DELETE FROM usage_event WHERE created_at >= :s AND created_at < :e"),
            {"s": start, "e": end},
        )
        db.commit()


def _record_usage(now: datetime, tokens: int) -> None:
    with SessionLocal() as db:
        db.add(
            UsageEvent(
                event_type=llm_budget.TOKEN_EVENT_TYPES[0],
                quantity=Decimal(tokens),
                unit="tokens",
                created_at=now,
            )
        )
        db.commit()


def _counter(now: datetime) -> int | None:
    with SessionLocal() as db:
        return db.execute(
            text(
                "SELECT used FROM llm_token_budget WHERE scope = 'instance_month' AND period = :p"
            ),
            {"p": now.strftime("%Y-%m")},
        ).scalar()


def test_a_call_against_an_existing_counter_runs_one_update_and_no_sum(month):
    now, engine = month
    llm_budget.reserve(user_id=None, amount=10, now=now)  # first call: creates the counter

    with count_statements(engine, include_app_engine=False) as log:
        llm_budget.reserve(user_id=None, amount=10, now=now)

    assert log.matching(r"\bSUM\s*\(") == [], log.describe()
    budget = log.matching(r"llm_token_budget")
    assert len(budget) == 1 and budget[0].lstrip().upper().startswith("UPDATE"), log.describe()
    assert _counter(now) == 20


def test_a_refused_call_does_not_reseed_the_counter(month):
    now, engine = month
    llm_budget.reserve(user_id=None, amount=LIMIT, now=now)

    with (
        count_statements(engine, include_app_engine=False) as log,
        pytest.raises(llm_budget.LLMBudgetExceededError),
    ):
        llm_budget.reserve(user_id=None, amount=1, now=now)

    assert log.matching(r"\bSUM\s*\(") == [], log.describe()
    assert _counter(now) == LIMIT


def test_the_first_call_of_a_period_seeds_from_usage_already_recorded(month):
    now, _ = month
    _record_usage(now, 40)

    llm_budget.reserve(user_id=None, amount=10, now=now)

    assert _counter(now) == 50


def test_racing_first_calls_seed_once_and_never_overshoot(month):
    """No counter yet: every racer takes the seed path at once."""
    now, _ = month
    _record_usage(now, 40)
    threads = 12
    barrier = threading.Barrier(threads)
    outcomes: list[str] = []
    guard = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            llm_budget.reserve(user_id=None, amount=20, now=now)
            outcome = "admitted"
        except llm_budget.LLMBudgetExceededError as refused:
            outcome = refused.code
        with guard:
            outcomes.append(outcome)

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join(timeout=60)

    assert len(outcomes) == threads
    assert set(outcomes) <= {"admitted", llm_budget.CODE_EXCEEDED}, outcomes
    assert outcomes.count("admitted") == 3  # 40 seeded + 3 x 20 = 100
    assert _counter(now) == LIMIT
