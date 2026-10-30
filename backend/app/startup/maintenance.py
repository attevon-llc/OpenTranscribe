"""One-shot startup jobs the API lifespan launches in the background.

Object-storage bootstrap, stale-Redis-state sweep and the delayed Celery dispatches
(recovery, search maintenance, thumbnail / imohash / embedding migrations). Split out of
``app/main.py``; the lifespan imports each name and schedules it.
"""

import asyncio
import logging

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
    from app.services.minio_service import set_object_quarantine_tag

    db = SessionLocal()
    try:
        rows = db.query(MediaFile).filter(MediaFile.is_quarantined.is_(True)).all()
        if not rows:
            return
        tagged = 0
        for row in rows:
            if row.storage_path and set_object_quarantine_tag(str(row.storage_path), True):
                tagged += 1
            if row.thumbnail_path:
                set_object_quarantine_tag(str(row.thumbnail_path), True)
            if row.playback_path:
                set_object_quarantine_tag(str(row.playback_path), True)
        logger.info(
            f"Quarantine-tag backfill: tagged {tagged}/{len(rows)} already-quarantined file(s)"
        )
    finally:
        db.close()


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

        # Presigned-URL revocation on quarantine (issue #907): provision the restricted
        # presign identity now, at process startup, rather than lazily on the first
        # presigned URL request — so a provisioning failure is visible in the startup
        # log instead of buried in the first user's request. No-op (and no admin call)
        # on native S3 or when the feature is disabled; never raises.
        from app.services import storage_presign_identity

        if storage_presign_identity.ensure_presign_identity():
            logger.info("Presign identity ready — quarantine will revoke presigned media URLs")
        elif native_s3:
            storage_presign_identity.report_native_s3_revocation_posture(minio_client)
        elif settings.STORAGE_PRESIGN_IDENTITY_ENABLED:
            logger.error(
                "Presign identity NOT provisioned — presigned media URLs will be signed "
                "with the root credential and quarantine will NOT revoke them. See the "
                "preceding log line for why."
            )

        # Bounded best-effort backfill (issue #907): tag any file quarantined BEFORE this
        # feature shipped, so upgrading enforces revocation on pre-existing takedowns too.
        # Normally zero rows. Never blocks startup — a backfill failure just means those
        # rows stay uncovered until the next restart.
        try:
            _backfill_quarantine_tags()
        except Exception as e:  # noqa: BLE001 — best-effort, must never block startup
            logger.warning(f"Quarantine-tag backfill failed (non-fatal): {e}")

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
        except Exception:
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
