"""Local Whisper model control: which model the GPU worker pins, and restarting it.

These routes were part of ``asr_settings`` and so were hidden whenever the deployment turned
off ``asr.user_providers`` (bring-your-own cloud ASR keys), which also took away the
super_admin's ability to pin the local model. They are gated on ``asr.model_selection``
instead (see ``app/api/router.py``) and keep their ``/asr-settings/local-model/*`` paths.
"""

import logging
import os
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth import get_current_active_user
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.rate_limit import get_api_rate_limit
from app.auth.rate_limit import limiter
from app.core import local_whisper_catalog
from app.core.constants import WHISPER_WORKER_PREFIXES
from app.db.base import get_db
from app.services.asr.factory import ASR_PROVIDER_CATALOG
from app.services.local_models import cache as local_model_cache
from app.services.local_models import worker_report
from app.utils.lite_mode import refuse_on_lite

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/local-model/active")
def get_active_local_model(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Return the active local Whisper model name and available downloaded models."""
    from app.models.system_settings import SystemSettings
    from app.services.asr.model_discovery import discover_local_models

    setting = db.query(SystemSettings).filter(SystemSettings.key == "asr.local_model").first()
    active_model = (
        setting.value if setting and setting.value else os.getenv("WHISPER_MODEL", "large-v3-turbo")
    )

    # Look up catalog info for the active model
    local_models = ASR_PROVIDER_CATALOG.get("local", {}).get("models", [])
    model_info: dict[str, Any] = {}
    for m in local_models:
        if m["id"] == active_model:
            model_info = {
                "display_name": m.get("display_name", active_model),
                "description": m.get("description", ""),
                "supports_translation": m.get("supports_translation", True),
                "supports_diarization": m.get("supports_diarization", True),
                "language_support": m.get("language_support", "multilingual"),
            }
            break
    # English-only ".en" variants (tiny.en, base.en, …) are not in the catalog but are
    # English-only. Resolve them before the substring loop so they are not mis-matched to
    # the multilingual base entry (e.g. "tiny.en" -> "tiny").
    if not model_info and active_model.endswith(".en"):
        model_info = {
            "display_name": active_model,
            "description": "English-only Whisper model",
            "supports_translation": False,
            "supports_diarization": True,
            "language_support": "english_only",
        }
    # Substring match for models like "large-v3-turbo" in custom repo names
    if not model_info:
        sorted_models = sorted(local_models, key=lambda x: len(x["id"]), reverse=True)
        for m in sorted_models:
            if m["id"] in active_model:
                model_info = {
                    "display_name": m.get("display_name", active_model),
                    "description": m.get("description", ""),
                    "supports_translation": m.get("supports_translation", True),
                    "supports_diarization": m.get("supports_diarization", True),
                    "language_support": m.get("language_support", "multilingual"),
                }
                break

    return {
        "active_model": active_model,
        "source": "database" if (setting and setting.value) else "environment",
        "available_models": discover_local_models(),
        "model_info": model_info,
        # What each Whisper worker has actually loaded (empty when Redis is unreachable).
        "loaded_models": worker_report.read_loaded_models(),
    }


class _SetLocalModelRequest(BaseModel):
    model_name: str


@router.post("/local-model/set")
@limiter.limit(get_api_rate_limit())
def set_active_local_model(
    request: Request,
    body: _SetLocalModelRequest,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> Any:
    """Set the active local Whisper model (admin only).

    Stores the model name in SystemSettings so the GPU worker uses it on
    next startup.  Does NOT restart the worker — call ``/local-model/restart``
    separately to apply with graceful drain. Lite has no local Whisper: 409
    ``local_asr_unavailable`` (#1228).
    """
    refuse_on_lite("local_asr_unavailable")
    requested = body.model_name.strip()
    if not requested:
        raise HTTPException(status_code=400, detail="model_name is required")

    # Only catalog models can be selected (#1246). Aliases such as `crisperwhisper` and the
    # PyTorch checkpoint `nyrahealth/CrisperWhisper` (no CT2 model.bin, so faster-whisper cannot
    # load it) resolve to their loadable catalog entry; any other name, including an arbitrary
    # `org/repo`, is refused.
    model_id = local_whisper_catalog.canonical_id(requested)
    entry = local_whisper_catalog.get(model_id) if model_id else None
    if entry is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unknown_local_model",
                "message": "This model is not in the list of supported local models.",
            },
        )
    # The worker loads the model from the cache with no network on restart; a model that is not
    # fully on disk would make it download inside preload or start unloaded.
    if not local_model_cache.is_complete(entry.repo_id):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "local_model_not_downloaded",
                "message": "Download this model before activating it.",
            },
        )
    model_name = entry.id

    from app.models.system_settings import SystemSettings

    setting = db.query(SystemSettings).filter(SystemSettings.key == "asr.local_model").first()
    if setting:
        setting.value = model_name  # type: ignore[assignment]
    else:
        setting = SystemSettings(
            key="asr.local_model",
            value=model_name,
            description="Active local Whisper model set by admin",
        )
        db.add(setting)
    db.commit()

    logger.info("Admin %s set local ASR model to '%s'", current_user.email, model_name)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=request.client.host if request.client else None,
        details={"setting": "local_whisper_model", "action": "set", "model": model_name},
    )

    return {
        "model_name": model_name,
        "message": f"Local model set to '{model_name}'. Restart GPU worker to apply.",
    }


@router.post("/local-model/restart")
@limiter.limit("5/minute")
def restart_gpu_worker(
    request: Request,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> Any:
    """Gracefully restart the Whisper worker(s) to apply a new model (super_admin only).

    Sends a warm shutdown, by name, to the workers in ``WHISPER_WORKER_PREFIXES``.  Each
    finishes the jobs it is running before exiting (the remote control does not arm the
    cooperative abort), Docker ``restart: always`` brings the container back, and the
    ``worker_ready`` hook preloads the stored model and records it
    (``services/local_models/worker_report``).  Clients confirm the switch by waiting for the
    returned ``workers`` to report the new model after ``restart_requested_at`` via
    ``GET /local-model/active``.  Lite runs no GPU worker: 409 ``local_asr_unavailable``
    (#1228).  No worker answering is 409 ``gpu_worker_not_found`` and nothing is signalled;
    it used to broadcast ``shutdown`` to every worker in the deployment.
    """
    refuse_on_lite("local_asr_unavailable")
    from app.core.celery import celery_app
    from app.models.system_settings import SystemSettings

    # Read the pending model so we can report it
    setting = db.query(SystemSettings).filter(SystemSettings.key == "asr.local_model").first()
    pending_model = (
        setting.value if setting and setting.value else os.getenv("WHISPER_MODEL", "large-v3-turbo")
    )

    restart_requested_at = datetime.now(UTC).isoformat()

    # Discover GPU workers
    try:
        inspector = celery_app.control.inspect(timeout=3.0)
        ping_result = inspector.ping() or {}
    except Exception as exc:
        logger.warning("Failed to inspect Celery workers: %s", exc)
        ping_result = {}

    # Only the workers that load the pinned Whisper model. Restarting anything else (the
    # diarization worker, media downloads - which include a model download in flight - yt-dlp,
    # redaction, embeddings) buys nothing and interrupts unrelated work.
    gpu_workers = [name for name in ping_result if name.startswith(WHISPER_WORKER_PREFIXES)]

    if not gpu_workers:
        # No broadcast: with nobody to name, "shutdown" would reach every worker.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "gpu_worker_not_found",
                "message": "No transcription worker answered. It may already be restarting.",
            },
        )

    # Check for active GPU tasks. Best-effort: this only produces an advisory
    # count in the response, so a broker hiccup must not block the restart.
    try:
        active_result = inspector.active() or {}
    except Exception:
        logger.exception("Failed to inspect active GPU tasks; reporting 0")
        active_result = {}

    active_gpu_tasks = 0
    for worker_name in gpu_workers:
        tasks = active_result.get(worker_name, [])
        active_gpu_tasks += len(tasks)

    # Send warm shutdown to GPU workers only
    try:
        celery_app.control.broadcast("shutdown", destination=gpu_workers)
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as exc:
        logger.exception("Failed to send shutdown to GPU workers")
        raise HTTPException(
            status_code=500,
            detail="Failed to signal worker restart.",
        ) from exc

    logger.info(
        "Admin %s triggered GPU worker restart (model=%s, workers=%s, active_tasks=%d)",
        current_user.email,
        pending_model,
        gpu_workers,
        active_gpu_tasks,
    )

    return {
        "status": "restart_signaled",
        "model": pending_model,
        "workers": gpu_workers,
        "active_tasks": active_gpu_tasks,
        "restart_requested_at": restart_requested_at,
        "message": (
            f"GPU worker restart signaled. "
            f"{'Worker will finish ' + str(active_gpu_tasks) + ' active task(s) before restarting.' if active_gpu_tasks > 0 else 'Worker will restart immediately.'} "
            f"New model: {pending_model}"
        ),
    }
