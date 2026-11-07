"""Admin YouTube ingestion policy (``/admin/youtube-settings``).

DB-backed ``SystemSettings`` (``youtube.*``): per-user download quotas and automatic retry.
Writes go through ``set_setting``, which invalidates the settings cache, and are audited.
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
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.db.base import get_db
from app.schemas.youtube_settings import YoutubeSettings
from app.schemas.youtube_settings import YoutubeSettingsUpdate
from app.services import youtube_policy

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/youtube-settings", response_model=YoutubeSettings)
def get_youtube_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> YoutubeSettings:
    """Return the effective YouTube ingestion policy."""
    return YoutubeSettings(**youtube_policy.get_youtube_policy(db).as_dict())


@router.put("/youtube-settings", response_model=YoutubeSettings)
def update_youtube_settings(
    request: Request,
    body: YoutubeSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> YoutubeSettings:
    """Update the policy (only provided fields)."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    policy = youtube_policy.update_youtube_policy(db, updates)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=request.client.host if request.client else None,
        details={"area": "youtube", "changed": {k: updates[k] for k in sorted(updates)}},
    )
    logger.info("Admin %s updated YouTube settings: %s", current_user.email, sorted(updates))
    return YoutubeSettings(**policy.as_dict())
