"""Per-user LLM usage — "what have I been using?"

A core, open-source surface. Anyone paying an LLM bill wants to see where it
went, and that is as true for a self-hoster with an OpenAI key as for a hosted
tenant. This endpoint reports: every recorded event type (chat, summaries,
topics, speaker identification, the LLM redaction detector, and any non-token
event), and the caller's standing against the token limits. Enforcing those
limits is ``services/llm_budget.py``'s job at the LLM call seam, not this one's.

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

from app.api.deps_context import get_current_context
from app.db.base import get_db
from app.models.usage_event import UsageEvent
from app.services.chat.pricing import RATES_VERIFIED_ON
from app.services.chat.pricing import estimate_cost_usd
from app.services.chat.usage import EVENT_TYPE_CHAT_TOKENS
from app.services.llm_budget import TOKEN_EVENT_TYPES
from app.services.llm_budget import status_for
from app.services.llm_call_events import FEATURE_OF_EVENT_TYPE
from app.services.request_context import RequestContext
from app.utils.db_helpers import org_stamp_is

logger = logging.getLogger(__name__)

router = APIRouter()

#: Bounded so a user cannot ask the database to scan an unbounded history.
MAX_WINDOW_DAYS = 365


def _model_key(meta: dict[str, Any]) -> tuple[str, str]:
    return str(meta.get("provider") or "unknown"), str(meta.get("model") or "unknown")


def _token_bucket(**identity: str) -> dict[str, Any]:
    return {
        **identity,
        "events": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": None,
    }


def _add_cost(bucket: dict[str, Any], cost: float) -> None:
    bucket["estimated_cost_usd"] = float((bucket["estimated_cost_usd"] or 0) + cost)


def _budget_view(db: Session, ctx: RequestContext) -> dict[str, Any]:
    """The caller's standing against the token limits (``0`` = unlimited).

    The instance's monthly total is not shown to a user (it spans every tenant), only
    whether the instance limit has been reached; admins read it at ``/admin/llm-budget``.
    """
    status = status_for(db, ctx.user.id)
    limits = status.limits
    return {
        "user_daily_tokens": limits.user_daily_tokens,
        "used_today": status.user_used_today,
        "day_resets_at": status.day_resets_at.isoformat(),
        "instance_limited": limits.instance_monthly_tokens > 0,
        "instance_limit_reached": limits.instance_monthly_tokens > 0
        and status.instance_used_this_month >= limits.instance_monthly_tokens,
        "month_resets_at": status.month_resets_at.isoformat(),
        "local_unlimited": limits.local_unlimited,
    }


@router.get("/me")
def get_my_usage(
    days: int = Query(30, ge=1, le=MAX_WINDOW_DAYS),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> dict[str, Any]:
    """Summarize the caller's own usage in the active tenant over a trailing window.

    Every event type is returned in ``by_event_type`` (count and quantity in its own
    unit). LLM token events (``chat.tokens``, ``summary.tokens``, ``topics.tokens``,
    ``speaker_id.tokens``, ``redaction.tokens``) are also broken down in ``totals``,
    ``by_feature`` and ``by_model``. ``totals.messages`` counts chat exchanges only;
    ``totals.events`` counts every LLM token event.

    Costs are **estimates**. They are computed from a rate table that a vendor can
    change at any time, they ignore any negotiated discount, and they are omitted
    entirely for models with no known rate — an honest blank beats a confident
    $0.00. A model on this deployment is free only when its event recorded that it
    was local. ``rates_verified_on`` is returned so a stale table is visible.

    Aggregation happens in Python rather than SQL because the per-model breakdown
    lives in a JSONB column and the row count per user per month is small. If that
    stops being true, this becomes a grouped query over generated columns.
    """
    since = datetime.now(UTC) - timedelta(days=days)

    events = (
        db.query(UsageEvent)
        .filter(
            UsageEvent.user_id == ctx.user.id,
            org_stamp_is(UsageEvent.organization_id, ctx.org_id),
            UsageEvent.created_at >= since,
        )
        .all()
    )

    by_type: dict[str, dict[str, Any]] = {}
    by_feature: dict[str, dict[str, Any]] = {}
    by_model: dict[tuple[str, str], dict[str, Any]] = {}
    totals = {
        "messages": 0,
        "events": 0,
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
        event_type = str(event.event_type)
        kind = by_type.setdefault(
            event_type, {"event_type": event_type, "unit": event.unit, "events": 0, "quantity": 0.0}
        )
        kind["events"] += 1
        kind["quantity"] += float(event.quantity or 0)
        if event_type not in TOKEN_EVENT_TYPES:
            continue

        meta = event.event_metadata or {}
        key = _model_key(meta)
        model_bucket = by_model.setdefault(key, _token_bucket(provider=key[0], model=key[1]))
        model_bucket.setdefault("messages", 0)
        feature = FEATURE_OF_EVENT_TYPE.get(event_type, event_type)
        feature_bucket = by_feature.setdefault(feature, _token_bucket(event_type=feature))

        prompt = int(meta.get("prompt_tokens") or 0)
        completion = int(meta.get("completion_tokens") or 0)
        cache_read = int(meta.get("cache_read_tokens") or 0)
        cache_write = int(meta.get("cache_write_tokens") or 0)
        total = int(event.quantity or 0)

        for target in (model_bucket, feature_bucket, totals):
            target["events"] += 1
            target["prompt_tokens"] += prompt
            target["completion_tokens"] += completion
            target["cache_read_tokens"] += cache_read
            target["cache_write_tokens"] += cache_write
            target["total_tokens"] += total

        if event_type == EVENT_TYPE_CHAT_TOKENS:
            totals["messages"] += 1
            model_bucket["messages"] += 1
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
            local=meta.get("local") if isinstance(meta.get("local"), bool) else None,
        )
        if cost is None:
            any_unpriced = True
        else:
            _add_cost(model_bucket, float(cost))
            _add_cost(feature_bucket, float(cost))
            total_cost = float((total_cost or 0) + float(cost))

    return {
        "window_days": days,
        "since": since.isoformat(),
        "totals": totals,
        "by_event_type": sorted(by_type.values(), key=lambda row: row["event_type"]),
        "by_feature": sorted(
            by_feature.values(), key=lambda row: row["total_tokens"], reverse=True
        ),
        "by_model": sorted(by_model.values(), key=lambda row: row["total_tokens"], reverse=True),
        "estimated_cost_usd": total_cost,
        # True when at least one model had no known rate, so the UI can say the
        # total is partial rather than presenting it as complete.
        "cost_incomplete": any_unpriced,
        "rates_verified_on": RATES_VERIFIED_ON,
        "budget": _budget_view(db, ctx),
    }


@router.get("/me/daily")
def get_my_daily_usage(
    days: int = Query(30, ge=1, le=MAX_WINDOW_DAYS),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> dict[str, Any]:
    """Daily LLM token totals for the caller, over every LLM feature — the series behind a chart.

    Grouped in SQL: this one only needs the date, the event type and the quantity, all
    real columns, so there is no reason to pull every row into Python. ``messages`` counts
    chat exchanges, ``events`` every LLM token event, and ``by_event_type`` splits the day's
    tokens by feature.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    day = func.date_trunc("day", UsageEvent.created_at).label("day")

    rows = (
        db.query(
            day, UsageEvent.event_type, func.sum(UsageEvent.quantity), func.count(UsageEvent.id)
        )
        .filter(
            UsageEvent.user_id == ctx.user.id,
            org_stamp_is(UsageEvent.organization_id, ctx.org_id),
            UsageEvent.event_type.in_(TOKEN_EVENT_TYPES),
            UsageEvent.created_at >= since,
        )
        .group_by(day, UsageEvent.event_type)
        .order_by(day)
        .all()
    )

    series: dict[str, dict[str, Any]] = {}
    for when, event_type, quantity, count in rows:
        point = series.setdefault(
            when.date().isoformat(),
            {
                "day": when.date().isoformat(),
                "total_tokens": 0,
                "messages": 0,
                "events": 0,
                "by_event_type": {},
            },
        )
        tokens = int(quantity or 0)
        point["total_tokens"] += tokens
        point["events"] += int(count or 0)
        point["by_event_type"][str(event_type)] = tokens
        if event_type == EVENT_TYPE_CHAT_TOKENS:
            point["messages"] += int(count or 0)

    return {"window_days": days, "series": list(series.values())}
