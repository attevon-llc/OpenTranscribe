"""Local Whisper model library: list, download and cancel (#1246) - super_admin only.

- ``GET  /admin/local-models``                  - the catalog, what is on disk, the download in flight
- ``POST /admin/local-models/download``         - download one catalog model (one at a time)
- ``POST /admin/local-models/download/cancel``  - stop the download in flight

Only catalog ids are accepted: the repository and every filesystem path are derived from the
catalog, never from input. The download runs as a Celery task on the ``download`` queue, see
``tasks/local_model_download.py``; progress reaches the browser as the ``local_model_download``
WebSocket event and is also readable here, so a reload or a dropped socket loses nothing.
Activating a downloaded model is ``/asr-settings/local-model/set`` and ``.../restart``.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.dependencies import get_current_active_superuser
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.rate_limit import limiter
from app.core import local_whisper_catalog as catalog
from app.core.redis import get_redis
from app.db.base import get_db
from app.schemas.local_models import LocalModelCatalogResponse
from app.schemas.local_models import LocalModelDownloadRequest
from app.schemas.local_models import LocalModelDownloadState
from app.schemas.local_models import LocalModelEntry
from app.services.local_models import cache
from app.services.local_models.downloader import ERROR_MESSAGES
from app.services.local_models.state import ACTIVE_STATUSES
from app.services.local_models.state import DownloadStore
from app.utils.lite_mode import refuse_on_lite

logger = logging.getLogger(__name__)
router = APIRouter()

#: Free space kept beyond the bytes a download still needs (the task re-checks exactly).
DISK_HEADROOM_BYTES = 1024**3


def _error(status_code: int, code: str, **extra: Any) -> HTTPException:
    """An error body with a stable code and a fixed sentence (never exception text)."""
    detail = {"code": code, "message": ERROR_MESSAGES[code], **extra}
    return HTTPException(status_code=status_code, detail=detail)


def _store() -> DownloadStore:
    return DownloadStore(get_redis())


def _audit(request: Request, actor: models.User, action: str, model_id: str) -> None:
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=actor.id,
        username=str(actor.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"setting": "local_whisper_model", "action": action, "model": model_id},
    )


def _state_model(raw: dict, lock_held: bool) -> LocalModelDownloadState:
    """The stored state as a response; a stale 'active' state with no lock reads as failed."""
    status, code = raw["status"], raw.get("error_code")
    if status in ACTIVE_STATUSES and not lock_held:
        status, code = "failed", "download_failed"  # the worker died; its lock expired
    return LocalModelDownloadState(
        model_id=raw["model_id"],
        status=status,
        bytes_done=int(raw.get("bytes_done") or 0),
        bytes_total=raw.get("bytes_total"),
        percent=raw.get("percent"),
        error_code=code,
        updated_at=datetime.fromtimestamp(raw.get("updated_at") or time.time(), tz=UTC),
    )


def _current_download() -> LocalModelDownloadState | None:
    """The download state, or None; Redis being down must not break the model list."""
    try:
        store = _store()
        raw = store.read_state()
        if not raw or "model_id" not in raw or "status" not in raw:
            return None
        return _state_model(raw, store.current_lock() is not None)
    except Exception as exc:
        logger.warning("Could not read the model download state: %s", type(exc).__name__)
        return None


def _active_model() -> str:
    from app.utils.whisper_model_choice import deployment_model_name

    return deployment_model_name()


@router.get("", response_model=LocalModelCatalogResponse)
def list_local_models(
    current_user: models.User = Depends(get_current_active_superuser),
) -> LocalModelCatalogResponse:
    """The catalog with what is downloaded, the download in flight and the free disk space."""
    refuse_on_lite("local_asr_unavailable")
    active = catalog.canonical_id(_active_model())
    entries = [
        LocalModelEntry(
            id=m.id,
            display_name=m.display_name,
            repo_id=m.repo_id,
            approx_bytes=m.approx_bytes,
            vram_hint_gb=m.vram_hint_gb,
            language_support=m.language_support,
            supports_translation=m.supports_translation,
            is_default=m.is_default,
            downloaded=cache.is_complete(m.repo_id),
            partial=cache.has_partial(m.repo_id),
            present_bytes=cache.present_bytes(m.repo_id),
            is_active=m.id == active,
        )
        for m in catalog.LOCAL_WHISPER_MODELS
    ]
    return LocalModelCatalogResponse(
        models=entries,
        download=_current_download(),
        free_bytes=cache.free_bytes(),
        cache_writable=cache.cache_writable(),
        active_model=_active_model(),
    )


@router.post("/download", status_code=202, response_model=LocalModelDownloadState)
@limiter.limit("5/minute")
def start_local_model_download(
    request: Request,
    body: LocalModelDownloadRequest,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> LocalModelDownloadState:
    """Queue the download of one catalog model. One download runs at a time, deployment-wide."""
    refuse_on_lite("local_asr_unavailable")
    model_id = catalog.canonical_id(body.model_id.strip())
    entry = catalog.get(model_id) if model_id else None
    if entry is None:
        raise _error(400, "unknown_local_model")
    if cache.is_complete(entry.repo_id):
        raise _error(409, "local_model_already_downloaded")
    if not cache.cache_writable():
        raise _error(409, "cache_not_writable")
    required = entry.approx_bytes - cache.present_bytes(entry.repo_id) + DISK_HEADROOM_BYTES
    free = cache.free_bytes()
    if free < required:
        raise _error(409, "insufficient_disk_space", required_bytes=required, free_bytes=free)

    from app.tasks.local_model_download import download_local_model_task

    task_id = str(uuid.uuid4())
    try:
        store = _store()
        acquired = store.try_acquire(entry.id, task_id, current_user.id)
    except Exception as exc:  # fail closed: without the lock two downloads could collide
        logger.error("Could not take the model download lock: %s", type(exc).__name__)
        raise _error(503, "model_download_unavailable") from None
    if not acquired:
        held = store.current_lock() or {}
        raise _error(409, "model_download_in_progress", model_id=held.get("model_id"))
    try:
        raw = store.write_state(
            replace=True,
            model_id=entry.id,
            status="queued",
            bytes_done=0,
            bytes_total=entry.approx_bytes,
            percent=0.0,
            task_id=task_id,
            user_id=current_user.id,
        )
        download_local_model_task.apply_async(args=[entry.id, current_user.id], task_id=task_id)
    except Exception as exc:
        logger.error("Could not queue the model download: %s", type(exc).__name__)
        store.release(task_id)
        raise _error(503, "model_download_unavailable") from None
    _audit(request, current_user, "download_started", entry.id)
    return _state_model(raw, lock_held=True)


@router.post("/download/cancel", response_model=LocalModelDownloadState)
@limiter.limit("10/minute")
def cancel_local_model_download(
    request: Request,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    current_user: models.User = Depends(get_current_active_superuser),
) -> LocalModelDownloadState:
    """Stop the download in flight; its partial files are removed and the lock is released."""
    refuse_on_lite("local_asr_unavailable")
    try:
        store = _store()
        lock = store.current_lock()
    except Exception as exc:
        logger.error("Could not read the model download lock: %s", type(exc).__name__)
        raise _error(503, "model_download_unavailable") from None
    if lock is None:
        raise _error(404, "no_model_download")
    task_id, model_id = str(lock["task_id"]), str(lock["model_id"])
    store.request_cancel(task_id)
    raw = store.read_state() or {"model_id": model_id, "status": "queued"}
    if raw.get("status") == "queued":
        # Not started: revoke the message (without terminate, nothing is running to terminate)
        # and settle the state here. If the task starts anyway it sees the flag first and exits.
        from app.core.celery import celery_app

        celery_app.control.revoke(task_id)
        raw = store.write_state(status="cancelled")
        store.release(task_id)
    _audit(request, current_user, "download_cancelled", model_id)
    return _state_model(raw, lock_held=True)
