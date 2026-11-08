"""One-shot startup jobs the API lifespan launches in the background.

Object-storage bootstrap, stale-Redis-state sweep and the delayed Celery dispatches
(recovery, search maintenance, thumbnail / imohash / embedding migrations). Split out of
``app/main.py``; the lifespan imports each name and schedules it.
"""

import asyncio
import logging

from minio.error import MinioException
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.core.config import settings

logger = logging.getLogger(__name__)


def _backfill_quarantine_tags() -> None:
    """One-time-per-boot, best-effort: tag files quarantined before issue #907 shipped.

    ``quarantine_file``/``release_file`` keep the presigned-URL-revocation tag in sync with
    ``is_quarantined`` going forward; this covers the gap for rows that were already
    quarantined when a deployment upgrades onto this feature. Normally zero rows — a
    takedown is rare and this only matters for one still open at upgrade time. Never
    raises; the caller treats a failure here as non-fatal to startup.
    """
    from app.db.base import SessionLocal
    from app.models.media import MediaFile
    from app.services.media_revocation import MediaSnapshot
    from app.services.media_revocation import resolve_enforcement
    from app.services.media_revocation import revoke_media

    # Read, snapshot and close BEFORE any storage call: no connection idles in a transaction
    # across the tag writes.
    db = SessionLocal()
    try:
        rows = [
            MediaSnapshot.of(row)
            for row in db.query(MediaFile).filter(MediaFile.is_quarantined.is_(True)).all()
        ]
    finally:
        db.close()
    if not rows:
        return
    # Resolved ONCE per boot, and one summary line instead of an ERROR per row.
    enforcement = resolve_enforcement()
    reports = [revoke_media(row, enforcement=enforcement, log=False) for row in rows]
    incomplete = [r for r in reports if not r.complete]
    if not incomplete:
        logger.info(
            f"Quarantine-tag backfill: presigned media URLs revoked for all {len(rows)} "
            f"already-quarantined file(s)"
        )
        return
    reasons = sorted({str(r.reason) for r in incomplete})
    missing = sorted({p for r in incomplete for p in r.missing_permissions})
    logger.error(
        f"Quarantine-tag backfill: presigned media URLs NOT revoked for "
        f"{len(incomplete)}/{len(rows)} already-quarantined file(s) "
        f"(reasons: {', '.join(reasons)}; enforcement={enforcement[0]}"
        + (f"; the storage identity is missing {', '.join(missing)}" if missing else "")
        + "). See docs/abuse-and-takedown.md."
    )


def _setup_minio() -> None:
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

    Elected per boot window (``media_bucket_setup``) for everything that acts on the shared
    bucket, so N replicas do not race ``make_bucket`` or walk the quarantine backfill N times.
    """
    try:
        from app.services import storage_backend
        from app.services.minio_service import minio_client
        from app.utils.boot_once import run_once_per_boot

        bucket_name = settings.MEDIA_BUCKET_NAME
        native_s3 = storage_backend.is_native_s3()

        # Bucket creation, CORS, lifecycle, the public-policy sweep and the quarantine
        # backfill act on the SHARED bucket: one replica per boot window does them. The
        # presign identity below stays per process — its result is memoized in-process and
        # every replica signs URLs with it.
        owns_bucket_setup = run_once_per_boot("media_bucket_setup")
        if owns_bucket_setup:
            _setup_media_bucket(minio_client, bucket_name, native_s3)

        _ensure_presign_identity(minio_client, native_s3)

        if not owns_bucket_setup:
            return

        # Bounded best-effort backfill (issue #907): tag any file quarantined BEFORE this
        # feature shipped, so upgrading enforces revocation on pre-existing takedowns too.
        # Normally zero rows. Never blocks startup — a backfill failure just means those
        # rows stay uncovered until the next restart.
        try:
            _backfill_quarantine_tags()
        except Exception as e:  # best-effort, must never block startup
            logger.warning(f"Quarantine-tag backfill failed (non-fatal): {e}")

        if native_s3:
            return

        _remove_public_bucket_policy(minio_client, bucket_name)

    except Exception as e:
        logger.error(f"Error setting up media bucket: {e}")


def _setup_media_bucket(minio_client, bucket_name: str, native_s3: bool) -> None:
    """Create (MinIO only) and configure the shared media bucket. Raises on storage errors."""
    from app.services import storage_backend

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


def _ensure_presign_identity(minio_client, native_s3: bool) -> None:
    """Provision this process's restricted presign identity (issue #907). Never raises.

    Done now, at process startup, rather than lazily on the first presigned URL request —
    so a provisioning failure is visible in the startup log instead of buried in the first
    user's request. No-op (and no admin call) on native S3 or when the feature is disabled.
    """
    from app.services import storage_presign_identity

    if storage_presign_identity.ensure_presign_identity():
        logger.info("Presign identity ready — quarantine will revoke presigned media URLs")
    elif native_s3:
        storage_presign_identity.report_native_s3_revocation_posture(minio_client)
        # The bucket-policy Deny only enforces a tag that landed: check, at boot rather
        # than at the first takedown, that this identity can write one.
        from app.services.media_revocation_probe import report_native_s3_tagging_posture

        report_native_s3_tagging_posture()
    elif settings.STORAGE_PRESIGN_IDENTITY_ENABLED:
        logger.error(
            "Presign identity NOT provisioned — presigned media URLs will be signed "
            "with the root credential and quarantine will NOT revoke them. See the "
            "preceding log line for why."
        )


def _remove_public_bucket_policy(minio_client, bucket_name: str) -> None:
    """Delete a legacy public-read bucket policy (MinIO only).

    Previous versions set a public read policy ("Principal": {"AWS": "*"}) which allowed
    anonymous access to all stored files. Since all file access goes through presigned
    URLs, public read is unnecessary.
    """
    import json

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
    except Exception:
        # No policy set (expected for new buckets) — this is the secure default
        logger.debug("No bucket policy set for '%s' (secure default)", bucket_name)


def _apply_derived_cache_policy() -> None:
    """Apply the derived-cache retention and run the one-time legacy reclaims. Never raises.

    Retention runs in every process on purpose: one idempotent lifecycle PUT that re-reads
    the admin-configured value (DB over env), so any restart picks up a UI change with no
    redeploy. The legacy reclaims list and delete bucket objects and their DB flags are
    check-then-set, so replicas booting together would each run them in full: they are
    elected (``legacy_cache_reclaim``).
    """
    try:
        from app.db.base import SessionLocal
        from app.services import cache_management_service
        from app.services import system_settings_service
        from app.utils.boot_once import run_once_per_boot

        db = SessionLocal()
        try:
            days = cache_management_service.apply_retention(db)
            logger.info(f"Derived-cache retention applied: {days} day(s)")

            if not run_once_per_boot("legacy_cache_reclaim"):
                return
            reclaimed_key = "cache.legacy_derived_reclaimed"
            if not system_settings_service.get_setting_bool(db, reclaimed_key, False):
                count = cache_management_service.reclaim_legacy_derived_cache()
                system_settings_service.set_setting(
                    db,
                    reclaimed_key,
                    True,
                    "One-time reclaim of pre-prefix derived cache objects has run",
                )
                if count:
                    logger.info(f"Upgrade reclaim: removed {count} legacy derived cache object(s)")

            cache_management_service.reclaim_legacy_audio_cache_once(db)
        finally:
            db.close()
    except (SQLAlchemyError, MinioException, OSError, ValueError) as e:
        logger.warning(f"Derived-cache retention/reclaim setup failed (non-fatal): {e}")


def _clear_stale_task_state() -> None:
    """Clear Redis state that cannot outlive the process that asked for it.

    This used to delete every task lock, run flag and ``task_progress:*`` key on the
    assumption that "the Celery workers that were processing it are gone". They are not:
    this runs in the **API** process, which restarts on its own (deploy, crash,
    ``--reload`` in dev) while workers keep running, so it deleted live workers' locks and
    progress (concurrency audit M4; the reindex coordination keys were the first casualty,
    see git history). Every one of those keys now recovers on its own instead:

    * task locks are leases the holder renews (``app/utils/task_lock.py``,
      ``TASK_LOCK_LEASE_SECONDS``): a dead holder's lock expires within one lease;
    * multi-task run flags (``auto_label_lock:*``, ``embedding_consistency_running``) and
      progress/status keys carry a TTL their running tasks renew on every write, and have
      a Stop control that clears them.

    What remains is ``reindex_cancel:*``: a user's cancel request is meaningless after a
    restart, and clearing it can only let a reindex run, never delete anything.
    """
    from app.core.redis import get_redis

    r = get_redis()
    cleared = 0
    for key in r.scan_iter(match="reindex_cancel:*"):
        cleared += int(r.delete(key))
    if cleared:
        logger.info("Cleared %d stale reindex cancel request(s) on startup", cleared)


async def _run_startup_recovery() -> None:
    """Schedule the startup recovery task after a delay, once per boot window.

    The task's own lock stops two recoveries overlapping, not a second one starting after
    the first finished; electing the dispatch is what keeps N replicas from re-queueing
    stuck files N times in one rollout.
    """
    try:
        await asyncio.sleep(10)
        from app.tasks.recovery import startup_recovery_task
        from app.utils.boot_once import run_once_per_boot

        if not run_once_per_boot("startup_recovery_dispatch"):
            return

        result = startup_recovery_task.delay()
        logger.info(f"Startup recovery task scheduled: {result.id}")
    except Exception as e:
        logger.error(f"Error scheduling startup recovery: {e}")


async def _run_search_maintenance() -> None:
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


async def _run_availability_reconcile() -> None:
    """Converge the index's ``availability`` stamp once per boot window (issue #1253).

    A file taken down before the index carried quarantine has documents with no value, which
    every reader treats as available; the unscoped reconcile stamps every trashed and
    quarantined file. The beat runs the same task every 15 minutes, so this only shortens the
    window after an upgrade or a restore of the index. Result lists keep the Postgres
    post-filter meanwhile. Neither call raises: the election runs the step when Redis is down,
    and a failed dispatch is logged and left to the beat.
    """
    await asyncio.sleep(30)
    from app.services.search.availability_stamp import dispatch_reconcile
    from app.utils.boot_once import run_once_per_boot

    if run_once_per_boot("availability_reconcile_dispatch"):
        dispatch_reconcile(None)


def _repair_tag_tenancy() -> None:
    """Run ``tag_tenant_repair`` in its own session; the caller logs a failure."""
    from app.db.base import SessionLocal
    from app.services.tag_tenant_repair import repair_cross_tenant_file_tags

    db = SessionLocal()
    try:
        repair_cross_tenant_file_tags(db)
    finally:
        db.close()


async def _run_tag_tenant_repair() -> None:
    """Repoint tags attached to a file of another tenant, once per boot window.

    A startup pass rather than an Alembic data revision; ``services/tag_tenant_repair``
    says why. Elected so concurrent replicas do not walk the same rows, and run in the
    threadpool because it is synchronous database work.
    """
    try:
        await asyncio.sleep(20)
        from app.utils.boot_once import run_once_per_boot

        if not run_once_per_boot("tag_tenant_repair"):
            return
        await run_in_threadpool(_repair_tag_tenancy)
    except SQLAlchemyError as e:
        logger.error(f"Tag tenant repair failed (non-fatal, retried next boot): {e}")


async def _run_thumbnail_migration() -> None:
    """Dispatch the one-time thumbnail passes (WebP migration, missing backfill) until done.

    Each pass sets a ``SystemSettings`` done flag when it reaches the end of the library
    (``tasks/thumbnail_migration``), so a boot after that dispatches nothing. Before the flag,
    every boot re-walked every thumbnail-less video and re-queued the files whose generation
    always fails (M2 #11: 24,920 tasks per restart). Same pattern as the imohash recompute.
    """
    try:
        await asyncio.sleep(45)  # Wait for other startup tasks
        await run_in_threadpool(_dispatch_thumbnail_passes)
    except Exception as e:
        logger.error(f"Error scheduling thumbnail migration: {e}")


def _dispatch_thumbnail_passes() -> None:
    """Blocking half of the startup step (DB read, Redis election, broker publish)."""
    from app.core import constants
    from app.core.queues import CPUPriority
    from app.tasks.thumbnail_migration import backfill_missing_thumbnails
    from app.tasks.thumbnail_migration import migrate_thumbnails_to_webp
    from app.tasks.thumbnail_migration import one_time_pass_done
    from app.utils.boot_once import run_once_per_boot

    pending = [
        (task, kwargs)
        for key, task, kwargs in (
            (
                constants.THUMBNAIL_WEBP_MIGRATION_DONE_KEY,
                migrate_thumbnails_to_webp,
                {"batch_size": 20},
            ),
            (constants.THUMBNAIL_BACKFILL_DONE_KEY, backfill_missing_thumbnails, {}),
        )
        if not one_time_pass_done(key)
    ]
    if not pending:
        logger.info("Thumbnail passes already completed; nothing to dispatch")
        return
    # One replica per boot window: each dispatch walks the whole media library.
    if not run_once_per_boot("thumbnail_migration_dispatch"):
        return
    for task, kwargs in pending:
        result = task.apply_async(kwargs=kwargs, priority=CPUPriority.MAINTENANCE)
        logger.info(f"Thumbnail task {task.name} scheduled: {result.id}")


async def _run_imohash_recompute() -> None:
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
        from app.utils.boot_once import run_once_per_boot

        # The flag is set only when the whole library is done, so every replica booting
        # before then would otherwise dispatch its own full recompute.
        if not run_once_per_boot("imohash_recompute_dispatch"):
            return
        result = recompute_all.delay()
        logger.info(f"imohash package recompute task scheduled: {result.id}")
    except Exception as e:
        logger.error(f"Error scheduling imohash recompute: {e}")


async def _run_one_time_embedding_normalization() -> None:
    """Dispatch the one-time embedding normalization to a worker until it has completed.

    It used to run ``normalize_speaker_embeddings_task.apply()`` here: a full scroll over
    the speaker indices inline in the API lifespan, on the event loop every request shares,
    on every replica (concurrency audit M12). It also set the "done" flag from the result,
    so an ``already_running`` skip or an OpenSearch outage (both ``normalized: 0``) marked
    the migration done without it having run. Now the API only checks the flag and
    publishes, elected so one replica does it, and the task sets the flag after a clean run.
    """
    try:
        await asyncio.sleep(60)
        await run_in_threadpool(_dispatch_embedding_normalization)
    except Exception as e:
        logger.error(f"Embedding normalization dispatch error: {e}")


def _dispatch_embedding_normalization() -> None:
    """Blocking half of the startup step (DB read, Redis election, broker publish)."""
    from app.db.base import SessionLocal
    from app.models.system_settings import SystemSettings
    from app.tasks.speaker_embedding_migration import NORMALIZATION_DONE_KEY
    from app.tasks.speaker_embedding_migration import normalize_speaker_embeddings_task
    from app.utils.boot_once import run_once_per_boot

    db = SessionLocal()
    try:
        flag = db.query(SystemSettings).filter(SystemSettings.key == NORMALIZATION_DONE_KEY).first()
        done = flag is not None and flag.value == "true"
    finally:
        db.close()
    if done:
        logger.info("Embedding normalization already completed — skipping")
        return
    if not run_once_per_boot("embedding_normalization"):
        return
    result = normalize_speaker_embeddings_task.delay()
    logger.info("Embedding normalization dispatched to a worker: %s", getattr(result, "id", None))
