"""Startup hooks the API lifespan runs inline or as background tasks.

Neural-search bootstrap fast path, usage-hook registration, PII / reranker warm-ups,
native diarizer provisioning and the boot-time status log lines. Split out of
``app/main.py``; the lifespan imports each name and invokes it.
"""

import asyncio
import logging

from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.constants import NEURAL_BOOTSTRAP_LOCK_TIMEOUT_SECONDS
from app.core.constants import NEURAL_BOOTSTRAP_STARTUP_DELAY_SECONDS

logger = logging.getLogger(__name__)


# NOTE: Search settings (model ID, dimension) are now managed by OpenSearch
# neural search via ml_model_service.py and persisted in system_settings table.
# No need to load them into runtime config - they're read directly from DB when needed.


async def _initialize_neural_search():
    """Startup fast path for the neural-search bootstrap (issue #625).

    Waits for OpenSearch to come up, then runs the same idempotent
    :func:`~app.services.search.neural_bootstrap.ensure_neural_search_bootstrap` that
    ``app.tasks.search_maintenance_task.neural_search_bootstrap_task`` runs every 10 minutes
    from Celery beat. This is no longer the only attempt: a cold or slow OpenSearch boot that
    outlasts this one shot self-heals on the next beat tick instead of losing neural search
    permanently. All the actual bootstrap logic (managed-mode adoption, ML settings,
    local-model scan, download, deploy, pipeline) lives in that module now — see
    ``backend/app/services/search/CLAUDE.md``.

    Elected via ``run_once_per_boot`` so N replicas booting together don't all race the same
    expensive OpenSearch calls.
    """
    if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
        logger.info("Neural search disabled, skipping initialization")
        return

    try:
        from app.utils.boot_once import run_once_per_boot

        if not run_once_per_boot("neural_search_bootstrap"):
            return

        # Wait for OpenSearch to be ready
        await asyncio.sleep(NEURAL_BOOTSTRAP_STARTUP_DELAY_SECONDS)

        from app.services.search.neural_bootstrap import ensure_neural_search_bootstrap
        from app.tasks.search_maintenance_task import NEURAL_BOOTSTRAP_LOCK_KEY
        from app.utils.task_lock import task_lock_manager

        # `run_once_per_boot` above only stops N replicas booting together from
        # racing EACH OTHER. It does nothing about this startup path racing the
        # beat self-heal (`neural_search_bootstrap_task`, every 10 minutes) on the
        # SAME replica -- that race is what let one caller deploy a model the other
        # was still registering, deleting the in-flight registration's cache
        # directory (see `ml_model_service._DEPLOYABLE_STATES`'s docstring). Taking
        # the beat task's own lock here means only one of the two callers ever runs
        # the bootstrap sequence at a time.
        with task_lock_manager.acquire_lock(
            NEURAL_BOOTSTRAP_LOCK_KEY, timeout=NEURAL_BOOTSTRAP_LOCK_TIMEOUT_SECONDS
        ) as acquired:
            if not acquired:
                logger.info(
                    "Neural bootstrap already running (beat self-heal in progress); "
                    "startup path skipping"
                )
                return
            result = await run_in_threadpool(ensure_neural_search_bootstrap)

        if result.state == "ok":
            logger.info(f"Neural search bootstrap ready (model={result.model_id})")
        elif result.state == "disabled":
            logger.info("Neural search bootstrap: disabled")
        else:
            logger.warning(
                "Neural search bootstrap degraded at startup (stage=%s: %s); "
                "the beat self-heal will retry.",
                result.stage,
                result.detail,
            )

    except Exception as e:
        logger.error(f"Error initializing neural search: {e}")


def _register_chat_usage_hook() -> None:
    """Install the core chat usage recorder.

    Registered in EVERY edition — a self-hosted operator paying an LLM bill wants
    the same visibility a hosted tenant does. The cloud edition registers its own
    billing hook alongside this one; the two are independent.
    """
    try:
        from app.services.chat.usage import register as register_chat_usage

        register_chat_usage()
    except Exception as e:  # noqa: BLE001 — accounting never blocks startup
        logger.warning(f"Chat usage hook registration failed (non-fatal): {e}")


def _start_pii_warmup() -> None:
    """Warm Presidio in the API process if this deployment actually redacts (issue #74).

    The API process runs three inline maskers — chat's fail-closed fallback, output
    redaction, and segment-edit re-detection — and the first of them in a fresh
    process paid a ~10 s ``AnalyzerEngine`` build on a user-facing request.

    The gate query *and* the build both run on a daemon thread, so the lifespan pays
    only ``Thread.start()`` (measured: 0.53 ms) and the backend's healthcheck window
    is untouched. Deployments where nobody enabled redaction build nothing at all.
    Never a startup dependency: Presidio is optional and its callers fail closed.
    """
    try:
        from app.services.redaction.warmup import start_pii_warmup

        start_pii_warmup()
    except Exception as e:  # noqa: BLE001 — an optimisation never blocks startup
        logger.warning(f"PII analyzer warm-up could not start (non-fatal): {e}")


def _start_reranker_warmup() -> None:
    """Load the chat reranker on a daemon thread (issue #1269).

    The first chat in a fresh API process paid a ~10-15 s cold load of the CPU
    cross-encoder. The settings gate and the load both run on the thread, so the
    lifespan pays only ``Thread.start()``. Never a startup dependency.
    """
    try:
        from app.services.chat.reranker_warmup import start_reranker_warmup

        start_reranker_warmup()
    except Exception as e:  # noqa: BLE001 — an optimisation never blocks startup
        logger.warning(f"Chat reranker warm-up could not start (non-fatal): {e}")


def _provision_native_diarizer() -> None:
    """Export the native diarizer's model set if it is not already vouched for.

    Wrapped so an import-time failure — a lite image with no such module, say — is as
    survivable as a provisioning failure. ``ensure_native_models`` never raises on its
    own; this guards everything around it.
    """
    try:
        from app.transcription.native_provision import ensure_native_models

        ensure_native_models()
    except Exception as e:  # noqa: BLE001 — a degraded diarizer never blocks startup
        logger.warning(f"diar-native provisioning could not run (non-fatal): {e}")


def _log_password_blocklist_status() -> None:
    """One WARNING when the breached-password check is on but no list is installed.

    Never fatal: the check is skipped and the rest of the password policy still applies.
    """
    try:
        from app.auth import password_blocklist
        from app.auth.password_policy import password_policy

        password_blocklist.log_startup_status(
            password_policy.blocklist_enabled, str(settings.PASSWORD_BLOCKLIST_PATH)
        )
    except Exception as e:
        logger.warning(f"Password blocklist status check failed (non-fatal): {e}")


def _log_tenancy_mode_at_startup() -> None:
    """Log the resolved tenancy mode, warning when TENANCY_MODE=single hides active orgs."""
    from app.db.session_utils import session_scope
    from app.services.platform_access import tenancy_mode
    from app.services.platform_access import warn_if_forced_single_with_orgs

    try:
        with session_scope() as db:
            logger.info("Tenancy mode: %s", tenancy_mode(db).value)
            warn_if_forced_single_with_orgs(db)
    except Exception as e:
        logger.warning(f"Tenancy mode startup check failed (non-fatal): {e}")
