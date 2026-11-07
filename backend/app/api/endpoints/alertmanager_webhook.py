"""Alertmanager webhook receiver: fans firing alerts out to the admins' notifications.

Mounted at the application ROOT (like ``/metrics``), never under ``/api``. Alertmanager
posts here over the compose network with a bearer token
(``http_config.authorization`` in ``monitoring/alertmanager/alertmanager.yml``;
https://prometheus.io/docs/alerting/latest/configuration/#http_config).

A POST here notifies **every admin**, so it is authenticated and nginx denies the
path (``location ^~ /internal/``). With ``ALERTMANAGER_WEBHOOK_TOKEN`` unset the
endpoint refuses every request (fail closed) rather than accepting anonymous ones.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict
from sqlalchemy.orm import Session

from app.db.base import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/alertmanager", tags=["monitoring"], include_in_schema=False)

EVENT_TYPE = "monitoring_alert"
_MAX_ALERTS = 10
_MAX_TEXT = 200


class _WebhookSettings(BaseSettings):
    """The webhook's own secret, kept out of ``Settings`` (``app/core/config.py`` is at its size cap)."""

    model_config = SettingsConfigDict(extra="ignore")

    ALERTMANAGER_WEBHOOK_TOKEN: str = ""


def _authenticated(request: Request) -> bool:
    expected = _WebhookSettings().ALERTMANAGER_WEBHOOK_TOKEN
    if not expected:
        return False
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(presented.strip().encode("utf-8"), expected.encode("utf-8"))


def _line(alert: dict[str, Any]) -> str:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    name = str(labels.get("alertname", "alert"))
    summary = str(annotations.get("summary", ""))
    return f"{name}: {summary}"[:_MAX_TEXT]


@router.post("/webhook")
def alertmanager_webhook(
    payload: dict[str, Any], request: Request, db: Session = Depends(get_db)
) -> dict[str, int]:
    """Notify admins of the firing alerts in an Alertmanager webhook payload."""
    if not _authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    from app.services.backup_alerts import _notify_admins

    firing = [a for a in (payload.get("alerts") or []) if a.get("status") == "firing"]
    if firing:
        lines = [_line(a) for a in firing[:_MAX_ALERTS]]
        extra = len(firing) - len(lines)
        if extra > 0:
            lines.append(f"... and {extra} more")
        _notify_admins(db, status="failed", message="\n".join(lines), event_type=EVENT_TYPE)
    return {"firing": len(firing)}
