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
from app.db.base import get_db
from app.schemas.chat import ChatAdminSettings
from app.schemas.chat import ChatAdminSettingsUpdate
from app.services.chat.settings import SETTING_KEYS
from app.services.chat.settings import get_chat_settings
from app.services.system_settings_service import set_setting

logger = logging.getLogger(__name__)

router = APIRouter()

_DESCRIPTIONS = {
    "candidate_pool": "Chunks retrieved before reranking",
    "final_chunks": "Chunks included in the prompt",
    "max_chunks_per_file": "Maximum chunks contributed by any one recording",
    "rerank_enabled": "Rerank retrieved chunks with a CPU cross-encoder",
    "rerank_max_pairs": "Maximum (query, chunk) pairs scored per message",
    "query_rewrite_enabled": "Expand follow-up questions into standalone queries",
    "cache_ttl_seconds": "Retrieval cache lifetime (0 disables)",
    "semantic_cache_enabled": "Reuse results for near-identical questions",
    "semantic_cache_threshold": "Cosine similarity required for a semantic cache hit",
    "history_max_turns": "Prior turns replayed to the model",
    "messages_per_hour": "Per-user hourly message ceiling",
    "max_concurrent_streams": "Per-user simultaneous streaming replies",
    "retention_days": "Delete conversations older than N days (0 keeps forever)",
}


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
        set_setting(db, SETTING_KEYS[field], stored, _DESCRIPTIONS[field])

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
