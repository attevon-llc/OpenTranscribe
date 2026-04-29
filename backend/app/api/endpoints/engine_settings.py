"""Admin API endpoints for DB-backed engine settings."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_admin_user
from app.db.base import get_db
from app.models.system_settings import SystemSettings
from app.services.system_settings_service import get_setting
from app.services.system_settings_service import set_setting

router = APIRouter()
logger = logging.getLogger(__name__)

_KEYS = {
    "transcriber_backend": "engine.transcriber_backend",
    "diarizer_backend": "engine.diarizer_backend",
    "gpu_split": "engine.gpu_split",
    "precompute_vad": "engine.precompute_vad",
    "shared_volume_path": "engine.shared_volume_path",
}

_ENV_DEFAULTS: dict[str, Any] = {
    "transcriber_backend": ("ENGINE_TRANSCRIBER_BACKEND", "faster_whisper"),
    "diarizer_backend": ("ENGINE_DIARIZER_BACKEND", "pyannote"),
    "gpu_split": ("ENGINE_GPU_SPLIT", "false"),
    "precompute_vad": ("ENGINE_PRECOMPUTE_VAD", "false"),
    "shared_volume_path": ("ENGINE_SHARED_VOLUME_PATH", "/tmp/transcription"),  # noqa: S108  # nosec B108
}

_BOOL_KEYS = {"gpu_split", "precompute_vad"}

_DESCRIPTIONS = {
    "transcriber_backend": "Transcription backend (faster_whisper | whisperx | cloud)",
    "diarizer_backend": "Speaker diarization backend (pyannote)",
    "gpu_split": "Enable separate gpu-transcribe / gpu-diarize queues (Phase 4)",
    "precompute_vad": "Enable Silero VAD pre-computation in Stage 1 (Phase 3a)",
    "shared_volume_path": "Shared-volume handoff path for cross-stage WAV files",
}


def _resolve_setting(db: Session, field: str) -> dict[str, Any]:
    """Return value and source for a single engine setting field."""
    db_key = _KEYS[field]
    env_var, default = _ENV_DEFAULTS[field]

    db_value = get_setting(db, db_key)
    if db_value is not None:
        value: Any = (
            db_value.lower() in ("true", "1", "yes", "on") if field in _BOOL_KEYS else db_value
        )
        return {"value": value, "source": "db"}

    env_value = os.getenv(env_var)
    if env_value is not None:
        value = (
            env_value.lower() in ("true", "1", "yes", "on") if field in _BOOL_KEYS else env_value
        )
        return {"value": value, "source": "env"}

    value = default.lower() in ("true", "1", "yes", "on") if field in _BOOL_KEYS else default
    return {"value": value, "source": "default"}


@router.get("")
def get_engine_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> Any:
    """Return current engine settings with source annotation (db / env / default)."""
    return {field: _resolve_setting(db, field) for field in _KEYS}


class _EngineSettingsUpdate(BaseModel):
    transcriber_backend: str | None = None
    diarizer_backend: str | None = None
    gpu_split: bool | None = None
    precompute_vad: bool | None = None
    shared_volume_path: str | None = None


@router.post("/update")
def update_engine_settings(
    body: _EngineSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> Any:
    """Write one or more engine settings to the DB. Only provided (non-None) fields are saved."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    for field, value in updates.items():
        db_key = _KEYS[field]
        set_setting(db, db_key, value, _DESCRIPTIONS[field])
        logger.info("Admin %s set %s = %r", current_user.email, db_key, value)

    return {field: _resolve_setting(db, field) for field in _KEYS}


@router.delete("/{key}", status_code=204)
def reset_engine_setting(
    key: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> None:
    """Delete a DB override for the given key, reverting to env var / default."""
    if key not in _KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown engine setting key '{key}'. Valid keys: {sorted(_KEYS)}",
        )
    db_key = _KEYS[key]
    row = db.query(SystemSettings).filter(SystemSettings.key == db_key).first()
    if row:
        db.delete(row)
        db.commit()
        logger.info("Admin %s reset engine setting %s to env/default", current_user.email, db_key)
