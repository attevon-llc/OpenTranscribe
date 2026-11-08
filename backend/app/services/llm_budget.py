"""LLM token limits: a per-user daily and a per-instance monthly cap (issue #790, D13).

**Where it is enforced.** At the one seam every LLM feature passes through:
``LLMService.chat_completion`` and ``LLMService.chat_completion_stream``. Chat, its query
planning, summaries, topics, speaker identification and the LLM redaction detector all reach
the provider through those two methods, so no feature carries its own check.
``chat_completion_raw`` (the admin reasoning probe) and the connection test are diagnostics,
not features, and are not limited.

**Unit.** Tokens, never dollars: rates change and a local model costs nothing per token, while
tokens are what every call is measured in. A provider with no known price therefore cannot
slip past a limit, and local models are counted like any other unless an admin sets
"local models unlimited".

**Accounting** (``llm_token_budget``, one row per user-day and per instance-month). Before a
call, :func:`reserve` takes an estimate (prompt characters / 4 plus the reply budget) with one
conditional statement per counter::

    UPDATE llm_token_budget SET used = used + :n
     WHERE <row> AND used + :n <= :limit RETURNING used

Under READ COMMITTED a concurrent ``UPDATE`` of the same row waits for the first writer and
then re-evaluates its ``WHERE`` against the updated row (PostgreSQL documentation, "Read
Committed Isolation Level"), so two calls can never both take the last tokens. Both counters
are reserved in one transaction (user row first, always the same order, so two reservers
cannot deadlock); a refusal by either rolls both back. After the call, :func:`settle` moves
the reservation to the measured count, which may push a counter slightly past its limit when
a provider ignores the reply budget. A counter row is created on first use, seeded with the
tokens ``usage_event`` already holds for that period, so a limit set mid-day still counts the
day so far.

**No limit, no cost.** With both limits at 0 (the default), nothing is read or written beyond
the cached settings lookup. ``usage_event`` remains the reporting record either way.

**Failure.** A configured limit that cannot be checked refuses the call
(``llm_budget_unavailable``) rather than letting spend through unbounded. Unreadable settings
do the same, because whether a limit applies is then unknown.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_LLM_BUDGET_INSTANCE_MONTHLY_TOKENS
from app.core.constants import DEFAULT_LLM_BUDGET_LOCAL_UNLIMITED
from app.core.constants import DEFAULT_LLM_BUDGET_USER_DAILY_TOKENS
from app.core.constants import LLM_BUDGET_INSTANCE_MONTHLY_TOKENS_KEY
from app.core.constants import LLM_BUDGET_LOCAL_UNLIMITED_KEY
from app.core.constants import LLM_BUDGET_USER_DAILY_TOKENS_KEY
from app.models.llm_token_budget import SCOPE_INSTANCE_MONTH
from app.models.llm_token_budget import SCOPE_USER_DAY
from app.services.llm_usage import STAGE_EVENT_TYPES

logger = logging.getLogger(__name__)

#: Stable code of a refusal because a limit is reached. Task records, SSE error frames and the
#: UI key off it; the English sentence is only a fallback.
CODE_EXCEEDED = "llm_budget_exceeded"
#: Stable code of a refusal because a configured limit could not be checked.
CODE_UNAVAILABLE = "llm_budget_unavailable"

#: Rough characters per token for the reservation estimate (the ratio metering uses).
_CHARS_PER_TOKEN = 4

#: ``usage_event.event_type`` values that are LLM tokens, i.e. that a counter is seeded from.
#: ``chat.tokens`` is ``services.chat.usage.EVENT_TYPE_CHAT_TOKENS``, spelled out because the
#: chat package imports this module's caller; a unit test pins the two together.
TOKEN_EVENT_TYPES: tuple[str, ...] = (
    "chat.tokens",
    "chat.prep.tokens",
    *STAGE_EVENT_TYPES.values(),
)

Scope = Literal["user_day", "instance_month"]

_MESSAGES = {
    SCOPE_USER_DAY: "You have reached your daily AI token limit. It resets at midnight UTC.",
    SCOPE_INSTANCE_MONTH: (
        "This server has reached its monthly AI token limit. It resets on the first of the "
        "month (UTC). Ask an administrator to raise it."
    ),
    CODE_UNAVAILABLE: "The AI token limit could not be checked, so the request was not sent.",
}


class LLMBudgetExceededError(Exception):
    """A provider call was refused by the token limits before anything was sent.

    ``str()`` is a fixed sentence safe to show a user; ``code`` is the stable identifier
    (:data:`CODE_EXCEEDED` or :data:`CODE_UNAVAILABLE`).
    """

    def __init__(
        self,
        code: str,
        *,
        scope: Scope | None = None,
        limit: int = 0,
        used: int = 0,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.code = code
        self.scope = scope
        self.limit = limit
        self.used = used
        self.retry_after_seconds = retry_after_seconds
        super().__init__(_MESSAGES[scope if code == CODE_EXCEEDED and scope else CODE_UNAVAILABLE])

    @property
    def user_message(self) -> str:
        """The fixed, user-safe sentence for this refusal."""
        return str(self)


@dataclass(frozen=True)
class BudgetLimits:
    """The admin's limits. ``0`` means unlimited."""

    user_daily_tokens: int = DEFAULT_LLM_BUDGET_USER_DAILY_TOKENS
    instance_monthly_tokens: int = DEFAULT_LLM_BUDGET_INSTANCE_MONTHLY_TOKENS
    local_unlimited: bool = DEFAULT_LLM_BUDGET_LOCAL_UNLIMITED

    @property
    def any(self) -> bool:
        return self.user_daily_tokens > 0 or self.instance_monthly_tokens > 0


@dataclass(frozen=True)
class Reservation:
    """What :func:`reserve` took, so :func:`settle` can correct exactly those counters."""

    amount: int = 0
    user_id: int | None = None
    day: str | None = None
    month: str | None = None

    @property
    def held(self) -> bool:
        return self.amount > 0 and self.month is not None


NO_RESERVATION = Reservation()


def read_limits(db: Session) -> BudgetLimits:
    """The current limits (cached ``SystemSettings`` reads). Negative values read as 0."""
    from app.services.system_settings_service import get_setting_bool
    from app.services.system_settings_service import get_setting_int

    return BudgetLimits(
        user_daily_tokens=max(
            0,
            get_setting_int(
                db, LLM_BUDGET_USER_DAILY_TOKENS_KEY, DEFAULT_LLM_BUDGET_USER_DAILY_TOKENS
            ),
        ),
        instance_monthly_tokens=max(
            0,
            get_setting_int(
                db,
                LLM_BUDGET_INSTANCE_MONTHLY_TOKENS_KEY,
                DEFAULT_LLM_BUDGET_INSTANCE_MONTHLY_TOKENS,
            ),
        ),
        local_unlimited=get_setting_bool(
            db, LLM_BUDGET_LOCAL_UNLIMITED_KEY, DEFAULT_LLM_BUDGET_LOCAL_UNLIMITED
        ),
    )


def update_limits(
    db: Session,
    *,
    user_daily_tokens: int | None = None,
    instance_monthly_tokens: int | None = None,
    local_unlimited: bool | None = None,
) -> BudgetLimits:
    """Write the provided limits (``set_setting`` busts the settings cache) and return them."""
    from app.services.system_settings_service import set_setting

    if user_daily_tokens is not None:
        set_setting(
            db,
            LLM_BUDGET_USER_DAILY_TOKENS_KEY,
            user_daily_tokens,
            "LLM tokens one user may spend per UTC day, all AI features (0 = unlimited)",
        )
    if instance_monthly_tokens is not None:
        set_setting(
            db,
            LLM_BUDGET_INSTANCE_MONTHLY_TOKENS_KEY,
            instance_monthly_tokens,
            "LLM tokens this server may spend per UTC month, all AI features (0 = unlimited)",
        )
    if local_unlimited is not None:
        set_setting(
            db,
            LLM_BUDGET_LOCAL_UNLIMITED_KEY,
            local_unlimited,
            "Exempt models running on this server from the LLM token limits",
        )
    return read_limits(db)


def periods(now: datetime) -> tuple[str, str]:
    """``(day, month)`` keys of the UTC period containing ``now``."""
    now = now.astimezone(UTC)
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")


def period_bounds(scope: str, now: datetime) -> tuple[datetime, datetime]:
    """Start (inclusive) and end (exclusive) of the UTC day or month containing ``now``."""
    now = now.astimezone(UTC)
    if scope == SCOPE_USER_DAY:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def estimate_tokens(messages: list[dict[str, Any]], reply_budget: int) -> int:
    """The reservation for one call: prompt characters / 4 plus the reply budget."""
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return max(1, chars // _CHARS_PER_TOKEN + max(0, int(reply_budget or 0)))


_ENSURE_SQL = text(
    """
    INSERT INTO llm_token_budget (scope, user_id, period, used)
    SELECT :scope, :user_id, :period, COALESCE(SUM(quantity), 0)::bigint
      FROM usage_event
     WHERE event_type = ANY(:types)
       AND created_at >= :start AND created_at < :end
       AND (CAST(:user_id AS INTEGER) IS NULL OR user_id = :user_id)
    ON CONFLICT (scope, (COALESCE(user_id, 0)), period) DO NOTHING
    """
)

_TAKE_SQL = text(
    """
    UPDATE llm_token_budget
       SET used = used + :amount, updated_at = now()
     WHERE scope = :scope AND COALESCE(user_id, 0) = :subject AND period = :period
       AND (:limit = 0 OR used + :amount <= :limit)
    RETURNING used
    """
)

_USED_SQL = text(
    """
    SELECT used FROM llm_token_budget
     WHERE scope = :scope AND COALESCE(user_id, 0) = :subject AND period = :period
    """
)

_ADJUST_SQL = text(
    """
    UPDATE llm_token_budget
       SET used = GREATEST(0, used + :delta), updated_at = now()
     WHERE scope = :scope AND COALESCE(user_id, 0) = :subject AND period = :period
    """
)


def _ensure_counter(
    db: Session, scope: str, user_id: int | None, period: str, now: datetime
) -> None:
    """Create the period's counter, seeded from the usage already recorded in it.

    The seed is a SUM over the period's ``usage_event`` rows, so it runs only when
    :func:`_take_seeded` found no counter row, never on every call.
    """
    start, end = period_bounds(scope, now)
    db.execute(
        _ENSURE_SQL,
        {
            "scope": scope,
            "user_id": user_id,
            "period": period,
            "types": list(TOKEN_EVENT_TYPES),
            "start": start,
            "end": end,
        },
    )


def _take(
    db: Session, scope: str, user_id: int | None, period: str, amount: int, limit: int
) -> int | None:
    """Add ``amount`` if it fits under ``limit`` (0 = no limit); the new total, or None."""
    row = db.execute(
        _TAKE_SQL,
        {
            "scope": scope,
            "subject": user_id or 0,
            "period": period,
            "amount": amount,
            "limit": limit,
        },
    ).first()
    return None if row is None else int(row[0])


def _used(db: Session, scope: str, user_id: int | None, period: str) -> int:
    row = db.execute(_USED_SQL, {"scope": scope, "subject": user_id or 0, "period": period}).first()
    return 0 if row is None else int(row[0])


def _take_seeded(
    db: Session,
    scope: str,
    user_id: int | None,
    period: str,
    amount: int,
    limit: int,
    now: datetime,
) -> int | None:
    """:func:`_take`, creating the counter first only if it does not exist yet.

    The conditional UPDATE runs first, so a call against an existing counter is one
    indexed statement. Zero rows means either "over the limit" or "no counter yet"; only
    the second seeds (the month-wide SUM) and retries. Correct under Read Committed: two
    first calls racing both reach the ``INSERT ... ON CONFLICT DO NOTHING``, the loser
    waits on the winner's row and inserts nothing, and each retry's UPDATE takes a fresh
    snapshot and re-checks the limit against the committed total.
    """
    taken = _take(db, scope, user_id, period, amount, limit)
    if taken is not None:
        return taken
    if db.execute(_USED_SQL, {"scope": scope, "subject": user_id or 0, "period": period}).first():
        return None
    _ensure_counter(db, scope, user_id, period, now)
    return _take(db, scope, user_id, period, amount, limit)


def _retry_after(scope: str, now: datetime) -> int:
    _, end = period_bounds(scope, now)
    return max(1, int((end - now.astimezone(UTC)).total_seconds()))


def _session() -> Session:
    from app.db.base import SessionLocal

    return SessionLocal()


def _resolve(value: Any) -> Any:
    return value() if callable(value) else value


def reserve(
    *,
    user_id: int | None | Callable[[], int | None],
    amount: int,
    local: bool | None | Callable[[], bool] = None,
    now: datetime | None = None,
) -> Reservation:
    """Take ``amount`` tokens from the caller's counters before a provider call.

    Args:
        user_id: Whose daily limit applies, or a callable returning it (resolved only when
            a limit is set); ``None`` (a system call with no owner) is checked against the
            instance limit only.
        amount: Tokens to reserve (:func:`estimate_tokens`).
        local: Whether the call goes to a model on this deployment, or a callable answering
            it. Only consulted when the admin exempts local models; ``None`` is never exempt.
        now: Clock, for tests.

    Returns:
        The reservation to pass to :func:`settle` (empty when no limit applies).

    Raises:
        LLMBudgetExceededError: A limit would be exceeded (``CODE_EXCEEDED``), or a limit is
            configured but could not be checked (``CODE_UNAVAILABLE``).
    """
    now = now or datetime.now(UTC)
    db = _session()
    try:
        try:
            limits = read_limits(db)
        except Exception:
            logger.exception("LLM token limits could not be read; refusing the call")
            raise LLMBudgetExceededError(CODE_UNAVAILABLE) from None
        if not limits.any:
            return NO_RESERVATION
        try:
            if limits.local_unlimited and _resolve(local) is True:
                return NO_RESERVATION
            subject: int | None = _resolve(user_id)
        except Exception:
            logger.exception("LLM token limit subject could not be resolved; refusing the call")
            raise LLMBudgetExceededError(CODE_UNAVAILABLE) from None

        day, month = periods(now)
        user_limit = limits.user_daily_tokens if subject else 0
        try:
            refused: tuple[Scope, int, int] | None = None
            if (
                subject
                and _take_seeded(db, SCOPE_USER_DAY, subject, day, amount, user_limit, now) is None
            ):
                refused = (SCOPE_USER_DAY, user_limit, _used(db, SCOPE_USER_DAY, subject, day))
            if refused is None:
                inst_limit = limits.instance_monthly_tokens
                if (
                    _take_seeded(db, SCOPE_INSTANCE_MONTH, None, month, amount, inst_limit, now)
                    is None
                ):
                    refused = (
                        SCOPE_INSTANCE_MONTH,
                        inst_limit,
                        _used(db, SCOPE_INSTANCE_MONTH, None, month),
                    )
            if refused is not None:
                db.rollback()
            else:
                db.commit()
        except Exception:
            db.rollback()
            logger.exception("LLM token limit check failed; refusing the call")
            raise LLMBudgetExceededError(CODE_UNAVAILABLE) from None

        if refused is not None:
            scope, limit, used = refused
            logger.info(
                "LLM call refused: %s token limit reached (user %s, %s of %s used, %s requested)",
                scope,
                subject,
                used,
                limit,
                amount,
            )
            raise LLMBudgetExceededError(
                CODE_EXCEEDED,
                scope=scope,
                limit=limit,
                used=used,
                retry_after_seconds=_retry_after(scope, now),
            )
        return Reservation(amount=amount, user_id=subject, day=day, month=month)
    finally:
        db.close()


def settle(reservation: Reservation, actual: int) -> None:
    """Move a reservation to the measured token count. Never raises: the call already ran."""
    if not reservation.held:
        return
    delta = int(actual) - reservation.amount
    if delta == 0:
        return
    db = _session()
    try:
        if reservation.user_id:
            db.execute(
                _ADJUST_SQL,
                {
                    "scope": SCOPE_USER_DAY,
                    "subject": reservation.user_id,
                    "period": reservation.day,
                    "delta": delta,
                },
            )
        db.execute(
            _ADJUST_SQL,
            {
                "scope": SCOPE_INSTANCE_MONTH,
                "subject": 0,
                "period": reservation.month,
                "delta": delta,
            },
        )
        db.commit()
    except Exception:  # accounting never fails a call that already ran
        db.rollback()
        logger.exception("Could not settle an LLM token reservation (%s tokens)", delta)
    finally:
        db.close()


@dataclass(frozen=True)
class BudgetStatus:
    """Usage against the limits for one user, for the usage view."""

    limits: BudgetLimits
    user_used_today: int
    instance_used_this_month: int
    day_resets_at: datetime
    month_resets_at: datetime


def _period_events_sum(db: Session, scope: str, user_id: int | None, now: datetime) -> int:
    start, end = period_bounds(scope, now)
    row = db.execute(
        text(
            """
            SELECT COALESCE(SUM(quantity), 0)::bigint FROM usage_event
             WHERE event_type = ANY(:types) AND created_at >= :start AND created_at < :end
               AND (CAST(:user_id AS INTEGER) IS NULL OR user_id = :user_id)
            """
        ),
        {"types": list(TOKEN_EVENT_TYPES), "start": start, "end": end, "user_id": user_id},
    ).first()
    return 0 if row is None else int(row[0])


def _counter_or_events(
    db: Session, scope: str, user_id: int | None, period: str, now: datetime
) -> int:
    row = db.execute(_USED_SQL, {"scope": scope, "subject": user_id or 0, "period": period}).first()
    if row is not None:
        return int(row[0])
    return _period_events_sum(db, scope, user_id, now)


def status_for(db: Session, user_id: int, now: datetime | None = None) -> BudgetStatus:
    """The caller's usage today and the instance's this month, against the limits.

    The enforcement counter is reported when one exists for the period (it is what a limit
    compares against and includes calls no usage event describes); otherwise the recorded
    usage events, which is what the counter would start from.
    """
    now = now or datetime.now(UTC)
    day, month = periods(now)
    return BudgetStatus(
        limits=read_limits(db),
        user_used_today=_counter_or_events(db, SCOPE_USER_DAY, user_id, day, now),
        instance_used_this_month=_counter_or_events(db, SCOPE_INSTANCE_MONTH, None, month, now),
        day_resets_at=period_bounds(SCOPE_USER_DAY, now)[1],
        month_resets_at=period_bounds(SCOPE_INSTANCE_MONTH, now)[1],
    )


def refusal_of(exc: BaseException | None) -> LLMBudgetExceededError | None:
    """The token-limit refusal behind ``exc`` (itself, or anywhere in its cause chain).

    Tasks wrap provider errors in their own exceptions; this is how their failure handlers
    record the refusal's stable code instead of the wrapper's text.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, LLMBudgetExceededError):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def notification_extra(refusal: LLMBudgetExceededError) -> dict[str, Any]:
    """Fields a task's failure notification carries so the client can translate it."""
    return {"error_code": refusal.code, "budget_scope": refusal.scope}
