"""Per-user LLM spend for the admin (issue #790): tokens from the enforcement counters, dollars
estimated from ``usage_event``.

Tokens come from ``llm_token_budget`` (the numbers a limit is enforced against): today's
``user_day`` row, and the sum of the user's ``user_day`` rows this UTC month. The estimate comes
from ``usage_event`` token events of the month, grouped in SQL by user, provider, model and
locality and priced with :func:`app.services.chat.pricing.estimate_cost_usd`. A group with no
known price makes that user's figure incomplete (``cost_incomplete``) instead of a confident
$0. Only counts and money are returned: no conversation, file or prompt identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import cast
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.models.llm_token_budget import SCOPE_USER_DAY
from app.models.llm_token_budget import LLMTokenBudget
from app.models.usage_event import UsageEvent
from app.services.chat.pricing import estimate_cost_usd
from app.services.llm_budget import TOKEN_EVENT_TYPES
from app.services.llm_budget import period_bounds
from app.services.llm_budget import periods


@dataclass
class UserSpend:
    """One user's standing; ``estimated_cost_usd`` is ``None`` until something priced is seen."""

    user_id: int
    email: str
    full_name: str | None
    tokens_today: int = 0
    tokens_this_month: int = 0
    estimated_cost_usd: float | None = None
    cost_incomplete: bool = False


def _meta_tokens(key: str):
    return func.coalesce(func.sum(cast(UsageEvent.event_metadata[key].astext, BigInteger)), 0)


def per_user_spend(db: Session, now: datetime) -> list[UserSpend]:
    """Every user with LLM activity this month, heaviest first."""
    day, month = periods(now)
    month_start, month_end = period_bounds("instance_month", now)

    rows: dict[int, UserSpend] = {}

    def entry(user_id: int) -> UserSpend:
        if user_id not in rows:
            rows[user_id] = UserSpend(user_id=user_id, email="", full_name=None)
        return rows[user_id]

    counters = (
        db.query(LLMTokenBudget.user_id, LLMTokenBudget.period, LLMTokenBudget.used)
        .filter(
            LLMTokenBudget.scope == SCOPE_USER_DAY,
            LLMTokenBudget.period.like(f"{month}-%"),
        )
        .all()
    )
    for user_id, period, used in counters:
        spend = entry(int(user_id))
        spend.tokens_this_month += int(used)
        if period == day:
            spend.tokens_today += int(used)

    meta = UsageEvent.event_metadata
    groups = (
        db.query(
            UsageEvent.user_id,
            meta["provider"].astext,
            meta["model"].astext,
            cast(meta["local"].astext, Boolean),
            _meta_tokens("prompt_tokens"),
            _meta_tokens("completion_tokens"),
            _meta_tokens("cache_read_tokens"),
            _meta_tokens("cache_write_tokens"),
        )
        .filter(
            UsageEvent.user_id.isnot(None),
            UsageEvent.event_type.in_(TOKEN_EVENT_TYPES),
            UsageEvent.created_at >= month_start,
            UsageEvent.created_at < month_end,
        )
        .group_by(
            UsageEvent.user_id,
            meta["provider"].astext,
            meta["model"].astext,
            cast(meta["local"].astext, Boolean),
        )
        .all()
    )
    for user_id, provider, model, local, prompt, completion, cache_read, cache_write in groups:
        spend = entry(int(user_id))
        cost = estimate_cost_usd(
            provider=provider or "unknown",
            model=model or "unknown",
            prompt_tokens=int(prompt),
            completion_tokens=int(completion),
            cache_read_tokens=int(cache_read),
            cache_write_tokens=int(cache_write),
            local=local,
        )
        if cost is None:
            spend.cost_incomplete = True
        else:
            spend.estimated_cost_usd = float((spend.estimated_cost_usd or 0) + float(cost))

    users = db.query(models.User).filter(models.User.id.in_(list(rows))).all() if rows else []
    for user in users:
        rows[user.id].email = str(user.email)
        rows[user.id].full_name = user.full_name
    return sorted(rows.values(), key=lambda s: (-s.tokens_this_month, s.user_id))
