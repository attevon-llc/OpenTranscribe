# Skip heavy AI imports during testing - speeds up test startup significantly
import logging
import os
import ssl

logger = logging.getLogger(__name__)


def _int_env(key: str, default: int) -> int:
    """Read an int env var, falling back to *default* on absence or garbage."""
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        logger.warning("Invalid integer for %s; using %d", key, default)
        return default


_SKIP_CELERY = os.environ.get("SKIP_CELERY", "").lower() == "true"

if not _SKIP_CELERY:
    # PyTorch 2.6+ compatibility fix - MUST be done BEFORE any ML library imports
    # Patch torch.load to default to weights_only=False for trusted HuggingFace models
    # This must be at the TOP of celery.py because Celery's include= imports task modules
    # which import pyannote/whisperx that cache torch.load at import time
    import torch

    _original_torch_load = torch.load

    def _patched_torch_load(*args, **kwargs):
        # Handle both missing weights_only AND weights_only=None (which PyTorch 2.8 treats as True)
        if kwargs.get("weights_only") is None:
            kwargs["weights_only"] = False
        return _original_torch_load(*args, **kwargs)

    torch.load = _patched_torch_load

    # Note: WhisperX 3.8.1 has native PyAnnote v4 support — no patches needed

# Imports must come after torch.load patch to prevent caching issues
from celery import Celery  # noqa: E402
from celery.schedules import crontab  # noqa: E402
from celery.signals import beat_init  # noqa: E402
from celery.signals import before_task_publish  # noqa: E402
from celery.signals import setup_logging  # noqa: E402
from celery.signals import task_postrun  # noqa: E402
from celery.signals import task_prerun  # noqa: E402
from celery.signals import worker_process_init  # noqa: E402
from celery.signals import worker_ready  # noqa: E402
from kombu import Queue  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.constants import CeleryQueues  # noqa: E402

# Explicit queue declarations — single source of truth.
# With task_create_missing_queues=False, any typo in a queue name will raise
# an error at dispatch time instead of silently creating a phantom queue.
CELERY_QUEUES = tuple(Queue(q) for q in CeleryQueues.ALL)

# Initialize Celery
# kombu refuses to connect over `rediss://` unless an SSL context is supplied — it does
# NOT fall back to a default. config.py builds a rediss:// URL whenever REDIS_USE_TLS is
# set, so without this every worker dies at startup on a TLS Redis (issue #284 A1.17).
# CERT_REQUIRED (the default) verifies the server certificate; set
# CELERY_REDIS_SSL_CERT_REQS=optional|none only for a self-signed broker you control.
_REDIS_SSL_MODES = {
    "required": ssl.CERT_REQUIRED,
    "optional": ssl.CERT_OPTIONAL,
    "none": ssl.CERT_NONE,
}
_redis_uses_tls = str(settings.CELERY_BROKER_URL).startswith("rediss://")
_redis_ssl_options = (
    {
        "ssl_cert_reqs": _REDIS_SSL_MODES.get(
            os.getenv("CELERY_REDIS_SSL_CERT_REQS", "required").lower(), ssl.CERT_REQUIRED
        )
    }
    if _redis_uses_tls
    else None
)

celery_app = Celery(
    "transcribe_app",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.transcription",
        "app.tasks.transcription.core",
        "app.tasks.transcription.preprocess",
        "app.tasks.transcription.postprocess",
        "app.tasks.transcription.dispatch",
        "app.tasks.waveform",
        "app.tasks.waveform_generation",
        "app.tasks.summarization",
        "app.tasks.analytics",
        "app.tasks.cleanup",
        "app.tasks.utility",
        "app.tasks.recovery",
        "app.tasks.youtube_processing",
        "app.tasks.media_download",
        # Re-export shim (NOT dead) — keeps legacy task-name routing working by
        # re-exporting from speaker_{identification,update,embedding}_task. See
        # app/tasks/speaker_tasks.py. Must stay in this include list.
        "app.tasks.speaker_tasks",
        "app.tasks.speaker_identification_task",
        "app.tasks.speaker_update_task",
        "app.tasks.speaker_merge_task",
        "app.tasks.speaker_embedding_task",
        "app.tasks.speaker_attribute_task",
        "app.tasks.topic_extraction",
        "app.tasks.ingest_artifacts_task",
        "app.tasks.reindex_task",
        "app.tasks.search_maintenance_task",
        "app.tasks.opensearch_integrity_task",
        "app.tasks.search_indexing_task",
        "app.tasks.rename_propagation_task",
        "app.tasks.redaction_task",
        "app.tasks.chat_retention",
        "app.tasks.erasure_reconciliation",
        "app.tasks.thumbnail",
        "app.tasks.thumbnail_migration",
        "app.tasks.embedding_migration_v4",
        "app.tasks.imohash_recompute",
        "app.tasks.watch_source_tasks",
        "app.tasks.speaker_embedding_migration",
        "app.tasks.baseline_export",
        "app.tasks.rediarize_task",
        "app.tasks.speaker_clustering",
        "app.tasks.auto_labeling",
        "app.tasks.speaker_attribute_migration_task",
        "app.tasks.combined_speaker_analysis_task",
        "app.tasks.speaker_embedding_consistency",
        "app.tasks.embedding_consistency_repair",
        "app.tasks.recovery_tasks",
        "app.tasks.backup_tasks",
        "app.tasks.directory_sync_task",
    ],
)

# Configure Celery
celery_app.conf.update(
    broker_use_ssl=_redis_ssl_options,
    redis_backend_use_ssl=_redis_ssl_options,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # GPU-safe default: one task at a time. Short-task queues (cpu/nlp/download/
    # utility) override this per-worker with --prefetch-multiplier, since a global 1
    # makes every worker round-trip the broker between tasks (issue #284 A1.8).
    # Keep the GPU worker at 1 — it must never hold a second task it cannot start.
    worker_prefetch_multiplier=1,
    # Global task time limits (issue #284 A1.2). There were NONE, so a hung CUDA call
    # held the single GPU slot forever and no later transcription could start.
    #
    # Deliberately GENEROUS rather than tight. Media is capped at 4 h and hybrid/CPU
    # transcription of a long file is legitimately slow, so a tight global limit would
    # truncate real work — and getting per-task overrides wrong on even one long task
    # silently kills valid jobs. A 3 h ceiling still converts "wedged forever" into
    # "recovered in 3 h", which is the actual failure this addresses. Tasks needing more
    # override per-task (see combined_speaker_analysis_task for the pattern).
    #
    # Kept under visibility_timeout (6 h) so a timeout kill happens before redelivery,
    # not after — otherwise the two mechanisms would fight and duplicate work.
    #
    # CAVEAT: soft_time_limit is delivered via SIGALRM, which is unreliable under
    # --pool=threads — which is exactly what the GPU workers use. Treat these as a
    # backstop for the prefork/CPU queues; on GPU the real protection is
    # visibility_timeout plus the DB-status crash recovery in tasks/recovery.py.
    task_soft_time_limit=_int_env("CELERY_SOFT_TIME_LIMIT", 10800),  # 3 h
    task_time_limit=_int_env("CELERY_HARD_TIME_LIMIT", 11700),  # 3 h 15 m
    worker_send_task_events=True,  # Enable real-time task events for Flower
    task_send_sent_event=True,  # Fire event when task is dispatched to queue
    result_expires=86400,  # Expire results after 24h (prevent Redis bloat)
    # Enable Redis priority queues: lower number = higher priority (runs first).
    # Priorities are PER-QUEUE — GPUPriority.X is independent of CPUPriority.X.
    # Named constants defined in app.core.constants: GPUPriority, CPUPriority, etc.
    # GPU queue:  0=speaker-reassign  1=embed-extract  3=transcription  4=rediarize
    #             5=recluster  7=admin-migration-batches
    # CPU queue:  2=pipeline-critical  4=user-triggered  5=system  6=admin  8=maintenance
    # NLP queue:  3=user-triggered  5=auto-pipeline  7=admin-batch  9=background
    # Download:   3=single-url  6=playlist
    # Embedding:  2=pipeline-critical
    # Utility:    1=emergency  3=operational  5=routine  7=background  9=dev-tools
    task_queues=CELERY_QUEUES,
    task_create_missing_queues=False,  # Catch queue name typos at dispatch time
    broker_transport_options={
        "priority_steps": list(range(10)),
        "queue_order_strategy": "priority",
        # visibility_timeout MUST exceed the longest task (issue #284 A1.1).
        #
        # With Redis as the broker, an un-acked message is redelivered to another
        # worker once this elapses. The transcription tasks are acks_late=True, so a
        # run that outlives it is handed to a SECOND worker while the first is still
        # going — the same file transcribed twice, on the GPU, concurrently.
        #
        # Nothing set this before, so the kombu default of 3600s applied: ANY
        # transcription over one hour was silently duplicated. A 4-hour media limit
        # makes that reachable with an ordinary file, not just a pathological one.
        #
        # 21600 (6h) covers the longest supported job with headroom. Do NOT set it
        # absurdly high "to be safe": crash recovery keys off DB status
        # (tasks/recovery.py), not off redelivery, so an inflated value only delays
        # requeueing after a genuine worker loss.
        "visibility_timeout": int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "21600")),
    },
    task_routes={
        # GPU Queue - GPU-intensive AI tasks (concurrency=1, requires GPU)
        # See priority comment above for priority scheme
        "transcription.process_file": {"queue": CeleryQueues.GPU},
        # Pipeline chain tasks (3-stage: CPU preprocess → GPU transcribe → CPU postprocess)
        # NOTE: "transcription.gpu_transcribe" is intentionally NOT listed here so
        # dispatch.py can route it to either "gpu" or "cloud-asr" at call time.
        # NOTE: "transcription.cpu_transcribe" is also intentionally NOT listed here
        # so dispatch.py can route it to "cpu-transcribe" at call time.
        "transcription.preprocess": {"queue": CeleryQueues.CPU},
        "transcription.postprocess": {"queue": CeleryQueues.CPU},
        "transcription.enrich_and_dispatch": {"queue": CeleryQueues.CPU},
        "transcription.pipeline_error": {"queue": CeleryQueues.UTILITY},
        "rediarize": {"queue": CeleryQueues.GPU},
        "update_speaker_embedding_on_reassignment": {"queue": CeleryQueues.GPU},
        "extract_v4_embeddings": {"queue": CeleryQueues.GPU},
        "extract_v4_embeddings_batch": {"queue": CeleryQueues.GPU},
        "speaker.recluster_all": {"queue": CeleryQueues.GPU},
        "speaker.cluster_for_file": {"queue": CeleryQueues.CPU},
        # Download Queue - Network I/O tasks (concurrency=3, no GPU)
        "download.media_url": {"queue": CeleryQueues.DOWNLOAD},
        "download.media_playlist": {"queue": CeleryQueues.DOWNLOAD},
        "download.prepare_media": {"queue": CeleryQueues.DOWNLOAD},
        "download.prepare_bulk_subtitles": {"queue": CeleryQueues.DOWNLOAD},
        # CPU Queue - CPU-intensive parallel tasks (concurrency=8, no GPU)
        "media.generate_waveform": {"queue": CeleryQueues.CPU},
        "media.generate_waveform_data": {"queue": CeleryQueues.CPU},
        "analytics.analyze_transcript": {"queue": CeleryQueues.CPU},
        "detect_speaker_attributes": {"queue": CeleryQueues.CPU},
        "migrate_speaker_attributes": {"queue": CeleryQueues.CPU},
        "detect_speaker_attributes_batch": {"queue": CeleryQueues.GPU},
        "analyze_speakers_combined_batch": {"queue": CeleryQueues.GPU},
        "migrate_speakers_combined": {"queue": CeleryQueues.CPU},
        "system.update_gpu_stats": {"queue": CeleryQueues.CPU},
        "migrate_speaker_embeddings_to_v4": {"queue": CeleryQueues.CPU},
        "migration.normalize_embeddings": {"queue": CeleryQueues.CPU},
        "generate_thumbnail": {"queue": CeleryQueues.CPU},
        "migrate_thumbnails_to_webp": {"queue": CeleryQueues.CPU},
        "reindex_transcripts": {"queue": CeleryQueues.CPU},
        "reindex_batch": {"queue": CeleryQueues.CPU},
        "search_index_maintenance": {"queue": CeleryQueues.CPU},
        "opensearch_orphan_cleanup": {"queue": CeleryQueues.CPU},
        "speaker_embedding_consistency_check": {"queue": CeleryQueues.CPU},
        "speaker_embedding_consistency_repair_batch": {"queue": CeleryQueues.GPU},
        "process_speaker_update_background": {"queue": CeleryQueues.CPU},
        # Rename propagation into the chunk plane (issue #405). Lightweight
        # update_by_query, but user-visible latency matters (chat and search go
        # on answering with the old name until it lands), so it rides the CPU
        # queue next to the speaker update that triggers it rather than the
        # slower utility queue.
        "propagate_speaker_rename": {"queue": CeleryQueues.CPU},
        "propagate_title_rename": {"queue": CeleryQueues.CPU},
        # Digest-plane regeneration triggered by a rename (#383 addendum-G1).
        # Same queue and same reasoning as the two routes above: it is
        # dispatched from the same `dispatch_speaker_rename` call and a
        # renamed speaker's summary tier should not lag behind the roster
        # rewrite by much. A task with no route falls through to the default
        # 'celery' queue silently — see `_validate_task_routes` below.
        "regenerate_rename_digests": {"queue": CeleryQueues.CPU},
        "process_speaker_merge_background": {"queue": CeleryQueues.CPU},
        "extract_speaker_embeddings": {"queue": CeleryQueues.GPU},
        # NLP Queue - LLM API calls (concurrency=4, no GPU needed)
        "ai.generate_summary": {"queue": CeleryQueues.NLP},
        "ai.identify_speakers": {"queue": CeleryQueues.NLP},
        "ai.extract_topics": {"queue": CeleryQueues.NLP},
        "ai.extract_topics_batch": {"queue": CeleryQueues.NLP},
        "ai.group_batch_files": {"queue": CeleryQueues.NLP},
        "ai.retroactive_auto_label": {"queue": CeleryQueues.NLP},
        "ai.auto_label_batch": {"queue": CeleryQueues.NLP},
        # Deterministic ingest artifacts (#383 Phase 2). No LLM and no model load — it
        # rides the nlp pool because that is the CPU-only enrichment pool, not because
        # it calls a provider. It must still run when none is configured (#403 D6).
        "artifacts.generate_file_facts": {"queue": CeleryQueues.NLP},
        # Redaction Queue - Content moderation detection (dedicated CPU service)
        "redaction.detect": {"queue": CeleryQueues.REDACTION},
        "redaction.reindex_all": {"queue": CeleryQueues.REDACTION},
        # Embedding Queue - Search indexing with embedding model (concurrency=1)
        "index_transcript_search": {"queue": CeleryQueues.EMBEDDING},
        # Access/tag index updates are lightweight OpenSearch writes (no GPU/embedding needed)
        "update_file_access_index": {"queue": CeleryQueues.UTILITY},
        "update_file_tags_index": {"queue": CeleryQueues.UTILITY},
        # Opt-in speaker_id/profile_id backfill maintenance pass (search_indexing_task.py) —
        # lightweight OpenSearch writes, same shape as its two siblings above.
        "backfill_speaker_id_fields": {"queue": CeleryQueues.UTILITY},
        # Utility Queue - Lightweight maintenance tasks (concurrency=8)
        "system.startup_recovery": {"queue": CeleryQueues.UTILITY},
        "system.recover_user_files": {"queue": CeleryQueues.UTILITY},
        "system.health_check": {"queue": CeleryQueues.UTILITY},
        "cleanup_expired_files": {"queue": CeleryQueues.UTILITY},
        "cleanup.run_periodic_cleanup": {"queue": CeleryQueues.UTILITY},
        "cleanup.deep_cleanup": {"queue": CeleryQueues.UTILITY},
        "cleanup.health_check": {"queue": CeleryQueues.UTILITY},
        "cleanup.emergency_recovery": {"queue": CeleryQueues.UTILITY},
        "cleanup.scratch_janitor": {"queue": CeleryQueues.CPU},
        "cleanup.orphan_upload_sweeper": {"queue": CeleryQueues.UTILITY},
        "check_migration_status": {"queue": CeleryQueues.UTILITY},
        "finalize_v4_migration": {"queue": CeleryQueues.UTILITY},
        "export_transcript_baseline": {"queue": CeleryQueues.UTILITY},
        "compare_transcript_baseline": {"queue": CeleryQueues.UTILITY},
        "imohash_recompute.recompute_all": {"queue": CeleryQueues.UTILITY},
        # Watch Sources (issue #26)
        "watch_source.scan_all": {"queue": CeleryQueues.UTILITY},
        "watch_source.scan_single": {"queue": CeleryQueues.DOWNLOAD},
        "watch_source.stitch_and_import": {"queue": CeleryQueues.CPU},
        "watch_source.send_notification": {"queue": CeleryQueues.UTILITY},
        "watch_source.cleanup_temp": {"queue": CeleryQueues.UTILITY},
        # Scheduled database backups (Feature C)
        "backup.check_schedule": {"queue": CeleryQueues.UTILITY},
        "backup.run": {"queue": CeleryQueues.UTILITY},
        # Media mirror (issue #242): due-check is lightweight (utility); the run is
        # bulk object I/O → download queue (NEVER the gpu queue).
        "backup.mirror_check_schedule": {"queue": CeleryQueues.UTILITY},
        "backup.mirror_run": {"queue": CeleryQueues.DOWNLOAD},
        # Directory reconciliation / LDAP deprovisioning: LDAP searches + small DB
        # writes. CPU queue — never gpu.
        "directory.sync_check_schedule": {"queue": CeleryQueues.CPU},
        "directory.sync_run": {"queue": CeleryQueues.CPU},
        # GDPR Art. 17 reconciliation (issue #442): small DB reads plus, when there is
        # deferred work, object-storage and OpenSearch deletes. Utility — never gpu.
        "gdpr.erasure_reconcile": {"queue": CeleryQueues.UTILITY},
    },
    # Configure beat schedule for periodic tasks
    beat_schedule={
        "periodic-health-check": {
            "task": "system.health_check",
            "schedule": crontab(minute="*/10"),  # Run every 10 minutes
            "options": {"queue": "utility", "priority": 3},  # UtilityPriority.OPERATIONAL
        },
        "search-index-maintenance": {
            "task": "search_index_maintenance",
            "schedule": crontab(minute=0, hour="*/6"),  # Every 6 hours
            "options": {"queue": "cpu", "priority": 8},  # CPUPriority.MAINTENANCE
        },
        "opensearch-orphan-cleanup": {
            "task": "opensearch_orphan_cleanup",
            "schedule": crontab(minute=0, hour="3,9,15,21"),  # Every 6h, offset from maintenance
            "options": {"queue": "cpu", "priority": 8},  # CPUPriority.MAINTENANCE
        },
        "embedding-consistency-check": {
            "task": "speaker_embedding_consistency_check",
            "schedule": crontab(minute="*/10"),  # Every 10 minutes
            "options": {"queue": "cpu", "priority": 8},  # CPUPriority.MAINTENANCE
        },
        "gpu-stats-update": {
            "task": "system.update_gpu_stats",
            "schedule": crontab(minute="*/5"),  # Run every 5 minutes
            "options": {"queue": "cpu", "priority": 5},  # CPUPriority.SYSTEM
        },
        "cleanup-expired-files": {
            "task": "cleanup_expired_files",
            "schedule": crontab(minute=0),  # Every hour on the hour
            "options": {"queue": "utility", "priority": 5},  # UtilityPriority.ROUTINE
        },
        "scratch-janitor": {
            "task": "cleanup.scratch_janitor",
            "schedule": crontab(minute=15),  # Hourly at :15, offset from cleanup/maintenance
            "options": {"queue": "cpu", "priority": 5},  # CPUPriority.SYSTEM
        },
        "orphan-upload-sweeper": {
            "task": "cleanup.orphan_upload_sweeper",
            # Every 15 minutes, offset from the hourly cleanup tasks. PENDING
            # rows older than 30 min are the signal we never heard back from
            # the client / presigned PUT.
            "schedule": crontab(minute="5,20,35,50"),
            "options": {"queue": "utility", "priority": 5},  # UtilityPriority.ROUTINE
        },
        "watch-source-scan": {
            "task": "watch_source.scan_all",
            # Every minute the orchestrator checks which enabled sources are due
            # per their own polling_interval_minutes (DB-driven, no restart).
            "schedule": crontab(minute="*"),
            "options": {"queue": "utility", "priority": 5},  # UtilityPriority.ROUTINE
        },
        "watch-source-temp-cleanup": {
            "task": "watch_source.cleanup_temp",
            "schedule": crontab(minute=25),  # hourly at :25, offset from other jobs
            "options": {"queue": "utility", "priority": 7},  # UtilityPriority.BACKGROUND
        },
        "backup-check-schedule": {
            "task": "backup.check_schedule",
            # Every 5 minutes the check task loads DB-backed backup settings and
            # decides (against the stored cron + last_run_at) whether a backup is
            # due — fully DB-driven, no beat restart when the schedule changes.
            "schedule": crontab(minute="*/5"),
            "options": {"queue": "utility", "priority": 5},  # UtilityPriority.ROUTINE
        },
        "chat-retention-sweep": {
            "task": "chat.retention_sweep",
            # Daily at 04:10. A no-op unless an admin sets chat.retention_days
            # above 0, so this costs one cheap settings read a day by default.
            "schedule": crontab(minute=10, hour=4),
            "options": {"queue": "utility", "priority": 7},  # UtilityPriority.BACKGROUND
        },
        "gdpr-erasure-reconcile": {
            "task": "gdpr.erasure_reconcile",
            # Daily at 04:40, offset from the chat retention sweep. Two indexed
            # queries returning nothing on a deployment that has never had an erasure
            # request; real work only when one was deferred behind a legal hold or a
            # restore resurrected a subject. Daily rather than hourly because the
            # deadline it defends is one MONTH (Art. 12(3)) and the prompt path is
            # the hook in tasks/erasure_reconciliation.notify_hold_released.
            "schedule": crontab(minute=40, hour=4),
            "options": {"queue": "utility", "priority": 7},  # UtilityPriority.BACKGROUND
        },
        "media-mirror-check-schedule": {
            "task": "backup.mirror_check_schedule",
            # Same DB-driven due-check pattern for the media mirror (issue #242);
            # offset from the backup check so both never contend on the same tick.
            "schedule": crontab(minute="2-59/5"),
            "options": {"queue": "utility", "priority": 5},  # UtilityPriority.ROUTINE
        },
        "directory-sync-check-schedule": {
            "task": "directory.sync_check_schedule",
            # Same DB-driven due-check pattern: the cron the operator actually
            # configures lives in SystemSettings, so this tick only asks "is it
            # due yet?". Quarter-hourly at odd minutes, offset from every other
            # scheduled job. This is the ONLY beat entry that touches User rows.
            "schedule": crontab(minute="7,22,37,52"),
            "options": {"queue": CeleryQueues.CPU, "priority": 8},  # CPUPriority.MAINTENANCE
        },
    },
)

# Make this app the process-wide default, not just this thread's (issue #485).
#
# `Celery.__init__` sets only the CREATING thread's `celery._state._tls.current_app`;
# it never sets the module-global `default_app`. Anything that resolves a task through
# `get_current_app()` — every `@shared_task` proxy — therefore reads
# `_tls.current_app or default_app`, and on a thread that did not construct the app both
# are None. Celery then mints a fallback `Celery('default')` with NO broker configured and
# caches it globally, so the dispatch goes to the amqp:// class default and is refused.
#
# The API hits this on every request: FastAPI/Starlette runs each sync `def` endpoint on a
# `run_in_threadpool` worker thread, which is never the import thread. That made every admin
# "Run now" action 500 intermittently, while workers (which import this module on their own
# main thread) were unaffected.
#
# `set_default()` is what populates `default_app`, so the fallback resolves to the real,
# configured app from any thread and any process. Keep this even though the `@shared_task`
# decorators below were converted to `@celery_app.task` — it is the cheap general guard, and
# without it a single reintroduced `shared_task` silently brings the bug back.
celery_app.set_default()


# Apply our logging config (text/JSON per settings.LOG_FORMAT) to Celery.
# Connecting setup_logging ALSO disables Celery's root-logger hijack
# (worker_hijack_root_logger), so the configuration survives worker startup.
# We deliberately use setup_logging — NOT worker_process_init, which only fires
# in prefork child processes and never under this app's default --pool=threads.
@setup_logging.connect
def configure_celery_logging(**kwargs):
    """Configure structured/text logging for Celery workers and the beat."""
    from app.core.logging_config import configure_logging

    configure_logging()


# Correlate background-task logs with the HTTP request that spawned them: the
# request_id rides in the task headers and is adopted into the audit ContextVar
# for the duration of the task (reset in close_session_after_task below). Tasks
# that spawn sub-tasks propagate automatically — publish happens inside the
# task context, so before_task_publish re-reads the now-set ContextVar.
@before_task_publish.connect
def inject_request_id_header(headers=None, **kwargs):
    """Stamp the current request_id onto outgoing task headers (no-op if empty)."""
    from app.middleware.audit import get_request_id

    request_id = get_request_id()
    if request_id and headers is not None:
        headers["request_id"] = request_id


@task_prerun.connect
def adopt_request_id(task=None, **kwargs):
    """Adopt the inbound request_id header into the task's ContextVar."""
    from app.middleware.audit import set_request_id

    request = getattr(task, "request", None)
    request_id = getattr(request, "request_id", None) if request is not None else None
    if request_id:
        set_request_id(request_id)


# The watch-source FS-event layer needs a long-lived process, and beat is the
# one service that is single-instance by design — running two would double every
# scheduled task, so nobody scales it. Starting it here (and NOT on the workers)
# gives exactly one observer set per deployment. It never raises: any failure
# logs and leaves the every-minute `watch_source.scan_all` poll as the sole
# mechanism, which is the pre-#294 behaviour.
@beat_init.connect
def start_watch_source_fs_events(**kwargs):
    """Start the watchdog-based watch-source observer inside celery-beat."""
    try:
        from app.services.watch_sources.fs_events import start_supervisor

        start_supervisor()
    except Exception as e:  # noqa: BLE001 - beat must start regardless
        logger.error(f"Watch-source FS-event supervisor could not be started: {e}")


# Signal handlers for proper database connection management
@worker_process_init.connect
def init_worker_process(**kwargs):
    """Initialize worker process - register HF token and dispose connections."""
    import os
    import warnings

    # Suppress benign warnings from AI libraries (community models, library compat)
    warnings.filterwarnings("ignore", message=".*loss function.*", category=UserWarning)
    warnings.filterwarnings("ignore", message=".*not writable.*", category=UserWarning)
    warnings.filterwarnings("ignore", module="pytorch_lightning")
    warnings.filterwarnings("ignore", module="lightning_fabric")

    # Register HuggingFace token for gated model access (e.g., pyannote)
    # Skip in offline mode — models are pre-downloaded and no network is available
    hf_token = os.getenv("HUGGINGFACE_TOKEN")
    if hf_token and os.getenv("HF_HUB_OFFLINE") != "1":
        try:
            from huggingface_hub import login

            login(token=hf_token, add_to_git_credential=False)
            logger.info("HuggingFace token registered for gated model access")
        except Exception as e:
            logger.warning(f"Failed to register HuggingFace token: {e}")

    from app.db.base import engine

    engine.dispose()


@worker_ready.connect
def warn_inert_max_tasks_per_child(**kwargs):
    """Warn when --max-tasks-per-child is configured but cannot take effect.

    ``--max-tasks-per-child`` is a PREFORK-pool feature: it recycles the child process
    after N tasks. Under ``--pool=threads`` there are no child processes, so Celery
    silently ignores it (issue #284 A1.7). The GPU workers default to threads —
    deliberately, so model weights stay pinned in VRAM between tasks — which means an
    operator lowering GPU_MAX_TASKS to bound a VRAM leak gets **no recycling at all**
    and no indication of that.

    We warn rather than switch pools: forcing prefork would reload the model on every
    task and destroy the performance characteristic threads exists to provide. An
    operator who genuinely needs recycling sets GPU_WORKER_POOL=prefork and accepts
    that cost.
    """
    import os
    import sys

    argv = " ".join(sys.argv)
    if "--pool=threads" not in argv and os.getenv("GPU_WORKER_POOL", "threads") != "threads":
        return
    if "--max-tasks-per-child" not in argv:
        return

    # A deliberately huge value means "never recycle" and is not a misconfiguration.
    try:
        configured = int(argv.split("--max-tasks-per-child=")[1].split()[0])
    except (IndexError, ValueError):
        return
    if configured >= 10000:
        return

    logger.warning(
        "--max-tasks-per-child=%d is set but this worker uses the threads pool, where "
        "Celery IGNORES it — there is no worker recycling and no VRAM-leak guard. "
        "Set GPU_WORKER_POOL=prefork if you need recycling (models reload per task).",
        configured,
    )


@worker_ready.connect
def preload_models(**kwargs):
    """Preload AI models at worker startup.

    GPU workers: Load Whisper + PyAnnote into VRAM (shared across threads).
    CPU-transcribe workers: Load lightweight Whisper model into RAM.
    Other CPU workers: No model preloading needed.
    """
    import os

    # GPU model preloading — ONLY on GPU workers (PRELOAD_GPU_MODELS=true).
    # Other workers (cpu-processor, search-indexer, etc.) must NOT load models,
    # even if CUDA is available, to avoid wasting 15+ GB of GPU memory.
    # Set via docker-compose.yml environment for gpu worker containers only.
    is_gpu_worker = os.environ.get("PRELOAD_GPU_MODELS", "").lower() == "true"

    try:
        if is_gpu_worker:
            from app.transcription.config import TranscriptionConfig

            config = TranscriptionConfig.from_environment()
            if config.device == "cuda":
                import torch

                from app.transcription.model_manager import ModelManager

                ModelManager.get_instance().ensure_models_loaded(config)

                # Enable TF32 AFTER model loading. PyAnnote's fix_reproducibility()
                # disables TF32 during Pipeline.from_pretrained(). Re-enabling here
                # gives Whisper ~15-20% speedup on Ampere+ GPUs (RTX 3000+, A-series).
                # pipeline.py also re-enables after each diarization run.
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
                logger.info("TF32 enabled for Tensor Core acceleration")

                # Pin the model name so subsequent tasks use the loaded model,
                # even if the admin changes the DB setting before restarting.
                TranscriptionConfig.pin_model(config.model_name)

                logger.info(
                    "GPU models preloaded and pinned "
                    f"(model={config.model_name}, "
                    f"concurrent_requests={config.concurrent_requests})"
                )
        else:
            logger.info("Skipping GPU model preload (PRELOAD_GPU_MODELS not set)")
    except Exception as e:
        logger.debug(f"GPU model preloading skipped: {e}")

    # CPU lightweight model preloading
    if os.getenv("PRELOAD_CPU_WHISPER", "").lower() == "true":
        try:
            from app.transcription.config import TranscriptionConfig as CpuTranscriptionConfig

            cpu_config = CpuTranscriptionConfig.for_cpu_lightweight()
            logger.info(
                f"Preloading CPU lightweight model '{cpu_config.model_name}' "
                f"(compute_type={cpu_config.compute_type})..."
            )
            from faster_whisper import WhisperModel

            # Load model to warm the cache — subsequent loads are instant
            _model = WhisperModel(
                cpu_config.model_name,
                device="cpu",
                compute_type="int8",
            )
            del _model
            logger.info(f"CPU lightweight model '{cpu_config.model_name}' preloaded successfully")
        except Exception as e:
            logger.warning(f"CPU lightweight model preloading failed: {e}")

    # Content-redaction model preloading — ONLY on the dedicated celery-redaction
    # worker (PRELOAD_REDACTION_MODELS=true). Loads Presidio+GLiNER (PII) and the
    # toxicity classifier once, shared across the threads pool. No GPU.
    if os.getenv("PRELOAD_REDACTION_MODELS", "").lower() == "true":
        try:
            # Cap PyTorch intra-op threads so a single inference can't peg every core
            # on a shared single machine (env OMP_NUM_THREADS is the primary control;
            # this is a runtime fallback). Background redaction yields to user-facing work.
            _rt = os.getenv("OMP_NUM_THREADS") or os.getenv("REDACTION_TORCH_THREADS")
            if _rt:
                try:
                    import torch

                    torch.set_num_threads(int(_rt))
                    logger.info("Redaction worker: torch intra-op threads capped at %s", _rt)
                except Exception as _te:  # noqa: BLE001
                    logger.debug("Could not cap torch threads: %s", _te)

            from app.services.redaction.detectors import pii_presidio
            from app.services.redaction.detectors import toxicity

            ok_pii = pii_presidio.preload()
            ok_tox = toxicity.preload()

            # Bring the PII pool's workers up now. Spawned workers each import the app and
            # load spaCy, so a cold pool makes the first scan slower than the sequential
            # path it replaces — the cost belongs at worker start, not in a user's scan.
            from app.services.redaction import pii_pool

            ok_pool = pii_pool.warm()
            logger.info(
                "Redaction models preloaded (pii=%s, toxicity=%s, pii_pool=%s)",
                ok_pii,
                ok_tox,
                ok_pool,
            )
        except Exception as e:
            logger.warning(f"Redaction model preloading failed: {e}")

    # Validate that all registered tasks have explicit queue routes
    _validate_task_routes()


def _validate_task_routes():
    """Log warnings for tasks missing from task_routes.

    Runs once at worker startup. Tasks not in task_routes and without a
    decorator-level queue= will silently go to the default 'celery' queue,
    which may not be the intended behavior.
    """
    # Tasks intentionally excluded from task_routes (dynamically routed at call time)
    intentionally_unrouted = {
        "transcription.gpu_transcribe",  # Routed to "gpu" or "cloud-asr" by dispatch.py
        "transcription.cpu_transcribe",  # Routed to "cpu-transcribe" by dispatch.py
    }

    routed_names = set(celery_app.conf.task_routes.keys())
    unrouted = []

    for name in celery_app.tasks:
        if name.startswith("celery."):
            continue
        if name in intentionally_unrouted:
            continue
        if name not in routed_names:
            unrouted.append(name)

    if unrouted:
        for name in sorted(unrouted):
            logger.warning(
                f"Task '{name}' has no task_routes entry — will go to default 'celery' queue"
            )
    else:
        logger.info(
            f"Task route validation passed: {len(routed_names)} routes, "
            f"{len(intentionally_unrouted)} intentionally dynamic"
        )


@task_postrun.connect
def close_session_after_task(**kwargs):
    """Clear the per-task request_id correlation.

    Deliberately does NOT call ``engine.dispose()`` (issue #284 A1.6). Disposing on
    every ``task_postrun`` tears down the whole connection pool after each task, so
    every task paid a fresh Postgres TCP + TLS + auth handshake and pooling did nothing.
    The post-fork dispose in ``worker_process_init`` is what actually matters — it stops
    a forked child inheriting the parent's sockets — and that is still in place.

    Sessions are closed by their own context managers (``session_scope`` /
    ``SessionLocal`` teardown), not here, so nothing leaks by removing this.
    """
    from app.middleware.audit import set_request_id

    # Clear the request_id adopted in task_prerun so it can't leak into the next
    # task that reuses this thread/process (threads pool reuses workers).
    set_request_id("")
