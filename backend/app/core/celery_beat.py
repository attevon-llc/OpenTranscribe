"""The celery-beat schedule (installed as ``beat_schedule`` by ``core/celery.py``).

Every entry also carries a :class:`BeatEffect` in :data:`BEAT_ENTRY_EFFECT`, read by the
inertness checker (``app/core/inert.py``, issue #898) and enforced by
``tests/unit/test_inert_checker.py``: an entry added here without a classification fails it.
"""

from enum import StrEnum
from typing import Any

from celery.schedules import crontab

from app.core.queues import CeleryQueues
from app.core.task_config import task_recovery_config

BEAT_SCHEDULE: dict[str, dict[str, Any]] = {
    "periodic-health-check": {
        "task": "system.health_check",
        "schedule": crontab(minute="*/10"),  # Run every 10 minutes
        "options": {"queue": "utility", "priority": 3},  # UtilityPriority.OPERATIONAL
    },
    # Requeues transcription stages a dead worker took with it (app/core/broker_orphans.py).
    # A plain interval, not a crontab, so it can run more often than once a minute.
    "reclaim-orphaned-deliveries": {
        "task": "system.reclaim_orphaned_deliveries",
        "schedule": float(task_recovery_config.BROKER_ORPHAN_SWEEP_INTERVAL),
        "options": {"queue": "utility", "priority": 3},  # UtilityPriority.OPERATIONAL
    },
    # Replays idempotent tasks whose worker died mid-run (issue #1067). Cheap when
    # nothing is lost: one HGETALL of the replay records plus one MGET of heartbeats.
    "reclaim-lost-tasks": {
        "task": "system.reclaim_lost_tasks",
        "schedule": crontab(minute="*/2"),
        "options": {"queue": "utility", "priority": 3},  # UtilityPriority.OPERATIONAL
    },
    "search-index-maintenance": {
        "task": "search_index_maintenance",
        "schedule": crontab(minute=0, hour="*/6"),  # Every 6 hours
        "options": {"queue": "cpu", "priority": 8},  # CPUPriority.MAINTENANCE
    },
    "search-index-sweep": {
        "task": "search_index_sweep",
        # Durable backstop for issue #1182: re-dispatches completed files whose search
        # indexing failed for good or never ran. Bounded batch + per-file cooldown; a
        # no-op when OPENSEARCH_ENABLED=false.
        "schedule": crontab(minute="1,11,21,31,41,51"),
        "options": {"queue": "utility", "priority": 5},  # UtilityPriority.ROUTINE
    },
    "neural-search-bootstrap": {
        "task": "neural_search_bootstrap",
        # Every 10 minutes. A cold or slow OpenSearch boot can outlast the startup
        # fast path's one-shot attempt (issue #625) — this is the self-heal. A
        # healthy deployment pays only the cheap probe every tick.
        "schedule": crontab(minute="3,13,23,33,43,53"),
        "options": {"queue": "utility", "priority": 5},  # UtilityPriority.ROUTINE
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
    "purge-trash": {
        "task": "files.purge_trash",
        # Hourly at :45, offset from cleanup-expired-files (:00). Destroys trashed files whose
        # window has ended (issue #1101); one indexed query when nothing is due.
        "schedule": crontab(minute=45),
        "options": {"queue": "utility", "priority": 7},  # UtilityPriority.BACKGROUND
    },
    "availability-reconcile": {
        "task": "search.availability_reconcile",
        # Quarter-hourly at :08/:23/:38/:53. Makes the index's trash and takedown stamp agree
        # with Postgres (issues #1101, #1253): a stamp that failed in the trash, restore,
        # quarantine or release request, or a document a concurrent writer landed after it. Idempotent update_by_query calls that match
        # nothing when the index already agrees.
        "schedule": crontab(minute="8,23,38,53"),
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
    "account-inactivity-sweep": {
        "task": "account.inactivity_sweep",
        # Daily at 04:25, offset from the chat retention (04:10) / GDPR erasure
        # (04:40) sweeps so none contend on the same tick. FedRAMP AC-2: a no-op
        # unless an admin sets ACCOUNT_EXPIRATION_ENABLED (default off), so this
        # costs one indexed query a day by default on a deployment that hasn't
        # opted in.
        "schedule": crontab(minute=25, hour=4),
        "options": {"queue": "utility", "priority": 5},  # UtilityPriority.ROUTINE
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
    "gdpr-erasure-execute-due": {
        "task": "gdpr.erasure_execute_due",
        # Hourly at :35. Runs scheduled erasures (self-service and organization) whose time has
        # come; the 04:40 reconcile runs the same executor and stays the guarantee, this is the
        # latency (issue #1105). One indexed query returning nothing when none is due.
        "schedule": crontab(minute=35),
        "options": {"queue": "utility", "priority": 7},  # UtilityPriority.BACKGROUND
    },
    "legal-acceptance-retention-sweep": {
        "task": "legal.acceptance_retention_sweep",
        # Daily at 05:10, after the 04:10-04:55 sweeps. Deletes pseudonymised legal
        # acceptance records whose retain_until has passed (v451); a no-op otherwise.
        "schedule": crontab(minute=10, hour=5),
        "options": {"queue": "utility", "priority": 7},  # UtilityPriority.BACKGROUND
    },
    "audit-outbox-relay": {
        "task": "audit.outbox_relay",
        # Every minute: delivers governance audit events the request could not (the sink was
        # down), retrying with backoff; a no-op query when the outbox has nothing due (v459).
        "schedule": 60.0,
        "options": {"queue": "utility", "priority": 3},  # UtilityPriority.OPERATIONAL
    },
    "audit-retention-sweep": {
        "task": "audit.retention_sweep",
        # Daily at 05:15. A no-op unless AUDIT_LOG_RETENTION_DAYS > 0 (default 0 = keep
        # forever); the database function enforces the 365-day floor regardless.
        "schedule": crontab(minute=15, hour=5),
        "options": {"queue": "utility", "priority": 7},  # UtilityPriority.BACKGROUND
    },
    "voiceprint-retention-sweep": {
        "task": "privacy.voiceprint_retention_sweep",
        # Daily at 05:25. A no-op unless privacy.voiceprint_retention_months > 0.
        "schedule": crontab(minute=25, hour=5),
        "options": {"queue": "utility", "priority": 7},  # UtilityPriority.BACKGROUND
    },
    "privacy-purge-reverify": {
        "task": "privacy.purge_reverify",
        # Quarter-hourly at odd minutes. Finishes (or fails as interrupted) a voiceprint purge a
        # dead worker left, from its privacy_purge_pending row (v461, issue #1334); one indexed
        # query returning nothing when no purge is outstanding.
        "schedule": crontab(minute="11,26,41,56"),
        "options": {"queue": "utility", "priority": 5},  # UtilityPriority.ROUTINE
    },
    "session-cap-sweep": {
        "task": "session.cap_sweep",
        # Daily at 04:55, offset from chat retention (04:10) / account
        # inactivity (04:25) / GDPR erasure (04:40) so none contend on the
        # same tick. Defence in depth for issue #632: every session-minting
        # path now enforces the cap itself, so this only has real work to do
        # after an admin LOWERS the cap (existing sessions don't shrink until
        # someone logs in again) or against a pre-fix backlog.
        "schedule": crontab(minute=55, hour=4),
        "options": {"queue": "utility", "priority": 7},  # UtilityPriority.BACKGROUND
    },
    "expired-refresh-token-cleanup": {
        "task": "auth.cleanup_expired_refresh_tokens",
        # Daily at 05:10, after the 04:55 session-cap sweep so the two never contend.
        # Expired rows are already unusable; this only reclaims table and index space.
        "schedule": crontab(minute=10, hour=5),
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
    "usage-report": {
        "task": "telemetry.usage_report",
        # Hourly at :47. Opt-in anonymous usage report (issue #1019): one settings read and
        # nothing else unless an administrator turned it on, then one small POST a day.
        "schedule": crontab(minute=47),
        "options": {"queue": "utility", "priority": 7},  # UtilityPriority.BACKGROUND
    },
}


class BeatEffect(StrEnum):
    """What a scheduled entry does to state that outlives one visit to the app."""

    #: Writes no application data (rows, objects, index documents); may refresh a cache or
    #: a metric. Safe to keep running on a deployment that must stay unchanged.
    READ_ONLY = "read-only"
    #: Changes, deletes, repairs or re-dispatches persisted state, or copies it elsewhere.
    MUTATING = "mutating"
    #: Brings new content or identities in from a source outside the app.
    INGEST = "ingest"


# A separate table, not a key inside each BEAT_SCHEDULE entry: celery.beat builds a
# ScheduleEntry(**entry) per item, whose __init__ takes no extra keyword, so a marker key
# there would stop beat from starting.
BEAT_ENTRY_EFFECT: dict[str, BeatEffect] = {
    "periodic-health-check": BeatEffect.MUTATING,  # recovers stuck tasks and files
    "reclaim-orphaned-deliveries": BeatEffect.MUTATING,  # requeues broker messages
    "reclaim-lost-tasks": BeatEffect.MUTATING,  # replays tasks a dead worker lost
    "search-index-maintenance": BeatEffect.MUTATING,  # dispatches reindexing
    "search-index-sweep": BeatEffect.MUTATING,  # re-dispatches failed indexing
    "neural-search-bootstrap": BeatEffect.MUTATING,  # registers/deploys the OpenSearch model
    "opensearch-orphan-cleanup": BeatEffect.MUTATING,  # deletes index documents
    "embedding-consistency-check": BeatEffect.MUTATING,  # dispatches GPU repair batches
    "gpu-stats-update": BeatEffect.READ_ONLY,  # refreshes the gpu_stats cache key only
    "cleanup-expired-files": BeatEffect.MUTATING,  # deletes files
    "purge-trash": BeatEffect.MUTATING,  # destroys trashed files past their window
    "availability-reconcile": BeatEffect.MUTATING,  # rewrites index documents' trash stamp
    "scratch-janitor": BeatEffect.MUTATING,  # deletes scratch directories
    "orphan-upload-sweeper": BeatEffect.MUTATING,  # deletes abandoned PENDING uploads
    "watch-source-scan": BeatEffect.INGEST,  # imports media from watch sources
    "watch-source-temp-cleanup": BeatEffect.MUTATING,  # deletes temp downloads
    "backup-check-schedule": BeatEffect.MUTATING,  # stamps settings, starts a backup
    "chat-retention-sweep": BeatEffect.MUTATING,  # deletes chat history
    "account-inactivity-sweep": BeatEffect.MUTATING,  # disables accounts
    "gdpr-erasure-reconcile": BeatEffect.MUTATING,  # erases subjects' data
    "gdpr-erasure-execute-due": BeatEffect.MUTATING,  # runs scheduled erasures
    "legal-acceptance-retention-sweep": BeatEffect.MUTATING,  # deletes acceptance records
    "audit-outbox-relay": BeatEffect.MUTATING,  # copies audit events out, marks/deletes rows
    "audit-retention-sweep": BeatEffect.MUTATING,  # deletes audit rows
    "voiceprint-retention-sweep": BeatEffect.MUTATING,  # deletes voiceprints
    "privacy-purge-reverify": BeatEffect.MUTATING,  # deletes centroid docs, fails lost purges
    "session-cap-sweep": BeatEffect.MUTATING,  # revokes sessions
    "expired-refresh-token-cleanup": BeatEffect.MUTATING,  # deletes expired session rows
    "media-mirror-check-schedule": BeatEffect.MUTATING,  # stamps settings, copies media out
    "directory-sync-check-schedule": BeatEffect.INGEST,  # pulls users from the directory
    "usage-report": BeatEffect.MUTATING,  # sends the opt-in usage report out, stamps settings
}
