"""Admin LLM token limits (``/admin/llm-budget``, issue #790, owner decision D13).

- ``GET /admin/llm-budget``: the limits and this instance's tokens this month.
- ``PUT /admin/llm-budget``: change any of the limits (``0`` = unlimited).

The limits are DB-backed ``SystemSettings`` (no restart, no ``.env`` edit); writing them
busts the settings cache. Enforcement is ``services/llm_budget.py`` at the shared LLM call
seam. Handlers are ``def``: they only do blocking SQLAlchemy work.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_admin_user
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.core.constants import LLM_BUDGET_MAX_TOKENS
from app.db.base import get_db
from app.services import llm_budget

logger = logging.getLogger(__name__)
router = APIRouter()


class LLMBudgetSettings(BaseModel):
    """The token limits (``0`` = unlimited) and this instance's standing against them."""

    user_daily_tokens: int
    instance_monthly_tokens: int
    local_unlimited: bool
    instance_used_this_month: int
    month_resets_at: datetime
    day_resets_at: datetime


class LLMBudgetUpdate(BaseModel):
    """Only the provided fields change."""

    user_daily_tokens: int | None = Field(default=None, ge=0, le=LLM_BUDGET_MAX_TOKENS)
    instance_monthly_tokens: int | None = Field(default=None, ge=0, le=LLM_BUDGET_MAX_TOKENS)
    local_unlimited: bool | None = None


def _view(db: Session, admin_id: int) -> LLMBudgetSettings:
    status = llm_budget.status_for(db, admin_id, now=datetime.now(UTC))
    return LLMBudgetSettings(
        user_daily_tokens=status.limits.user_daily_tokens,
        instance_monthly_tokens=status.limits.instance_monthly_tokens,
        local_unlimited=status.limits.local_unlimited,
        instance_used_this_month=status.instance_used_this_month,
        month_resets_at=status.month_resets_at,
        day_resets_at=status.day_resets_at,
    )


@router.get("", response_model=LLMBudgetSettings)
def get_llm_budget(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> LLMBudgetSettings:
    """The token limits and this instance's tokens this month."""
    return _view(db, current_user.id)


@router.put("", response_model=LLMBudgetSettings)
def update_llm_budget(
    request: Request,
    body: LLMBudgetUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> LLMBudgetSettings:
    """Change the token limits (only the provided fields)."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    llm_budget.update_limits(db, **updates)

    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=request.client.host if request.client else None,
        # The limits are not secrets, and an auditor needs the new values, not only the names.
        details={"area": "llm_budget", "changes": updates},
    )
    logger.info("Admin %s updated the LLM token limits: %s", current_user.email, updates)
    return _view(db, current_user.id)
