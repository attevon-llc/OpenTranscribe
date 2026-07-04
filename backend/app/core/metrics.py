"""Prometheus metric collectors for the FastAPI backend.

All collectors are registered on the **default** ``prometheus_client.REGISTRY``
at import time, exactly once. The middleware (``app.middleware.observability``)
and the ``/metrics`` endpoint (``app.api.endpoints.metrics``) read these.

Cardinality rules (do NOT relitigate — bounded label sets only):
  - NEVER use ``user_id``, raw request paths, query strings, or SQL text as a
    label value. Routes are the post-routing **template** (e.g.
    ``/api/files/{file_id}``), which is bounded by the route table.
  - Per-user / per-org analytics live in the structured access log + the
    Grafana PostgreSQL datasource, never in Prometheus labels.

Single-process registry is valid because production runs ONE uvicorn process
(``backend/Dockerfile.prod`` CMD has no ``--workers``). If workers are ever
added, migrate to ``prometheus_client.multiprocess`` with a
``PROMETHEUS_MULTIPROC_DIR`` — a plain in-process registry would then under-
report (each worker scraped independently).

Worker-process trap: Celery workers have their own registries that are never
scraped. A counter incremented inside a task is invisible. Product counters
here (``user_signups_total``, ``files_uploaded_total``) are incremented at
API-process call sites only; worker-side product events are dashboarded from
the database instead.

Test-reimport guard: importing this module twice in one process (some pytest
collection orders) would raise ``Duplicated timeseries``. Collectors are built
under a module-level ``_COLLECTORS_REGISTERED`` flag so a re-import is a no-op
and reuses the already-registered objects.
"""

from prometheus_client import Counter
from prometheus_client import Gauge
from prometheus_client import Histogram

# Latency buckets. The 120/300/600 tail exists because uploads/long requests can
# run for minutes — without those buckets p99 saturates at +Inf and hides real
# tail latency.
_HTTP_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
)

# DB query duration: 1ms → 10s. No statement/table labels (cardinality).
_DB_DURATION_BUCKETS = (
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

# The duplicate-call detector: how many DB statements ran per HTTP request.
_DB_QUERIES_PER_REQUEST_BUCKETS = (0, 1, 2, 5, 10, 25, 50, 100, 250)


_COLLECTORS_REGISTERED = False

# Declared here so module-level names exist for type checkers; populated by
# ``_register()`` exactly once on first import.
http_request_duration_seconds: Histogram
http_requests_total: Counter
http_requests_in_flight: Gauge
db_query_duration_seconds: Histogram
db_queries_per_request: Histogram
cache_operations_total: Counter
celery_queue_depth: Gauge
user_signups_total: Counter
files_uploaded_total: Counter
backup_runs_total: Counter
backup_last_success_timestamp_seconds: Gauge
backup_last_status: Gauge


def _register() -> None:
    """Build collectors on the default REGISTRY exactly once (re-import safe)."""
    global _COLLECTORS_REGISTERED
    global http_request_duration_seconds
    global http_requests_total
    global http_requests_in_flight
    global db_query_duration_seconds
    global db_queries_per_request
    global cache_operations_total
    global celery_queue_depth
    global user_signups_total
    global files_uploaded_total
    global backup_runs_total
    global backup_last_success_timestamp_seconds
    global backup_last_status

    if _COLLECTORS_REGISTERED:
        return

    http_request_duration_seconds = Histogram(
        "http_request_duration_seconds",
        "HTTP request latency in seconds.",
        ["method", "route", "status"],
        buckets=_HTTP_DURATION_BUCKETS,
    )
    http_requests_total = Counter(
        "http_requests_total",
        "Total HTTP requests (5xx error rate derived in Grafana).",
        ["method", "route", "status"],
    )
    http_requests_in_flight = Gauge(
        "http_requests_in_flight",
        "HTTP requests currently being processed.",
    )
    db_query_duration_seconds = Histogram(
        "db_query_duration_seconds",
        "Individual SQL statement execution time in seconds.",
        buckets=_DB_DURATION_BUCKETS,
    )
    db_queries_per_request = Histogram(
        "db_queries_per_request",
        "Number of SQL statements executed during one HTTP request "
        "(the duplicate/N+1 query detector).",
        ["method", "route"],
        buckets=_DB_QUERIES_PER_REQUEST_BUCKETS,
    )
    cache_operations_total = Counter(
        "cache_operations_total",
        "Cache lookups by backend and result.",
        ["cache", "result"],
    )
    celery_queue_depth = Gauge(
        "celery_queue_depth",
        "Pending tasks per Celery queue (priority sub-lists summed).",
        ["queue"],
    )
    user_signups_total = Counter(
        "user_signups_total",
        "User account creations by authentication method (API process only).",
        ["method"],
    )
    files_uploaded_total = Counter(
        "files_uploaded_total",
        "Media files accepted for processing by source (API process only).",
        ["source"],
    )
    # Backup collectors run in a Celery worker, so their state is persisted to
    # SystemSettings by the run task and projected here at scrape time by
    # ``app.core.backup_metrics.update_backup_metrics`` (same sample-at-scrape
    # pattern as celery_queue_depth).
    backup_runs_total = Counter(
        "backup_runs_total",
        "Scheduled/manual database backup runs by result (synced from the DB at scrape).",
        ["result"],
    )
    backup_last_success_timestamp_seconds = Gauge(
        "backup_last_success_timestamp_seconds",
        "Unix timestamp of the last successful database backup "
        "(0 = never; alert on time() - this > N).",
    )
    backup_last_status = Gauge(
        "backup_last_status",
        "1 when the most recent backup run succeeded, 0 when it failed "
        "(0 also before any run — gate alerts on backup_runs_total > 0).",
    )

    _COLLECTORS_REGISTERED = True


_register()
