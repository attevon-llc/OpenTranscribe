"""Admin API endpoints for DB-backed engine settings and Redis-backed metrics."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from app import models

# Deployment configuration is the super_admin tier: this router
# changes the transcription engine and GPU topology.
from app.api.endpoints.auth import get_current_active_superuser
from app.db.base import get_db
from app.models.system_settings import SystemSettings
from app.services.system_settings_service import get_setting
from app.services.system_settings_service import set_setting

router = APIRouter()
logger = logging.getLogger(__name__)

# Only runtime-safe, admin-tunable engine settings are exposed here. Deployment/infra
# settings (gpu_split — needs the --profile gpu-split worker containers; precompute_vad —
# unimplemented Phase-3a stub; shared_volume_path — internal cross-stage handoff dir) are
# intentionally NOT surfaced in the UI; they remain env/deployment config on EngineConfig.
_KEYS = {
    "transcriber_backend": "engine.transcriber_backend",
    "diarizer_backend": "engine.diarizer_backend",
    "boundary_smoothing_enabled": "engine.boundary_smoothing_enabled",
    "boundary_acoustic_recheck_enabled": "engine.boundary_acoustic_recheck_enabled",
    "boundary_acoustic_cosine_margin": "engine.boundary_acoustic_cosine_margin",
    "boundary_acoustic_max_word_dur": "engine.boundary_acoustic_max_word_dur",
}

_ENV_DEFAULTS: dict[str, Any] = {
    "transcriber_backend": ("ENGINE_TRANSCRIBER_BACKEND", "faster_whisper"),
    "diarizer_backend": ("ENGINE_DIARIZER_BACKEND", "native"),
    "boundary_smoothing_enabled": ("ENGINE_BOUNDARY_SMOOTHING_ENABLED", "true"),
    "boundary_acoustic_recheck_enabled": ("ENGINE_BOUNDARY_ACOUSTIC_RECHECK_ENABLED", "false"),
    "boundary_acoustic_cosine_margin": ("ENGINE_BOUNDARY_ACOUSTIC_COSINE_MARGIN", "0.05"),
    "boundary_acoustic_max_word_dur": ("ENGINE_BOUNDARY_ACOUSTIC_MAX_WORD_DUR", "1.0"),
}

_BOOL_KEYS = {
    "boundary_smoothing_enabled",
    "boundary_acoustic_recheck_enabled",
}

_FLOAT_KEYS = {"boundary_acoustic_cosine_margin", "boundary_acoustic_max_word_dur"}

_DESCRIPTIONS = {
    "transcriber_backend": "Transcription backend (faster_whisper | whisperx | cloud)",
    "diarizer_backend": (
        "Speaker diarization backend (native | pyannote) — native (diar-native, "
        "Rust/speakrs) is primary; pyannote is the explicit failover, also used "
        "automatically whenever the native sidecar is unreachable"
    ),
    "boundary_smoothing_enabled": (
        "Collapse 1-3 word wrong-speaker islands at turn boundaries (issue #193)"
    ),
    "boundary_acoustic_recheck_enabled": (
        "Re-check short absorbed backchannels (yeah/mm-hmm) by voiceprint and reassign "
        "the correct speaker (issue #193, Phase 3)"
    ),
    "boundary_acoustic_cosine_margin": (
        "Minimum voiceprint cosine improvement over the current speaker before a word is "
        "reassigned (higher = more conservative)"
    ),
    "boundary_acoustic_max_word_dur": (
        "Only re-check words at or below this duration in seconds (backchannel length)"
    ),
}


def _coerce(field: str, raw: str) -> Any:
    """Coerce a stored string value to the field's typed value (bool / float / str)."""
    if field in _BOOL_KEYS:
        return raw.lower() in ("true", "1", "yes", "on")
    if field in _FLOAT_KEYS:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return float(_ENV_DEFAULTS[field][1])
    return raw


def _resolve_setting(db: Session, field: str) -> dict[str, Any]:
    """Return value and source for a single engine setting field."""
    db_key = _KEYS[field]
    env_var, default = _ENV_DEFAULTS[field]

    db_value = get_setting(db, db_key)
    if db_value is not None:
        return {"value": _coerce(field, db_value), "source": "db"}

    env_value = os.getenv(env_var)
    if env_value is not None:
        return {"value": _coerce(field, env_value), "source": "env"}

    return {"value": _coerce(field, default), "source": "default"}


@router.get("")
def get_engine_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> Any:
    """Return current engine settings with source annotation (db / env / default)."""
    return {field: _resolve_setting(db, field) for field in _KEYS}


class _EngineSettingsUpdate(BaseModel):
    transcriber_backend: str | None = None
    diarizer_backend: str | None = None
    boundary_smoothing_enabled: bool | None = None
    boundary_acoustic_recheck_enabled: bool | None = None
    boundary_acoustic_cosine_margin: float | None = Field(default=None, ge=0.0, le=1.0)
    boundary_acoustic_max_word_dur: float | None = Field(default=None, ge=0.1, le=5.0)


@router.post("/update")
def update_engine_settings(
    body: _EngineSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> Any:
    """Write one or more engine settings to the DB. Only provided (non-None) fields are saved."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    if "transcriber_backend" in updates:
        from app.transcription.engine.backends import VALID_TRANSCRIBER_BACKENDS

        value = updates["transcriber_backend"]
        if value.lower() not in VALID_TRANSCRIBER_BACKENDS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown transcriber_backend '{value}'. "
                    f"Valid values: {sorted(VALID_TRANSCRIBER_BACKENDS)}"
                ),
            )
        updates["transcriber_backend"] = value.lower()

    if "diarizer_backend" in updates:
        from app.transcription.engine.backends import VALID_DIARIZER_BACKENDS

        value = updates["diarizer_backend"]
        if value.lower() not in VALID_DIARIZER_BACKENDS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown diarizer_backend '{value}'. "
                    f"Valid values: {sorted(VALID_DIARIZER_BACKENDS)}"
                ),
            )
        updates["diarizer_backend"] = value.lower()

    for field, value in updates.items():
        db_key = _KEYS[field]
        set_setting(db, db_key, value, _DESCRIPTIONS[field])
        logger.info("Admin %s set %s = %r", current_user.email, db_key, value)

    return {field: _resolve_setting(db, field) for field in _KEYS}


@router.delete("/{key}", status_code=204)
def reset_engine_setting(
    key: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
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


_METRICS_KEY_PREFIX = "engine:metrics:"


@router.get("/metrics")
def get_engine_metrics(
    current_user: models.User = Depends(get_current_active_superuser),
) -> Any:
    """Return live engine metrics from all active workers via Redis.

    Each Celery GPU worker publishes counters under ``engine:metrics:<hostname>``
    with a 120-second TTL. Returns a dict keyed by hostname. An empty dict
    means no workers have reported metrics recently.
    """
    try:
        from app.core.redis import get_redis

        r = get_redis()
        keys = r.keys(f"{_METRICS_KEY_PREFIX}*")
    except Exception as exc:
        logger.warning("engine-metrics: Redis unavailable: %s", exc)
        return {}

    result: dict[str, Any] = {}
    for key in keys:
        hostname = (key.decode() if isinstance(key, bytes) else key).removeprefix(
            _METRICS_KEY_PREFIX
        )
        try:
            raw = r.hgetall(key)
            stage_durations = {
                k.decode().removeprefix("last_").removesuffix("_s"): float(v)
                for k, v in raw.items()
                if k.startswith(b"last_") and k.endswith(b"_s")
            }
            result[hostname] = {
                "gpu_ready_queue_depth": int(raw.get(b"gpu_ready_queue_depth", 0) or 0),
                "gpu_in_flight": int(raw.get(b"gpu_in_flight", 0) or 0),
                "gpu_idle_seconds": float(raw.get(b"gpu_idle_seconds", 0.0) or 0.0),
                "last_stage_durations": stage_durations,
            }
        except Exception as exc:
            logger.warning("engine-metrics: failed to read key %s: %s", key, exc)
    return result
