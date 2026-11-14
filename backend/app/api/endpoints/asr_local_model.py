"""Local Whisper model control: which model the GPU worker pins, and restarting it.

These routes were part of ``asr_settings`` and so were hidden whenever the deployment turned
off ``asr.user_providers`` (bring-your-own cloud ASR keys), which also took away the
super_admin's ability to pin the local model. They are gated on ``asr.model_selection``
instead (see ``app/api/router.py``) and keep their ``/asr-settings/local-model/*`` paths.
"""

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from kombu.exceptions import KombuError
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth import get_current_active_user
from app.api.rate_limit import get_api_rate_limit
from app.api.rate_limit import limiter
from app.api.rate_limit import user_or_ip_key
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.core import local_whisper_catalog
from app.core.config import settings
from app.core.constants import WHISPER_WORKER_PREFIXES
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.services.asr.catalog import ASR_PROVIDER_CATALOG
from app.services.local_models import cache as local_model_cache
from app.services.local_models import worker_report
from app.services.system_setting_queries import find_system_setting
from app.utils.client_ip import known_client_ip
from app.utils.lite_mode import refuse_on_lite

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/local-model/active")
def get_active_local_model(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Return the active local Whisper model name and available downloaded models."""
    from app.services.asr.model_discovery import discover_local_models

    setting = find_system_setting(db, "asr.local_model")
    active_model = setting.value if setting and setting.value else settings.WHISPER_MODEL

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
                # The numbers behind ``description``, so the UI can say it in the reader's
                # language instead of showing the catalog's English sentence.
                "vram_hint_gb": m.get("vram_hint_gb"),
                "is_default": bool(m.get("is_default", False)),
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
                    "vram_hint_gb": m.get("vram_hint_gb"),
                    "is_default": bool(m.get("is_default", False)),
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
@limiter.limit(get_api_rate_limit(), key_func=user_or_ip_key)
def set_active_local_model(
    request: Request,
    body: _SetLocalModelRequest,
    response: Response,
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

    setting = find_system_setting(db, "asr.local_model")
    if setting:
        setting.value = model_name
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
        source_ip=known_client_ip(request),
        details={"setting": "local_whisper_model", "action": "set", "model": model_name},
    )

    return {
        "model_name": model_name,
        "message": f"Local model set to '{model_name}'. Restart GPU worker to apply.",
    }


@router.post("/local-model/restart")
@limiter.limit("5/minute", key_func=user_or_ip_key)
def restart_gpu_worker(
    request: Request,
    response: Response,
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

    # Read the pending model so we can report it
    setting = find_system_setting(db, "asr.local_model")
    pending_model = setting.value if setting and setting.value else settings.WHISPER_MODEL

    restart_requested_at = datetime.now(UTC).isoformat()

    # Discover GPU workers
    try:
        inspector = celery_app.control.inspect(timeout=3.0)
        ping_result = inspector.ping() or {}
    except (KombuError, OSError) as exc:
        # An unreachable broker is not "no worker answered": the admin must not be told to
        # wait for a restart that was never possible.
        logger.exception("Failed to inspect Celery workers: broker unreachable")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "worker_broker_unavailable",
                "message": "The task broker is unreachable, so workers cannot be restarted.",
            },
        ) from exc

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
    except (HTTPException, DomainError):
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
