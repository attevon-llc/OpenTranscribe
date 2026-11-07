"""Running token counters behind the LLM token limits (issue #790).

One row per (scope, subject, period): a user's tokens on one UTC day, or the whole
instance's tokens in one UTC calendar month. ``services/llm_budget.py`` is the only
writer; it reserves before each provider call with a single conditional ``UPDATE`` and
settles to the measured count afterwards, so two concurrent calls cannot both take the
last tokens. ``usage_event`` remains the reporting record; this table is the enforcement
counter and also counts calls no usage event describes (chat's query planning).

A user's rows are deleted with the user (``ON DELETE CASCADE``); an instance row has no
user (``user_id`` NULL) and is keyed by period alone.
"""

from datetime import datetime
from typing import Final

from sqlalchemy import BigInteger
from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from app.db.base import Base

SCOPE_USER_DAY: Final = "user_day"
SCOPE_INSTANCE_MONTH: Final = "instance_month"


class LLMTokenBudget(Base):
    __tablename__ = "llm_token_budget"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('user_day', 'instance_month')", name="ck_llm_token_budget_scope"
        ),
        CheckConstraint(
            "(scope = 'user_day') = (user_id IS NOT NULL)", name="ck_llm_token_budget_subject"
        ),
        CheckConstraint("used >= 0", name="ck_llm_token_budget_used"),
        Index(
            "uq_llm_token_budget_counter",
            "scope",
            text("COALESCE(user_id, 0)"),
            "period",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=True
    )
    #: ``YYYY-MM-DD`` for a user's day, ``YYYY-MM`` for the instance's month (UTC).
    period: Mapped[str] = mapped_column(String(10), nullable=False)
    used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
