"""Celery task: download one catalog Whisper model into the shared Hugging Face cache (#1246).

Runs on the ``download`` queue (network-bound, mounts the cache read-write, no GPU). The task
body does the work that may not run in a fork hook: it reads the saved Hugging Face token,
touches Redis and the network. The download itself is a supervised child process, see
``services/local_models/supervisor.py``; this module only wires it to Celery and the
administrator's WebSocket.

No ``autoretry_for`` and ``acks_late`` is off: a silent second multi-GB attempt is not something
to do unasked. The administrator presses Download again.
"""

from __future__ import annotations

import logging

from app.core import local_whisper_catalog
from app.core.celery import celery_app
from app.core.constants import NOTIFICATION_TYPE_LOCAL_MODEL_DOWNLOAD
from app.core.redis import get_redis
from app.services.local_models.state import DownloadStore
from app.services.local_models.supervisor import DOWNLOAD_DEADLINE_SECONDS
from app.services.local_models.supervisor import run_download
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)


@celery_app.task(
    name="asr.download_local_model",
    bind=True,
    soft_time_limit=DOWNLOAD_DEADLINE_SECONDS + 600,
    time_limit=DOWNLOAD_DEADLINE_SECONDS + 1500,
)
def download_local_model_task(self, model_id: str, user_id: int) -> dict:
    """Download ``model_id`` and tell the starting administrator how it went.

    Args:
        model_id: Catalog id of the model.
        user_id: Administrator who started the download; receives the progress events.

    Returns:
        ``{"model_id": ..., "status": "completed" | "failed" | "cancelled"}``.
    """
    task_id = self.request.id
    store = DownloadStore(get_redis())
    entry = local_whisper_catalog.get(model_id)
    if entry is None:
        store.write_state(
            replace=True, model_id=model_id, status="failed", error_code="unknown_local_model"
        )
        store.release(task_id)
        return {"model_id": model_id, "status": "failed"}

    from app.services.huggingface_token import get_hf_token

    token = get_hf_token()

    def notify(payload: dict) -> None:
        send_ws_event(user_id, NOTIFICATION_TYPE_LOCAL_MODEL_DOWNLOAD, payload)

    status = run_download(
        entry, task_id=task_id, user_id=user_id, store=store, token=token, notify=notify
    )
    return {"model_id": entry.id, "status": status}
