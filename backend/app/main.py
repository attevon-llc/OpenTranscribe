import asyncio
import logging
import os
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.api.router import api_router
from app.auth.rate_limit import limiter
from app.auth.rate_limit import rate_limit_exceeded_handler
from app.core.config import IMPLEMENTED_ENCRYPTION_ALGORITHMS
from app.core.config import settings
from app.core.constants import NEURAL_BOOTSTRAP_LOCK_TIMEOUT_SECONDS
from app.core.constants import NEURAL_BOOTSTRAP_STARTUP_DELAY_SECONDS
from app.core.entropy import assert_csprng_available
from app.core.entropy import validate_secret_entropy
from app.core.exceptions import AuthenticationError
from app.core.exceptions import EmailDeliveryError
from app.core.exceptions import LLMServiceError
from app.core.exceptions import OpenTranscribeError
from app.core.exceptions import SearchIndexError
from app.core.exceptions import StorageError
from app.core.legacy_auth_env import deprecated_oidc_env_names
from app.core.logging_config import configure_logging
from app.core.version import APP_VERSION
from app.middleware.audit import AuditMiddleware
from app.middleware.csrf import CSRFMiddleware
from app.middleware.observability import ObservabilityMiddleware

# Set up logging (text or structured JSON per settings.LOG_FORMAT)
configure_logging()
logger = logging.getLogger(__name__)


def _validate_fips_cryptographic_configuration() -> None:
    """Enforce the FIPS 140-3 settings this build has to be able to honour.

    A private step of :func:`_validate_production_secrets`, extracted only because that
    function is at its complexity ceiling — **not** a second boot gate. It has exactly one
    caller and must keep exactly one.

    Gated on ``settings.fips_140_3_active`` rather than ``is_hardened``: ``FIPS_MODE``
    defaults to false, so an ordinary dev/CI boot returns immediately, but a deployment
    that has explicitly claimed the FIPS profile must satisfy it whatever its ENVIRONMENT
    says. Both settings below were documented as compliance controls in
    ``docs/FIPS_140_3_COMPLIANCE.md``, ``docs/ENV_VARIABLES_FIPS_140_3.md`` and the
    operations security-hardening guide while being read by nothing outside a test that
    asserted a constant equals its own default. This is what makes that documentation true.

    Raises:
        ValueError: If ``ENCRYPTION_ALGORITHM_V3`` names an algorithm this build does not
            implement, or if ``FIPS_VALIDATE_ENTROPY`` is on and the CSPRNG or the
            configured key material fails validation.
    """
    if not settings.fips_140_3_active:
        return

    # Normalised before comparison: refusing to boot over `aes-256-gcm` vs `AES-256-GCM`
    # would be a false refusal, and a fail-closed control that rejects a correct
    # configuration is a bug rather than extra safety.
    configured_algorithm = settings.ENCRYPTION_ALGORITHM_V3.strip().upper()
    if configured_algorithm not in IMPLEMENTED_ENCRYPTION_ALGORITHMS:
        # Refuse rather than fall back. Silently encrypting with AES-256-GCM while the
        # operator configured something else would make the compliance documentation a
        # lie; switching algorithm to obey the setting would orphan every existing
        # ciphertext, because the v3 envelope records no algorithm field (see
        # IMPLEMENTED_ENCRYPTION_ALGORITHMS in core/config.py).
        implemented = ", ".join(sorted(IMPLEMENTED_ENCRYPTION_ALGORITHMS))
        logger.critical(
            "ENCRYPTION_ALGORITHM_V3=%r is not implemented by this build. "
            "Implemented: %s. Refusing to start.",
            settings.ENCRYPTION_ALGORITHM_V3,
            implemented,
        )
        raise ValueError(
            f"ENCRYPTION_ALGORITHM_V3={settings.ENCRYPTION_ALGORITHM_V3!r} is not "
            f"implemented by this build (implemented: {implemented}). FIPS 140-3 mode "
            "refuses to start rather than encrypt with an algorithm other than the one "
            "configured."
        )

    if not settings.FIPS_VALIDATE_ENTROPY:
        return

    # REDIS_PASSWORD is deliberately not in this list: it is a service credential checked
    # for presence by the caller, not cryptographic key material anything derives from.
    try:
        assert_csprng_available()
        for secret_name in ("ENCRYPTION_KEY", "JWT_SECRET_KEY"):
            validate_secret_entropy(secret_name, getattr(settings, secret_name))
    except ValueError as exc:
        logger.critical("%s Refusing to start.", exc)
        raise


def _validate_production_secrets():
    """Validate that production secrets are properly configured.

    Gated on ``settings.is_hardened`` (fail-closed): every check below applies unless
    ENVIRONMENT explicitly names a relaxed environment. The previous
    ``ENVIRONMENT in ("production", "prod")`` test was never true in practice — nothing
    passes ENVIRONMENT into the containers — so none of these ran anywhere (#284 A0.3).

    The one exception is :func:`_validate_fips_cryptographic_configuration`, which gates on
    ``settings.fips_140_3_active`` instead — see its docstring.
    """
    is_production = settings.is_hardened

    # Check JWT secret key
    insecure_jwt_secrets = (
        "this_should_be_changed_in_production",
        "changeme",
        "secret",
        "your-secret-key",
    )
    # "change_me" catches the .env.example placeholder (CHANGE_ME_auto_generated_on_install)
    # when .env was hand-copied instead of generated by the installer.
    jwt_secret = settings.JWT_SECRET_KEY.lower()
    if is_production and (jwt_secret in insecure_jwt_secrets or "change_me" in jwt_secret):
        logger.error(
            "SECURITY ERROR: JWT_SECRET_KEY is using an insecure default value in production! "
            "Set a strong, unique secret key via the JWT_SECRET_KEY environment variable."
        )
        raise ValueError("Insecure JWT_SECRET_KEY in production environment")

    # Check encryption key
    encryption_key = settings.ENCRYPTION_KEY.lower()
    if is_production and (
        "this_should_be_changed" in encryption_key or "change_me" in encryption_key
    ):
        logger.error(
            "SECURITY ERROR: ENCRYPTION_KEY is using an insecure default value in production! "
            "Set a strong, unique encryption key via the ENCRYPTION_KEY environment variable."
        )
        raise ValueError("Insecure ENCRYPTION_KEY in production environment")

    _validate_fips_cryptographic_configuration()

    # Warn about OIDC audience validation disabled in production
    if is_production and settings.OIDC_ENABLED and not settings.OIDC_VERIFY_AUDIENCE:
        logger.warning(
            "SECURITY WARNING: OIDC_VERIFY_AUDIENCE is disabled in production! "
            "This allows tokens intended for other clients to be accepted. "
            "Set OIDC_VERIFY_AUDIENCE=true and configure OIDC_AUDIENCE for proper token validation."
        )

    # One line, once, naming the retired environment-variable spellings still in use.
    # They keep working permanently (core/legacy_auth_env.py); this only tells the
    # operator that a provider-neutral canonical name now exists.
    deprecated_env = deprecated_oidc_env_names()
    if deprecated_env:
        logger.warning(
            "DEPRECATED: %s still use the pre-rename environment-variable names. "
            "They continue to work and take precedence, but the canonical spelling is "
            "OIDC_* (see docs/OIDC_SETUP.md).",
            ", ".join(deprecated_env),
        )

    # Enforce PKI trusted proxies in production, warn in development
    if settings.PKI_ENABLED and not settings.PKI_TRUSTED_PROXIES:
        if is_production:
            logger.critical(
                "PKI_ENABLED=true but PKI_TRUSTED_PROXIES is empty! "
                "Any client can inject fake certificate headers. Refusing to start."
            )
            raise ValueError("PKI_TRUSTED_PROXIES must be set when PKI_ENABLED=true in production")
        else:
            logger.warning(
                "SECURITY WARNING: PKI_ENABLED=true but PKI_TRUSTED_PROXIES is empty! "
                "This allows any client to inject PKI certificate headers. "
                "Configure PKI_TRUSTED_PROXIES with the address of the proxy that "
                "terminates mTLS — the narrowest range that covers it, e.g. its own "
                "'/32', or the container network it runs on. Do NOT paste a whole "
                "private range: '10.0.0.0/8' and '192.168.0.0/16' are what ordinary "
                "LAN routers hand out, so trusting one lets any device on that LAN "
                "assert a certificate DN and sign in as it (issue #620)."
            )

    # Same rule for trusted-header authentication, and for the same reason: a header
    # nobody vouched for is an attacker-supplied string, and PROXY_ROLE_HEADER can
    # turn one into an admin session. The runtime path fails closed as well
    # (auth/header_trust.py refuses every assertion with an empty allowlist) — this
    # guard is what makes the misconfiguration visible at boot rather than as a
    # silent, total authentication outage.
    #
    # It reads .env only. A deployment that enables proxy auth *solely* in the admin
    # UI is not visible here, because Settings is built before any database session
    # exists; the runtime refusal is what covers that case, and the admin UI's own
    # cross-field validation is where the warning belongs.
    if settings.PROXY_ENABLED and not settings.PROXY_TRUSTED_PROXIES:
        if is_production:
            logger.critical(
                "PROXY_ENABLED=true but PROXY_TRUSTED_PROXIES is empty! "
                "Any client could inject identity headers. Refusing to start."
            )
            raise ValueError(
                "PROXY_TRUSTED_PROXIES must be set when PROXY_ENABLED=true in production"
            )
        logger.warning(
            "SECURITY WARNING: PROXY_ENABLED=true but PROXY_TRUSTED_PROXIES is empty. "
            "Every header-sourced assertion will be REFUSED until you configure the "
            "reverse proxy's addresses (e.g. '10.0.0.0/8,172.16.0.0/12')."
        )

    # Check Redis password in production
    if is_production and not settings.REDIS_PASSWORD:
        logger.critical("REDIS_PASSWORD must be set in production!")
        raise ValueError("REDIS_PASSWORD is required in production environment")

    # Check debug mode in production
    if is_production and settings.DEBUG:
        logger.critical("DEBUG=true in production environment!")
        raise ValueError("DEBUG must be false in production environment")

    # ALLOW_INSECURE_COOKIES strips the auth cookies' Secure flag on plain-HTTP
    # requests to this deployment (see auth/cookies.py:_secure_for_request) — the
    # documented, narrow opt-out for a homelab/small-business LAN install with no
    # TLS-terminating reverse proxy. Logged here, not at cookies.py's import time:
    # this function runs after configure_logging(), so under LOG_FORMAT=json this
    # warning is actually JSON like every other boot line, instead of being the
    # one line a structured log collector could drop or mangle.
    if is_production and settings.ALLOW_INSECURE_COOKIES:
        logger.warning(
            "ALLOW_INSECURE_COOKIES is enabled: auth cookies are set WITHOUT the "
            "Secure flag on plain-HTTP requests to this hardened deployment. This "
            "is intended for a plain-HTTP LAN deployment with no TLS-terminating "
            "reverse proxy. If this deployment is reachable from an untrusted "
            "network, put a TLS-terminating reverse proxy in front of it and "
            "disable this setting instead."
        )

    # Warn about insecure presigned URLs in production
    public_storage_url = settings.STORAGE_PUBLIC_URL or settings.MINIO_PUBLIC_URL
    if is_production and public_storage_url and not public_storage_url.startswith("https://"):
        logger.warning(
            "SECURITY WARNING: STORAGE_PUBLIC_URL/MINIO_PUBLIC_URL uses HTTP instead of "
            "HTTPS. Presigned URLs will be served over an insecure connection in production."
        )

    # TESTING=true enables auth shortcuts (a fabricated user, a password-free login
    # path). Enforcement lives at the USE sites in endpoints/auth.py, which additionally
    # require `not settings.is_hardened` — so the flag is inert in a real deployment even
    # if it leaks into the environment. Warn here rather than refusing to boot: the
    # secrets-guard test suite legitimately simulates production while TESTING is set
    # process-wide by conftest (issue #284 A0.8).
    if is_production and os.environ.get("TESTING", "False").lower() == "true":
        logger.warning(
            "TESTING=true in a hardened environment. Auth shortcuts are disabled by "
            "is_hardened, but this variable should not be set in production."
        )

    # A wildcard origin combined with allow_credentials=True lets any site read
    # authenticated responses. Browsers reject that pairing, but the misconfiguration
    # should surface at boot rather than as confusing CORS failures (issue #284 A0.8).
    if is_production and "*" in settings.CORS_ORIGINS:
        logger.critical(
            "CORS_ORIGINS contains '*' while credentials are allowed. "
            "Set explicit origins. Refusing to start."
        )
        raise ValueError("Wildcard CORS_ORIGINS is not permitted with credentialed requests")

    if is_production:
        logger.info("Production security validation passed")


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


def _setup_minio():
    """Initialize the media bucket on startup.

    Creates the media bucket if it doesn't exist and ensures the bucket
    policy is private (no anonymous/public access). All file access goes
    through presigned URLs, so public read is unnecessary and a security risk.

    Every call in here is a blocking object-storage round trip and the function never
    awaited anything, so as an ``async def`` startup task it held the event loop for
    the whole bucket bootstrap — with the API already accepting requests (issue #320).
    The lifespan now dispatches it through ``run_in_threadpool``.

    Uses the shared storage client so ``STORAGE_BACKEND=s3`` bootstraps against
    the real bucket instead of a MinIO-shaped client built from raw env vars
    (issue #284 A1.11). On native S3 the bucket is owned by the operator's
    infrastructure: we verify it and configure CORS, but never create it and
    never touch its bucket policy — a globally-unique name created in the wrong
    region, or a deleted policy, is not ours to guess at.
    """
    try:
        import json

        from app.services import storage_backend
        from app.services.minio_service import minio_client

        bucket_name = settings.MEDIA_BUCKET_NAME
        native_s3 = storage_backend.is_native_s3()

        if not minio_client.bucket_exists(bucket_name):
            if native_s3:
                logger.error(
                    f"S3 bucket '{bucket_name}' does not exist or is not reachable with the "
                    "configured credentials. Create it (and grant the role access) before "
                    "starting OpenTranscribe."
                )
                return
            minio_client.make_bucket(bucket_name)
            logger.info(f"MinIO bucket '{bucket_name}' created successfully")
        else:
            logger.info(f"Media bucket '{bucket_name}' already exists")

        # Browser-direct uploads need a bucket CORS policy on S3 (MinIO allows any
        # origin already). Opt-in and best-effort; never blocks startup.
        storage_backend.ensure_bucket_cors(bucket_name)

        # Backstop for browser-side multipart uploads the user never finished:
        # the parts of an abandoned upload are billable and invisible in a normal
        # object listing. S3 needs an explicit lifecycle rule; MinIO expires them
        # itself and refuses the rule, so this is a no-op there. Either way the
        # explicit abort on cancel/delete is the primary path.
        from app.services.multipart_upload import ensure_abort_incomplete_lifecycle

        ensure_abort_incomplete_lifecycle(bucket_name)

        if native_s3:
            return

        # Security: ensure bucket has no public access policy.
        # Previous versions set a public read policy ("Principal": {"AWS": "*"})
        # which allowed anonymous access to all stored files. Since all file
        # access goes through presigned URLs, public read is unnecessary.
        # Remove any existing public policy to lock down the bucket.
        try:
            existing_policy = minio_client.get_bucket_policy(bucket_name)
            if existing_policy:
                policy_data = json.loads(existing_policy)
                has_public_access = any(
                    stmt.get("Principal") in ({"AWS": "*"}, "*", {"AWS": ["*"]})
                    for stmt in policy_data.get("Statement", [])
                )
                if has_public_access:
                    minio_client.delete_bucket_policy(bucket_name)
                    logger.warning(
                        f"SECURITY FIX: Removed public access policy from bucket '{bucket_name}'. "
                        "All file access now requires authentication via presigned URLs."
                    )
        except Exception:  # noqa: S110
            # No policy set (expected for new buckets) — this is the secure default
            logger.debug("No bucket policy set for '%s' (secure default)", bucket_name)

    except Exception as e:
        logger.error(f"Error setting up media bucket: {e}")


def _clear_stale_task_state():
    """Clear ALL stale task state from Redis on startup.

    Any task state from the previous process lifetime is stale — the
    Celery workers that were processing it are gone. Clear everything so
    the UI starts clean and tasks can be re-triggered.

    This covers:
    - Migration orchestrator state and locks
    - Reindex coordination state and locks
    - Auto-labeling locks
    - Data integrity / embedding consistency locks
    - Search maintenance locks
    - All progress tracker keys (task_progress:*)
    """
    from app.core.redis import get_redis

    r = get_redis()

    # All migration progress services and their associated keys
    prefixes = [
        "speaker_attr_migration",
        "combined_speaker_migration",
        "embedding_migration",
    ]

    cleared = []
    for prefix in prefixes:
        suffixes = [
            ":status",
            ":batch_task_ids",
            ":orchestrator_lock",
            ":completed",
            ":lock",
        ]
        deleted_any = False
        for suffix in suffixes:
            key = f"{prefix}{suffix}"
            if r.delete(key):
                deleted_any = True
        if deleted_any:
            cleared.append(prefix)

    # Clear ALL progress tracker keys — any running task is dead after restart
    for key in r.scan_iter(match="task_progress:*"):
        r.delete(key)
        cleared.append(str(key))

    # Clear stale task locks and coordination state.
    # Every Redis-based lock used by any task must be listed here
    # so that a restart never gets blocked by an orphaned lock.
    #
    # ⚠️ NOT the reindex coordination keys — `reindex_lock:*`, `reindex_state:*`
    # and `reindex_uuids:*` were in this list and had to come out. This runs in
    # the **API** process, which restarts on its own (deploy, crash, `--reload`
    # in dev) while the Celery workers that own those keys keep running. Wiping
    # them mid-reindex is not a no-op: the next `search_index_maintenance` sees
    # no lock, dispatches a second coordinator, that coordinator recreates the
    # shared state with its own tiny `worker_count`, the in-flight batch workers
    # increment into it, completion fires early with an almost-empty
    # "files I indexed" set — and the post-reindex orphan sweep deletes
    # everything not in it. Measured on an isolated stack: 432 files / 208k
    # chunks reduced to 252 files / 111k chunks by one backend reload during a
    # reindex. `reindex_lock` carries `ex=3600` and the state keys carry a TTL,
    # so a genuinely dead coordinator unblocks itself; a live one no longer gets
    # its coordination state deleted out from under it.
    # `reindex_cancel:*` stays — it is a user request, meaningless after a
    # restart, and clearing it can only cause a reindex to run, never to delete.
    stale_patterns = [
        # Reindex coordination
        "reindex_cancel:*",
        # Auto-labeling
        "auto_label_lock:*",
        "auto_label_progress:*",
        # Search and data integrity (TaskLockManager-based, like system.health_check)
        "search_index_maintenance",
        "data_integrity_running",
        # Embedding tasks
        "normalize_embeddings_lock",
        "embedding_consistency_running",
        "embedding_consistency_progress:*",
        # Speaker clustering (TaskLockManager-based)
        "recluster_speakers_user_*",
        # Health check (TaskLockManager-based)
        "system.health_check",
    ]
    for pattern in stale_patterns:
        for key in r.scan_iter(match=pattern):
            r.delete(key)
            cleared.append(str(key))

    if cleared:
        logger.info("Cleared %d stale task keys on startup", len(cleared))


async def _run_startup_recovery():
    """Schedule startup recovery task after a delay."""
    try:
        await asyncio.sleep(10)
        from app.tasks.recovery import startup_recovery_task

        result = startup_recovery_task.delay()
        logger.info(f"Startup recovery task scheduled: {result.id}")
    except Exception as e:
        logger.error(f"Error scheduling startup recovery: {e}")


async def _run_search_maintenance():
    """Schedule search index maintenance after a delay, once per boot window.

    Elected like ``opensearch_repair_indices`` below, and for the same reason:
    this dispatch fans out one reindex coordinator per user against the shared
    ``transcript_chunks`` index, and two overlapping passes have already
    corrupted three reindexes in one day (root CLAUDE.md). Back-to-back stack
    recreations — and, in dev, an ordinary hot reload on every ``app/**.py``
    save — are exactly the window ``run_once_per_boot`` closes.
    """
    try:
        await asyncio.sleep(30)
        from app.tasks.search_maintenance_task import search_index_maintenance_task
        from app.utils.boot_once import run_once_per_boot

        if not run_once_per_boot("search_index_maintenance_dispatch"):
            logger.info("Search index maintenance already dispatched this boot window; skipping")
            return

        result = search_index_maintenance_task.delay()
        logger.info(f"Search index maintenance task scheduled: {result.id}")
    except Exception as e:
        logger.error(f"Error scheduling search maintenance: {e}")


async def _run_thumbnail_migration():
    """Schedule thumbnail migration from JPEG to WebP after a delay."""
    try:
        await asyncio.sleep(45)  # Wait for other startup tasks
        from app.tasks.thumbnail_migration import migrate_thumbnails_to_webp

        result = migrate_thumbnails_to_webp.delay(batch_size=20)
        logger.info(f"Thumbnail migration task scheduled: {result.id}")
    except Exception as e:
        logger.error(f"Error scheduling thumbnail migration: {e}")


async def _run_imohash_recompute():
    """One-time recompute of every media_file.imohash after the package switch.

    The server-side fingerprint moved from a hand-rolled blake2b stand-in to the
    real ``imohash`` package, changing every existing value. Checks a DB flag
    first — if already done, returns immediately. Otherwise dispatches the
    batched recompute task, which sets the flag when the whole library is
    processed. Same pattern as the thumbnail / embedding-normalization
    migrations.
    """
    try:
        await asyncio.sleep(75)

        from app.db.base import SessionLocal
        from app.models.system_settings import SystemSettings
        from app.tasks.imohash_recompute import RECOMPUTE_FLAG_KEY

        db = SessionLocal()
        try:
            flag = db.query(SystemSettings).filter(SystemSettings.key == RECOMPUTE_FLAG_KEY).first()
            if flag and flag.value == "true":
                logger.info("imohash package recompute already completed — skipping")
                return
        finally:
            db.close()

        from app.tasks.imohash_recompute import recompute_all

        result = recompute_all.delay()
        logger.info(f"imohash package recompute task scheduled: {result.id}")
    except Exception as e:
        logger.error(f"Error scheduling imohash recompute: {e}")


async def _run_one_time_embedding_normalization():
    """One-time migration: normalize legacy embeddings for users upgrading.

    Checks a DB flag first — if already done, returns immediately.
    After successful normalization, sets the flag so it never runs again.
    """
    try:
        await asyncio.sleep(60)

        from app.db.base import SessionLocal
        from app.models.system_settings import SystemSettings

        db = SessionLocal()
        try:
            flag = (
                db.query(SystemSettings)
                .filter(SystemSettings.key == "embedding_normalization_done")
                .first()
            )
            if flag and flag.value == "true":
                logger.info("Embedding normalization already completed — skipping")
                return
        finally:
            db.close()

        from app.tasks.speaker_embedding_migration import normalize_speaker_embeddings_task

        result = normalize_speaker_embeddings_task.apply(throw=False)
        if result and result.result and result.result.get("normalized", 0) == 0:
            # All vectors already normalized — set flag
            db = SessionLocal()
            try:
                setting = (
                    db.query(SystemSettings)
                    .filter(SystemSettings.key == "embedding_normalization_done")
                    .first()
                )
                if not setting:
                    setting = SystemSettings(
                        key="embedding_normalization_done",
                        value="true",
                        description="One-time embedding L2 normalization migration completed",
                    )
                    db.add(setting)
                else:
                    setting.value = "true"
                db.commit()
                logger.info("Embedding normalization flag set — will not run again")
            finally:
                db.close()
        elif result and result.result:
            stats = result.result
            logger.info(
                "Embedding normalization migrated %d vectors (checked %d)",
                stats.get("normalized", 0),
                stats.get("total_found", 0),
            )
            # Set flag after successful migration
            db = SessionLocal()
            try:
                setting = SystemSettings(
                    key="embedding_normalization_done",
                    value="true",
                    description="One-time embedding L2 normalization migration completed",
                )
                db.merge(setting)
                db.commit()
            finally:
                db.close()
    except Exception as e:
        logger.error(f"Embedding normalization migration error: {e}")


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager for startup and shutdown events."""
    if os.environ.get("TESTING", "").lower() == "true":
        logger.info("Test mode: skipping startup tasks")
        yield
        return

    logger.info("Starting application...")
    _validate_production_secrets()

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
)

# Configure maximum upload size (50GB)
app.router.default_max_upload_size = 50 * 1024 * 1024 * 1024  # type: ignore[attr-defined]  # 50GB

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
        AuthenticationError: 401,
        StorageError: 503,
        SearchIndexError: 503,
        LLMServiceError: 502,
        EmailDeliveryError: 503,
    }
    status = status_map.get(type(exc), 500)
    return JSONResponse(
        status_code=status,
        content={"detail": exc.message},
    )


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
    """
    from sqlalchemy import text

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

    # Redis (critical) — ping the shared db-0 singleton.
    try:
        from app.core.redis import get_redis

        get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {type(exc).__name__}"

    # OpenSearch (degraded-but-ready) — reuse the existing client singleton.
    try:
        from app.services.opensearch_service import get_opensearch_client

        client = get_opensearch_client()
        if client is not None and client.ping():
            checks["opensearch"] = "ok"
        else:
            checks["opensearch"] = "unavailable"
    except Exception as exc:  # noqa: BLE001
        checks["opensearch"] = f"error: {type(exc).__name__}"

    # MinIO (degraded-but-ready) — reuse the existing client singleton.
    try:
        from app.services.minio_service import minio_client

        minio_client.bucket_exists(settings.MEDIA_BUCKET_NAME)
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
        from alembic.script import ScriptDirectory

        from app.db.base import engine
        from app.db.migrations import get_alembic_config

        head = ScriptDirectory.from_config(get_alembic_config()).get_current_head()
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
