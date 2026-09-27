"""Prometheus scrape endpoints.

Mounted at the application ROOT (no ``/api`` prefix), next to ``/health``.
Unauthenticated by design — parity with ``/health``, no sensitive payload,
nginx-denied (``location = /metrics { deny all; return 404; }``, and the same for
``/metrics/queues``), and the host port is LAN-only. Prometheus scrapes
``backend:8080/metrics`` over the compose network.

- ``/metrics`` — every collector. Queue gauges are sampled per scrape; the
  backup/media-mirror projection is DB-backed and refreshed at most once per
  ``backup_metrics.JOB_METRICS_TTL_SECONDS`` (issue #1001).
- ``/metrics/queues`` — ONLY ``celery_queue_depth`` / ``celery_queue_reserved``,
  from one pipelined Redis round trip and no database access. Sized for
  autoscalers that poll every few seconds and need nothing else; the full page
  renders every HTTP histogram series in the process.

Defined as plain ``def`` so Starlette runs them in the threadpool — the sync
Redis pipeline in ``update_queue_depths`` never blocks the event loop.
"""

from fastapi import APIRouter
from prometheus_client import CONTENT_TYPE_LATEST
from prometheus_client import REGISTRY
from prometheus_client import CollectorRegistry
from prometheus_client import generate_latest
from starlette.responses import Response

from app.core.backup_metrics import refresh_job_metrics
from app.core.celery_metrics import update_queue_depths
from app.core.metrics import celery_queue_depth
from app.core.metrics import celery_queue_reserved

router = APIRouter()

# The same two Gauge objects the default registry holds, rendered on their own so
# the queue page never walks the rest of the process's collectors.
_QUEUE_REGISTRY = CollectorRegistry(auto_describe=True)
_QUEUE_REGISTRY.register(celery_queue_depth)
_QUEUE_REGISTRY.register(celery_queue_reserved)


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Render all registered collectors in the Prometheus text exposition format."""
    update_queue_depths()
    refresh_job_metrics()
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@router.get("/metrics/queues", include_in_schema=False)
def queue_metrics() -> Response:
    """Render only the Celery queue gauges (Prometheus text format) for autoscalers."""
    update_queue_depths()
    return Response(generate_latest(_QUEUE_REGISTRY), media_type=CONTENT_TYPE_LATEST)
