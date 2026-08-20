"""Platform-admin RAG tuning (``/admin/chat-settings``).

All knobs are DB-backed ``SystemSettings`` (engine-settings pattern), so an
operator can retune retrieval depth, reranking and limits from the admin UI with
no restart and no ``.env`` edit.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_admin_user
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.core.chat_flag_registry import DESCRIPTIONS as _DESCRIPTIONS
from app.db.base import get_db
from app.schemas.chat import ChatAdminSettings
from app.schemas.chat import ChatAdminSettingsUpdate
from app.services.chat.settings import SETTING_KEYS
from app.services.chat.settings import get_chat_settings
from app.services.system_settings_service import set_setting

logger = logging.getLogger(__name__)

router = APIRouter()

# `_DESCRIPTIONS` used to be a second, hand-maintained dict here — a field
# added to `ChatAdminSettingsUpdate` without a matching entry raised
# `KeyError` on the very first save (a 500, not a 400) with nothing at commit
# time to catch it. It is now sourced from `core.chat_flag_registry`, the one
# declarative table these 13 flags are described in;
# `tests/unit/test_chat_flag_registry.py` is the completeness check that
# fails if the registry and `SETTING_KEYS`/the schema ever disagree again.


@router.get("", response_model=ChatAdminSettings)
def get_chat_admin_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> ChatAdminSettings:
    """Return the platform's chat/RAG configuration."""
    return ChatAdminSettings(**get_chat_settings(db).as_dict())


@router.put("", response_model=ChatAdminSettings)
def update_chat_admin_settings(
    request: Request,
    body: ChatAdminSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> ChatAdminSettings:
    """Update chat/RAG configuration (only provided fields)."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    for field, value in updates.items():
        stored = "true" if value is True else "false" if value is False else str(value)
        # `.get(..., field)` rather than `[field]`: a field that reaches here
        # is already schema-validated by `ChatAdminSettingsUpdate`, so
        # `SETTING_KEYS`/`_DESCRIPTIONS` SHOULD always have it — but "should"
        # is exactly the assumption that produced the 500 this guards
        # against, and a readable fallback description beats a 500 even for a
        # registry that has since drifted.
        setting_key = SETTING_KEYS.get(field, f"chat.{field}")
        description = _DESCRIPTIONS.get(field, field.replace("_", " "))
        set_setting(db, setting_key, stored, description)

    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=request.client.host if request.client else None,
        details={"area": "chat", "fields": sorted(updates.keys())},
    )
    logger.info("Admin %s updated chat settings: %s", current_user.email, sorted(updates))

    return ChatAdminSettings(**get_chat_settings(db).as_dict())
