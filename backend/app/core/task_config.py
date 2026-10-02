"""
Task recovery and monitoring configuration.

This module centralizes all configuration related to task recovery,
monitoring thresholds, and recovery policies.

The recovery thresholds are read from the environment (issue #1020) so an operator whose
GPU workers can legitimately be absent for a while — scaled to zero, paused, or behind a long
backlog — can widen them without a code change. Every value has the default it had when it
was hardcoded, except the transcription-liveness settings, which are new.
"""

import logging
import os
from dataclasses import dataclass
from dataclasses import field

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive integer from the environment, falling back to ``default``.

    An unparseable or too-small value logs a warning and uses the default rather than
    raising: this module is imported by every worker at startup, and a typo in one recovery
    threshold must not take the whole worker fleet down.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using the default %d", name, raw, default)
        return default
    if value < minimum:
        logger.warning(
            "%s=%d is below the minimum %d; using the default %d", name, value, minimum, default
        )
        return default
    return value


def _default_max_task_durations() -> dict[str, int]:
    return {
        # Measured from when the run STARTED on a worker, never from when it was queued:
        # a transcription waiting for a GPU worker has not used any of its budget (#1020).
        "transcription": _int_env("TASK_MAX_DURATION_TRANSCRIPTION_SECONDS", 3600),
        "extract_audio": 600,  # 10 minutes max for audio extraction
        "analyze_transcript": 900,  # 15 minutes max for analysis
        "summarize_transcript": 900,  # 15 minutes max for summarization
        "default": _int_env("TASK_MAX_DURATION_DEFAULT_SECONDS", 1800),
    }


@dataclass
class TaskRecoveryConfig:
    """Configuration for task recovery operations."""

    # Maximum allowed duration for different task types (in seconds)
    MAX_TASK_DURATIONS: dict[str, int] | None = field(default_factory=_default_max_task_durations)

    # Time before considering a task stale (in seconds)
    STALENESS_THRESHOLD: int = field(
        default_factory=lambda: _int_env("TASK_RECOVERY_STALENESS_SECONDS", 300)
    )

    # Time to wait before startup recovery kicks in (in seconds)
    STARTUP_RECOVERY_DELAY: int = 10

    # Periodic health check interval (in minutes)
    HEALTH_CHECK_INTERVAL: int = 10

    # Maximum runtime for health check task (in seconds)
    HEALTH_CHECK_MAX_RUNTIME: int = 480  # 8 minutes, less than 10 min interval

    # Task execution overlap prevention
    PREVENT_TASK_OVERLAP: bool = True

    # File age thresholds for recovery (in hours)
    FILE_RECOVERY_AGE_THRESHOLD: int = 2
    PENDING_FILE_RETRY_THRESHOLD: int = 6

    # Orphaned task threshold (in hours)
    ORPHANED_TASK_THRESHOLD: int = field(
        default_factory=lambda: _int_env("TASK_RECOVERY_ORPHANED_HOURS", 1)
    )

    # How often a running transcription stage refreshes its lease (heartbeat), and how long
    # one refresh stays valid. A run whose lease has lapsed is treated as dead, so the TTL must
    # cover the longest stretch a worker can go without scheduling its heartbeat thread (six
    # missed beats at the defaults). This TTL is the detection latency for a killed worker.
    TRANSCRIPTION_HEARTBEAT_INTERVAL: int = field(
        default_factory=lambda: _int_env("TRANSCRIPTION_HEARTBEAT_INTERVAL_SECONDS", 15)
    )
    TRANSCRIPTION_HEARTBEAT_TTL: int = field(
        default_factory=lambda: _int_env("TRANSCRIPTION_HEARTBEAT_TTL_SECONDS", 90)
    )

    # Infrastructure requeues per file (a dead worker's stage put back on its queue, or a dead
    # run re-dispatched). Counted separately from MediaFile.retry_count so worker loss does
    # not spend the file's error-retry budget; past this cap the file fails as interrupted
    # instead of cycling workers forever (a file that OOM-kills every worker it reaches).
    TRANSCRIPTION_MAX_INFRA_REQUEUES: int = field(
        default_factory=lambda: _int_env("TRANSCRIPTION_MAX_INFRA_REQUEUES", 5)
    )

    # How long a transcription may wait in the broker before recovery treats its message as
    # lost. Waiting for a worker is normal and never fails a file before this (#1020).
    TRANSCRIPTION_QUEUE_MAX_WAIT: int = field(
        default_factory=lambda: _int_env("TRANSCRIPTION_QUEUE_MAX_WAIT_SECONDS", 604800)
    )

    # Broker-level recovery of transcription stages held by a dead worker
    # (app/core/broker_orphans.py). A stage message in kombu's ``unacked`` hash whose run
    # has no live heartbeat and that was delivered longer ago than BROKER_ORPHAN_STALE is
    # orphaned: it is excluded from ``celery_queue_reserved`` and put back at the FRONT of
    # its queue by a sweep that runs every BROKER_ORPHAN_SWEEP_INTERVAL. Detection latency
    # is therefore about TRANSCRIPTION_HEARTBEAT_TTL + one sweep interval (~2.5 min by
    # default), not CELERY_VISIBILITY_TIMEOUT (6 h). The stale age only guards a delivery that
    # has not started yet (no lease is expected before its first beat).
    BROKER_ORPHAN_STALE: int = field(
        default_factory=lambda: _int_env("BROKER_ORPHAN_STALE_SECONDS", 120)
    )
    BROKER_ORPHAN_SWEEP_INTERVAL: int = field(
        default_factory=lambda: _int_env("BROKER_ORPHAN_SWEEP_INTERVAL_SECONDS", 60)
    )

    # Worker-loss replay for idempotent tasks (issue #1067, app/core/task_replay.py). A task
    # in REPLAYABLE_TASKS heartbeats while it runs; once the heartbeat has lapsed the
    # reclaim sweep re-sends it under the same task id, at most TASK_REPLAY_MAX_ATTEMPTS
    # times. The TTL is how long a dead worker's task waits before it is re-sent. It must
    # cover the longest stretch a process can go without scheduling its heartbeat thread.
    TASK_HEARTBEAT_INTERVAL: int = field(
        default_factory=lambda: _int_env("TASK_HEARTBEAT_INTERVAL_SECONDS", 30)
    )
    TASK_HEARTBEAT_TTL: int = field(
        default_factory=lambda: _int_env("TASK_HEARTBEAT_TTL_SECONDS", 120)
    )
    TASK_REPLAY_MAX_ATTEMPTS: int = field(
        default_factory=lambda: _int_env("TASK_REPLAY_MAX_ATTEMPTS", 2, minimum=0)
    )
    # A replay record older than this is dropped rather than replayed: work lost that long
    # ago is recovered, if at all, by the file-level recovery steps, not re-run blind.
    TASK_REPLAY_MAX_AGE: int = field(
        default_factory=lambda: _int_env("TASK_REPLAY_MAX_AGE_SECONDS", 86400)
    )

    # OOM retry configuration
    OOM_RETRY_ENABLED: bool = True  # Enable/disable OOM auto-retry
    OOM_BACKOFF_BASE_MINUTES: int = 10  # Base delay for exponential backoff (2^n * this value)

    def __post_init__(self):
        if self.MAX_TASK_DURATIONS is None:
            self.MAX_TASK_DURATIONS = _default_max_task_durations()
        if self.TRANSCRIPTION_HEARTBEAT_TTL <= self.TRANSCRIPTION_HEARTBEAT_INTERVAL:
            # A TTL no longer than the refresh interval expires between two beats of a
            # perfectly healthy run, so every running transcription would read as dead.
            logger.warning(
                "TRANSCRIPTION_HEARTBEAT_TTL_SECONDS (%d) must exceed the interval (%d); using %d",
                self.TRANSCRIPTION_HEARTBEAT_TTL,
                self.TRANSCRIPTION_HEARTBEAT_INTERVAL,
                self.TRANSCRIPTION_HEARTBEAT_INTERVAL * 3,
            )
            self.TRANSCRIPTION_HEARTBEAT_TTL = self.TRANSCRIPTION_HEARTBEAT_INTERVAL * 3
        if self.BROKER_ORPHAN_STALE < self.TRANSCRIPTION_HEARTBEAT_INTERVAL * 2:
            # A delivery younger than two beats may simply not have sent its first one yet.
            logger.warning(
                "BROKER_ORPHAN_STALE_SECONDS (%d) must be at least twice the heartbeat "
                "interval (%d); using %d",
                self.BROKER_ORPHAN_STALE,
                self.TRANSCRIPTION_HEARTBEAT_INTERVAL,
                self.TRANSCRIPTION_HEARTBEAT_INTERVAL * 2,
            )
            self.BROKER_ORPHAN_STALE = self.TRANSCRIPTION_HEARTBEAT_INTERVAL * 2
        if self.TASK_HEARTBEAT_TTL <= self.TASK_HEARTBEAT_INTERVAL:
            logger.warning(
                "TASK_HEARTBEAT_TTL_SECONDS (%d) must exceed the interval (%d); using %d",
                self.TASK_HEARTBEAT_TTL,
                self.TASK_HEARTBEAT_INTERVAL,
                self.TASK_HEARTBEAT_INTERVAL * 3,
            )
            self.TASK_HEARTBEAT_TTL = self.TASK_HEARTBEAT_INTERVAL * 3


# Global configuration instance
task_recovery_config = TaskRecoveryConfig()
