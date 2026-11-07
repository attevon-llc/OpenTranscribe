"""Admin deployment defaults for speech detection (``/admin/transcription-defaults``)."""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_admin_user
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.db.base import get_db
from app.services import transcription_defaults as defaults

logger = logging.getLogger(__name__)

router = APIRouter()


class VadDefaults(BaseModel):
    """The deployment's speech-detection defaults."""

    vad_threshold: float
    vad_min_silence_ms: int
    vad_min_speech_ms: int
    vad_speech_pad_ms: int


class VadDefaultsUpdate(BaseModel):
    """Fields to change; omitted fields keep their stored value."""

    model_config = ConfigDict(extra="forbid")

    vad_threshold: float | None = Field(default=None, ge=0.1, le=0.95)
    vad_min_silence_ms: int | None = Field(default=None, ge=100, le=5000)
    vad_min_speech_ms: int | None = Field(default=None, ge=50, le=5000)
    vad_speech_pad_ms: int | None = Field(default=None, ge=0, le=2000)


@router.get("/transcription-defaults", response_model=VadDefaults)
def get_transcription_defaults(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> VadDefaults:
    """Return the deployment's speech-detection defaults."""
    return VadDefaults.model_validate(asdict(defaults.get_deployment_vad_defaults(db)))


@router.put("/transcription-defaults", response_model=VadDefaults)
def put_transcription_defaults(
    request: Request,
    body: VadDefaultsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> VadDefaults:
    """Update the deployment defaults (only provided fields)."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    result = defaults.set_deployment_vad_defaults(db, updates)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=request.client.host if request.client else None,
        details={
            "area": "transcription.defaults",
            "changed": {k: updates[k] for k in sorted(updates)},
        },
    )
    logger.info("Admin %s updated VAD deployment defaults: %s", current_user.email, sorted(updates))
    return VadDefaults.model_validate(asdict(result))
