"""Per-user LLM usage — "what have I been using?"

A core, open-source surface. Anyone paying an LLM bill wants to see where it
went, and that is as true for a self-hoster with an OpenAI key as for a hosted
tenant. Quotas, tiers and invoicing are deliberately absent: this endpoint
reports, it does not enforce.

Handlers are declared ``def``, not ``async def`` (issue #284 A2.5): they only do
blocking SQLAlchemy work and never ``await``, so as coroutines they would occupy
the event loop for the whole request.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.usage_event import UsageEvent
from app.models.user import User
from app.services.chat.pricing import RATES_VERIFIED_ON
from app.services.chat.pricing import estimate_cost_usd
from app.services.chat.usage import EVENT_TYPE_CHAT_TOKENS

logger = logging.getLogger(__name__)

router = APIRouter()

#: Bounded so a user cannot ask the database to scan an unbounded history.
MAX_WINDOW_DAYS = 365


def _model_key(meta: dict[str, Any]) -> tuple[str, str]:
    return str(meta.get("provider") or "unknown"), str(meta.get("model") or "unknown")


@router.get("/me")
def get_my_usage(
    days: int = Query(30, ge=1, le=MAX_WINDOW_DAYS),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Summarize the caller's own chat LLM usage over a trailing window.

    Costs are **estimates**. They are computed from a rate table that a vendor can
    change at any time, they ignore any negotiated discount, and they are omitted
    entirely for models with no known rate — an honest blank beats a confident
    $0.00. ``rates_verified_on`` is returned so a stale table is visible.

    Aggregation happens in Python rather than SQL because the per-model breakdown
    lives in a JSONB column and the row count per user per month is small. If that
    stops being true, this becomes a grouped query over generated columns.
    """
    since = datetime.now(UTC) - timedelta(days=days)

    events = (
        db.query(UsageEvent)
        .filter(
            UsageEvent.user_id == current_user.id,
            UsageEvent.event_type == EVENT_TYPE_CHAT_TOKENS,
            UsageEvent.created_at >= since,
        )
        .all()
    )

    by_model: dict[tuple[str, str], dict[str, Any]] = {}
    totals = {
        "messages": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "grounded_messages": 0,
        "estimated_token_messages": 0,
    }
    total_cost = None
    any_unpriced = False

    for event in events:
        meta = event.event_metadata or {}
        key = _model_key(meta)
        bucket = by_model.setdefault(
            key,
            {
                "provider": key[0],
                "model": key[1],
                "messages": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": None,
            },
        )

        prompt = int(meta.get("prompt_tokens") or 0)
        completion = int(meta.get("completion_tokens") or 0)
        cache_read = int(meta.get("cache_read_tokens") or 0)
        cache_write = int(meta.get("cache_write_tokens") or 0)
        total = int(event.quantity or 0)

        for target in (bucket, totals):
            target["messages"] += 1
            target["prompt_tokens"] += prompt
            target["completion_tokens"] += completion
            target["cache_read_tokens"] += cache_read
            target["cache_write_tokens"] += cache_write
            target["total_tokens"] += total

        if meta.get("use_context"):
            totals["grounded_messages"] += 1
        if meta.get("tokens_estimated"):
            totals["estimated_token_messages"] += 1

        cost = estimate_cost_usd(
            provider=key[0],
            model=key[1],
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        if cost is None:
            any_unpriced = True
        else:
            bucket["estimated_cost_usd"] = float((bucket["estimated_cost_usd"] or 0) + float(cost))
            total_cost = float((total_cost or 0) + float(cost))

    return {
        "window_days": days,
        "since": since.isoformat(),
        "totals": totals,
        "by_model": sorted(by_model.values(), key=lambda row: row["total_tokens"], reverse=True),
        "estimated_cost_usd": total_cost,
        # True when at least one model had no known rate, so the UI can say the
        # total is partial rather than presenting it as complete.
        "cost_incomplete": any_unpriced,
        "rates_verified_on": RATES_VERIFIED_ON,
    }


@router.get("/me/daily")
def get_my_daily_usage(
    days: int = Query(30, ge=1, le=MAX_WINDOW_DAYS),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Daily token totals for the caller — the series behind a usage chart.

    Grouped in SQL: this one only needs the date and the quantity, both of which
    are real columns, so there is no reason to pull every row into Python.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    day = func.date_trunc("day", UsageEvent.created_at).label("day")

    rows = (
        db.query(day, func.sum(UsageEvent.quantity), func.count(UsageEvent.id))
        .filter(
            UsageEvent.user_id == current_user.id,
            UsageEvent.event_type == EVENT_TYPE_CHAT_TOKENS,
            UsageEvent.created_at >= since,
        )
        .group_by(day)
        .order_by(day)
        .all()
    )

    return {
        "window_days": days,
        "series": [
            {
                "day": row[0].date().isoformat(),
                "total_tokens": int(row[1] or 0),
                "messages": int(row[2] or 0),
            }
            for row in rows
        ],
    }
