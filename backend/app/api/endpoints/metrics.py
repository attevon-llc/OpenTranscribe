"""Prometheus scrape endpoint.

Mounted at the application ROOT (no ``/api`` prefix), next to ``/health``.
Unauthenticated by design — parity with ``/health``, no sensitive payload,
nginx-denied (``location = /metrics { deny all; return 404; }``), and the host
port is LAN-only. Prometheus scrapes ``backend:8080/metrics`` over the compose
network.

Defined as a plain ``def`` so Starlette runs it in the threadpool — the sync
``LLEN`` calls in ``update_queue_depths`` never block the event loop.
"""

from fastapi import APIRouter
from prometheus_client import CONTENT_TYPE_LATEST
from prometheus_client import REGISTRY
from prometheus_client import generate_latest
from starlette.responses import Response

from app.core.backup_metrics import update_backup_metrics
from app.core.backup_metrics import update_media_mirror_metrics
from app.core.celery_metrics import update_queue_depths

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Render all registered collectors in the Prometheus text exposition format."""
    update_queue_depths()
    update_backup_metrics()
    update_media_mirror_metrics()
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
