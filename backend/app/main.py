# Telemetry opt-outs must be in os.environ before anything can import pyannote or
# huggingface_hub (both read them at import). Keep this the first import.
from app.core import privacy_env  # noqa: F401  # isort: skip

# isort: split
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import Response

from app.api.router import api_router
from app.auth.rate_limit import limiter
from app.auth.rate_limit import rate_limit_exceeded_handler
from app.core.config import settings
from app.core.exceptions import ASRConfigurationError
from app.core.exceptions import DomainError
from app.core.exceptions import EmailDeliveryError
from app.core.exceptions import LLMServiceError
from app.core.exceptions import OpenTranscribeError
from app.core.exceptions import SearchIndexError
from app.core.exceptions import StorageError
from app.core.logging_config import configure_logging
from app.core.startup_validation import _validate_production_secrets
from app.core.validation_errors import validation_error_handler
from app.core.version import APP_VERSION
from app.middleware.audit import AuditMiddleware
from app.middleware.csrf import CSRFMiddleware
from app.middleware.observability import ObservabilityMiddleware
from app.middleware.robots import RobotsHeaderMiddleware
from app.middleware.upload_limit import UploadBodyLimitMiddleware
from app.startup.maintenance import _clear_stale_task_state
from app.startup.maintenance import _run_imohash_recompute
from app.startup.maintenance import _run_one_time_embedding_normalization
from app.startup.maintenance import _run_search_maintenance
from app.startup.maintenance import _run_startup_recovery
from app.startup.maintenance import _run_thumbnail_migration
from app.startup.maintenance import _setup_minio
from app.startup.warmups import _initialize_neural_search
from app.startup.warmups import _log_password_blocklist_status
from app.startup.warmups import _log_tenancy_mode_at_startup
from app.startup.warmups import _provision_native_diarizer
from app.startup.warmups import _register_chat_usage_hook
from app.startup.warmups import _start_pii_warmup
from app.startup.warmups import _start_reranker_warmup

# Set up logging (text or structured JSON per settings.LOG_FORMAT)
configure_logging()
logger = logging.getLogger(__name__)


async def _drain_websockets() -> None:
    """Close live WebSockets on shutdown so the process doesn't hang to SIGKILL.

    See ``ConnectionManager.drain`` (issue #284 A1.21). Never raises — a failure here
    must not block shutdown.
    """
    try:
        from app.api.websockets import manager

        await manager.drain()
    except Exception as e:  # noqa: BLE001 - never block shutdown on drain
        logger.warning(f"WebSocket drain failed (non-fatal): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager for startup and shutdown events."""
    # First, and in test mode too: the thread limiter belongs to this event loop, and
    # every request it serves depends on its size relative to the DB pool (#1169).
    from app.core.threadpool import configure_api_threadpool

    configure_api_threadpool()

    if os.environ.get("TESTING", "").lower() == "true":
        logger.info("Test mode: skipping startup tasks")
        yield
        return

    logger.info("Starting application...")
    _validate_production_secrets()

    # See app.core.celery: nltk >= 3.10.4 reads a group/world-writable data tree as absent.
    from app.utils.nltk_offline import ensure_nltk_data_private

    ensure_nltk_data_private()

    # Migrations run on startup for self-host (a single container owns the DB). On
    # an orchestrated deploy a dedicated migrate Job owns them instead, and every API
    # replica racing Alembic is exactly what RUN_MIGRATIONS_ON_STARTUP=false prevents
    # (issue #284 A1.4). When gated off we do NOT silently trust the DB: readiness
    # asserts the schema is at head and fails 503 otherwise, so a replica started
    # against an un-migrated database never takes traffic.
    if settings.RUN_MIGRATIONS_ON_STARTUP:
        from app.db.migrations import run_migrations

        try:
            run_migrations()
        except Exception as e:
            logger.critical(f"Database migration failed — aborting startup: {e}")
            raise SystemExit(1) from e
    else:
        logger.info(
            "RUN_MIGRATIONS_ON_STARTUP=false — skipping migrations; a migrate job is "
            "expected to own them. Readiness will verify the schema is at head."
        )

    _log_tenancy_mode_at_startup()

    # Export the native diarizer's ONNX/PLDA set before anything can ask for a
    # diarization. The weights are gated and non-redistributable, so every deployment
    # converts them locally once; that conversion is what the native engine's speed
    # comes from, and without it the pipeline silently runs in-process PyAnnote
    # (issues #654, #639). Idempotent — a valid marker makes this a stat pass, so the
    # cost is paid on the first boot only.
    #
    # Deliberately synchronous: the diar-native service waits on this container's
    # health, so finishing here is what guarantees the sidecar never starts against an
    # empty models directory and crash-loops on exit 8. The backend healthcheck already
    # allows a 600 s start_period, comfortably over the 137 s a cold export measured.
    #
    # Never fatal. A failure leaves /readyz at 503 and diarization falls back to
    # PyAnnote, which is supported; the loud refusal belongs in the installer's upgrade
    # preflight where an operator can still act (issue #670).
    _provision_native_diarizer()

    _register_chat_usage_hook()

    _log_password_blocklist_status()

    # Seed initial data (admin user, default tags, system prompts)
    try:
        from app.db.base import SessionLocal
        from app.initial_data import init_db

        db = SessionLocal()
        try:
            init_db(db)
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Initial data seeding failed (non-fatal): {e}")

    # Check OpenSearch index health (auto-repair corrupted shards from unclean shutdowns).
    #
    # ensure_*_exist are idempotent no-ops once the indices are there, so every replica
    # may run them. check_and_repair_indices is NOT cheap and acts on shared cluster
    # state, so it is elected — N replicas repairing the same indices concurrently is
    # wasted work at best (issue #284 A1.15).
    try:
        from app.services.opensearch_service import check_and_repair_indices
        from app.services.opensearch_service import ensure_indices_exist
        from app.services.opensearch_service import ensure_v4_index_exists
        from app.services.search.index_health import check_and_repair_chunks_index
        from app.utils.boot_once import run_once_per_boot

        ensure_indices_exist()
        ensure_v4_index_exists()
        if run_once_per_boot("opensearch_repair_indices"):
            check_and_repair_indices()
            # The chunk plane's health lives one layer up, in services/search —
            # its repair needs the reindex coordinator, and importing that from
            # opensearch_service forms a package cycle (issue #540).
            check_and_repair_chunks_index()
    except Exception as e:
        logger.warning(f"OpenSearch startup health check failed (non-fatal): {e}")

    # Sync speaker profile data from PostgreSQL to OpenSearch
    try:
        from app.db.base import SessionLocal
        from app.services.opensearch_service import sync_speaker_profiles_to_opensearch

        _db = SessionLocal()
        try:
            sync_result = sync_speaker_profiles_to_opensearch(_db)
            if sync_result["updated"] > 0:
                logger.info(f"Speaker profile sync: {sync_result}")
        finally:
            _db.close()
    except Exception as e:
        logger.warning(f"Speaker profile sync failed (non-fatal): {e}")

    # Apply the admin-configured derived-cache retention (DB over env) so the MinIO
    # lifecycle rule reflects UI changes after a restart — no redeploy needed. Also
    # run a one-time reclaim of pre-prefix (legacy) derived assets for upgraders.
    try:
        from app.db.base import SessionLocal
        from app.services import cache_management_service
        from app.services import system_settings_service

        _db = SessionLocal()
        try:
            days = cache_management_service.apply_retention(_db)
            logger.info(f"Derived-cache retention applied: {days} day(s)")

            reclaimed_key = "cache.legacy_derived_reclaimed"
            if not system_settings_service.get_setting_bool(_db, reclaimed_key, False):
                count = cache_management_service.reclaim_legacy_derived_cache()
                system_settings_service.set_setting(
                    _db,
                    reclaimed_key,
                    True,
                    "One-time reclaim of pre-prefix derived cache objects has run",
                )
                if count:
                    logger.info(f"Upgrade reclaim: removed {count} legacy derived cache object(s)")
        finally:
            _db.close()
    except Exception as e:
        logger.warning(f"Derived-cache retention/reclaim setup failed (non-fatal): {e}")

    # Clear stale migration state from Redis (orphaned by unclean shutdown).
    #
    # Elected, because this deletes ALL task_progress:* keys and every coordination
    # lock. Unelected, the second replica to boot wipes progress and locks belonging to
    # work the first replica is actively coordinating (issue #284 A1.3).
    try:
        from app.utils.boot_once import run_once_per_boot

        if run_once_per_boot("clear_stale_task_state"):
            _clear_stale_task_state()
    except Exception as e:
        logger.warning(f"Migration state cleanup failed (non-fatal): {e}")

    _start_pii_warmup()
    _start_reranker_warmup()

    logger.info("Setting up MinIO and task recovery...")
    minio_task = asyncio.create_task(run_in_threadpool(_setup_minio))
    recovery_task = asyncio.create_task(_run_startup_recovery())
    search_maintenance = asyncio.create_task(_run_search_maintenance())
    thumbnail_migration = asyncio.create_task(_run_thumbnail_migration())
    neural_search_task = asyncio.create_task(_initialize_neural_search())
    embedding_migration = asyncio.create_task(_run_one_time_embedding_normalization())
    imohash_recompute_task = asyncio.create_task(_run_imohash_recompute())

    yield

    logger.info("Shutting down application...")

    await _drain_websockets()

    for task in [
        minio_task,
        recovery_task,
        search_maintenance,
        thumbnail_migration,
        neural_search_task,
        embedding_migration,
        imohash_recompute_task,
    ]:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def _resolve_docs_urls() -> tuple[str | None, str | None, str | None]:
    """Resolve the OpenAPI schema, Swagger UI, and ReDoc URLs.

    Swagger UI is anonymously reachable wherever it is mounted (nginx proxies
    ``/api/`` straight through), and it enumerates the whole admin/auth attack
    surface. A hardened deployment therefore publishes none of the three; set
    ``ENABLE_API_DOCS=true`` to opt back in.

    Returns:
        The three URLs, or ``(None, None, None)`` when the docs are disabled.
    """
    opted_in = os.getenv("ENABLE_API_DOCS", "").strip().lower() in ("1", "true", "yes")
    if settings.is_hardened and not opted_in:
        return None, None, None
    return (
        f"{settings.API_PREFIX}/openapi.json",
        f"{settings.API_PREFIX}/docs",
        f"{settings.API_PREFIX}/redoc",
    )


_openapi_url, _swagger_url, _redoc_url = _resolve_docs_urls()

# Create FastAPI app with lifespan and consistent routing configuration
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Audio transcription and analysis API",
    version=APP_VERSION,
    openapi_url=_openapi_url,
    docs_url=_swagger_url,
    redoc_url=_redoc_url,
    lifespan=lifespan,
    # Disable redirect_slashes to prevent 307 redirects that expose Docker internal hostnames
    # Routes should be defined with "" (not "/") to match paths without trailing slash
    redirect_slashes=False,
    # Increase default timeout to 1 hour for large file uploads
    default_timeout=3600,
)

# Upload size limits enforced while the body streams (issue #999). Added BEFORE CORS so
# it runs INSIDE it (see the ordering note at ObservabilityMiddleware): its early 413
# still carries the CORS headers a browser needs to read the error.
app.add_middleware(UploadBodyLimitMiddleware)

# Set up CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "Accept",
        "Accept-Language",
        "X-Request-ID",
        "X-CSRF-Token",
    ],
    # Defence in depth (issue #788): the SPA is same-origin in dev (Vite proxy)
    # and prod (nginx), so a browser can already read these response headers
    # without this — but exposing them removes a trap for any future
    # split-host or third-party client that isn't.
    expose_headers=["Retry-After", "X-RateLimit-Limit", "Content-Disposition"],
)

# Mark every API response as non-indexable (issue #668). Response-header-only,
# so its position relative to the other middleware below is not load-bearing.
app.add_middleware(RobotsHeaderMiddleware)

# Add Audit Middleware for request ID tracking (FedRAMP AU-2/AU-3)
app.add_middleware(AuditMiddleware)

# CSRF protection for cookie-based authentication (C2 security hardening)
app.add_middleware(CSRFMiddleware)

# Observability MUST be the LAST add_middleware call so it runs OUTERMOST.
# Starlette semantics (verified 0.48.0): add_middleware does
# `user_middleware.insert(0, ...)` and the stack is built over `reversed(...)`,
# so the LAST-added middleware is the OUTERMOST — NOT the add-call order. The
# resulting runtime order is: Observability -> CSRF -> Audit -> CORS -> router.
# That lets us record CSRF 403 rejections plus every routed request, and read
# request.state.{request_id,user_id,client_ip} (set by inner layers during
# call_next on the shared scope) after call_next returns. Do NOT "fix" the
# order by reordering these calls.
app.add_middleware(ObservabilityMiddleware)


# Global handler for the application exception hierarchy.
# Maps domain-specific exceptions to appropriate HTTP status codes so that
# any ``OpenTranscribeError`` raised in endpoint code is automatically
# serialised as a structured JSON response.
#
# Deliberately `async def` even though it never awaits, which is the one shape the
# issue #320 sweep leaves alone: the body is a dict lookup and a JSONResponse
# construction, with no I/O to block the loop. Starlette does accept a plain `def`
# handler, but it dispatches one through `run_in_threadpool`, so declaring it `def`
# would buy a thread hop on every error response and free nothing.
@app.exception_handler(OpenTranscribeError)
async def handle_app_error(request, exc: OpenTranscribeError):
    status_map = {
        StorageError: 503,
        SearchIndexError: 503,
        LLMServiceError: 502,
        EmailDeliveryError: 503,
        ASRConfigurationError: 503,
    }
    # Walk the MRO so a subclass (speaker_cleanup's SearchIndexError) keeps its family's code.
    status = next((status_map[c] for c in type(exc).__mro__ if c in status_map), 500)
    return JSONResponse(
        status_code=status,
        content={"detail": exc.message},
    )


# A DomainError is a deliberate refusal raised below the API layer. Delegating to FastAPI's own
# HTTPException handler makes status, body bytes, headers and the body-less-status branch those of
# the HTTPException it replaces, by construction. Starlette picks the handler by walking the MRO,
# so this beats handle_app_error above for every DomainError subclass.
@app.exception_handler(DomainError)
async def handle_domain_error(request: Request, exc: DomainError) -> Response:
    return await http_exception_handler(
        request, HTTPException(status_code=exc.status_code, detail=exc.detail, headers=exc.headers)
    )


# FastAPI's default 422, with any echoed credential removed from the body (core/validation_errors).
app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]

# Include the API router
app.include_router(api_router, prefix=settings.API_PREFIX)

# Set up rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)  # type: ignore[arg-type]


# Prometheus scrape endpoint, mounted at ROOT (no API_PREFIX) next to /health.
# Unauthenticated by design — nginx denies it and the host port is LAN-only.
from app.api.endpoints.metrics import router as metrics_router  # noqa: E402

app.include_router(metrics_router)

# SCIM 2.0 provisioning, mounted at ROOT rather than under API_PREFIX: RFC 7644 §3.1
# fixes the base path, and every IdP connector appends "/Users" to whatever base URL
# it is given. Its errors must be SCIM Error resources, not FastAPI's {"detail": ...},
# or an administrator reading their connector log sees an opaque status code.
from app.api.endpoints.scim import router as scim_router  # noqa: E402
from app.api.endpoints.scim.errors import SCIMError  # noqa: E402
from app.api.endpoints.scim.errors import scim_error_handler  # noqa: E402

app.include_router(scim_router)
app.add_exception_handler(SCIMError, scim_error_handler)


# Health check endpoint
@app.get("/health")
def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "version": APP_VERSION}


@app.get("/health/ready")
def readiness_check():
    """Readiness probe for load balancers / orchestrators.

    Probes the critical dependencies (PostgreSQL, Redis) and the degraded-but-
    serviceable ones (OpenSearch, MinIO). A failed CRITICAL dependency returns
    503; OpenSearch/MinIO failures are reported but do not fail readiness, since
    queued transcription survives a brief search/storage outage. Plain ``def``
    so Starlette threadpools the short, synchronous probes.

    Probed several times a minute, so it must stay cheap and bounded (issue #1000):
    Redis, OpenSearch and object storage each get ``DEPENDENCY_PROBE_TIMEOUT_SECONDS``
    via probe-only clients, and the Alembic head is parsed once per process.
    """
    from sqlalchemy import text

    from app.core.constants import DEPENDENCY_PROBE_TIMEOUT_SECONDS

    checks: dict[str, str] = {}

    # PostgreSQL (critical) — short-lived session, SELECT 1.
    try:
        from app.db.base import SessionLocal

        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        checks["postgres"] = f"error: {type(exc).__name__}"

    # Redis (critical) — db 0 through the time-bounded probe client.
    try:
        from app.core.redis import get_probe_redis

        get_probe_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {type(exc).__name__}"

    # OpenSearch (degraded-but-ready) — reuse the existing client singleton.
    try:
        from app.services.opensearch_service import get_opensearch_client

        client = get_opensearch_client()
        if client is not None and client.ping(request_timeout=DEPENDENCY_PROBE_TIMEOUT_SECONDS):
            checks["opensearch"] = "ok"
        else:
            checks["opensearch"] = "unavailable"
    except Exception as exc:  # noqa: BLE001
        checks["opensearch"] = f"error: {type(exc).__name__}"

    # MinIO (degraded-but-ready) — same backend, time-bounded single-attempt transport.
    try:
        from app.services.minio_service import get_probe_client

        get_probe_client().bucket_exists(settings.MEDIA_BUCKET_NAME)
        checks["minio"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["minio"] = f"error: {type(exc).__name__}"

    # Schema freshness. ALWAYS reported, conditionally critical.
    #
    # Reported unconditionally so the schema state of any deployment is
    # observable over HTTP — the release harness reads it to assert an upgrade
    # actually migrated, instead of shelling `docker exec opentranscribe-postgres
    # psql`, which hardcodes a container name and breaks on --fresh stacks.
    #
    # Critical ONLY when a migrate job owns migrations (issue #284 A1.4): a
    # replica that did not run Alembic itself must prove the DB is at head before
    # taking traffic, or a rollout outpacing the migrate job serves requests
    # against an old schema. When this replica DOES run migrations on startup,
    # a stale schema cannot happen without startup having already failed, and
    # 503-ing on it would change self-host readiness semantics.
    schema_detail: dict[str, str] = {}
    try:
        from alembic.migration import MigrationContext

        from app.db.base import engine
        from app.db.migrations import get_alembic_head

        head = get_alembic_head()
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        checks["schema"] = "ok" if current == head else f"stale: {current} != head {head}"
        schema_detail = {"current": current or "none", "head": head or "none"}
    except Exception as exc:  # noqa: BLE001
        checks["schema"] = f"error: {type(exc).__name__}"

    if schema_detail:
        checks["schema_revision"] = schema_detail["current"]
        checks["schema_head"] = schema_detail["head"]

    schema_is_critical = not settings.RUN_MIGRATIONS_ON_STARTUP
    critical_ok = (
        checks["postgres"] == "ok"
        and checks["redis"] == "ok"
        and (not schema_is_critical or checks.get("schema", "ok") == "ok")
    )
    if not critical_ok:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": checks},
        )
    return {"status": "ready", "checks": checks}


# Static files are served by nginx in production, not by FastAPI
# Removed conflicting static file mounting to prevent nginx conflicts

# Run the application if executed directly
if __name__ == "__main__":
    import uvicorn

    # Binding to 0.0.0.0 is required for Docker containers
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)  # noqa: S104 # nosec B104
