"""
Migration progress tracking service using Redis.

This module provides a Redis-based service for tracking the progress of
long-running migrations like the v3 to v4 speaker embedding migration.
"""

import enum
import json
import logging
from datetime import UTC
from datetime import datetime
from typing import NotRequired
from typing import TypedDict
from typing import cast

import redis
from redis.exceptions import RedisError

from app.core.redis import get_request_redis

logger = logging.getLogger(__name__)

# Redis key prefix for migration tracking
MIGRATION_KEY_PREFIX = "embedding_migration"

# Issue #657, defect 6: 24h was quoted as the safety-net TTL while the docs
# themselves estimate 12+ hours for 1000+ files - leaving less than a 2x
# margin for a slow batch, a paused-for-migration-lock stretch, or simple
# GPU contention. Every increment_processed() call refreshes this TTL (see
# _INCREMENT_LUA's `EX`), so it is really "time since the last processed
# file", not "time since migration start" - but a long stall between files
# (a huge single file, a wedged GPU) could still exceed 24h and cause
# is_running() to read False mid-migration, aborting in-flight batches.
# 72h gives real headroom without keeping a genuinely-abandoned migration's
# Redis key alive indefinitely.
MIGRATION_STATUS_TTL_SECONDS = 72 * 60 * 60

# Upper bound on failed-file UUIDs kept for the progress API. The true failure
# count is still tracked (``failed_count``); only the listed UUIDs are capped.
MAX_RECORDED_FAILED_FILES = 1000


class MigrationStatus(TypedDict):
    """Type definition for migration status."""

    running: bool
    total_files: int
    processed_files: int
    failed_files: list[str]  # List of file UUIDs that failed
    started_at: str | None  # ISO timestamp
    completed_at: str | None  # ISO timestamp
    orchestrator_task_id: str | None
    last_updated: str | None  # ISO timestamp
    eta_seconds: NotRequired[float | None]  # Added by API layer from ProgressTracker


class RunState(enum.Enum):
    """Whether a migration is running, as far as Redis lets us tell."""

    RUNNING = "running"
    IDLE = "idle"
    UNKNOWN = "unknown"


class MigrationProgressService:
    """Service for tracking migration progress in Redis."""

    def __init__(self, key_prefix: str | None = None):
        """Initialize the service.

        Args:
            key_prefix: Redis key prefix. Defaults to MIGRATION_KEY_PREFIX.
        """
        self.key_prefix = key_prefix or MIGRATION_KEY_PREFIX
        self._redis_client: redis.Redis | None = None

    @property
    def redis_client(self) -> redis.Redis | None:
        """Return the shared Redis singleton."""
        if self._redis_client is None:
            try:
                self._redis_client = get_request_redis()
                self._redis_client.ping()
            except RedisError as e:
                logger.error(f"Failed to connect to Redis: {e}", exc_info=True)
                self._redis_client = None
        return self._redis_client

    def _get_key(self, suffix: str) -> str:
        """Get the full Redis key with prefix.

        Args:
            suffix: Key suffix to append.

        Returns:
            Full Redis key.
        """
        return f"{self.key_prefix}:{suffix}"

    def get_status(self) -> MigrationStatus:
        """Get the current migration status.

        Returns:
            MigrationStatus dict with current progress information.
        """
        default_status: MigrationStatus = {
            "running": False,
            "total_files": 0,
            "processed_files": 0,
            "failed_files": [],
            "started_at": None,
            "completed_at": None,
            "orchestrator_task_id": None,
            "last_updated": None,
        }

        if not self.redis_client:
            return default_status

        try:
            status_key = self._get_key("status")
            status_json = self.redis_client.get(status_key)

            if status_json:
                parsed = json.loads(status_json)
                self._merge_progress(parsed)
                return cast(MigrationStatus, parsed)
            return default_status

        except (RedisError, ValueError) as e:
            logger.error(f"Error getting migration status from Redis: {e}", exc_info=True)
            return default_status

    def _merge_progress(self, parsed: dict) -> None:
        """Overlay the O(1) counters/failed set onto the static status blob."""
        assert self.redis_client is not None
        counters = self.redis_client.hgetall(self._get_key("progress"))
        if not counters:
            return
        text = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in counters.items()
        }
        parsed["processed_files"] = int(text.get("processed_files", 0))
        if text.get("last_updated"):
            parsed["last_updated"] = text["last_updated"]
        members = self.redis_client.smembers(self._get_key("failed"))
        parsed["failed_files"] = sorted(m.decode() if isinstance(m, bytes) else m for m in members)

    def is_running(self) -> bool:
        """Check if a migration is currently running.

        Returns:
            True if a migration is in progress.
        """
        return self.running_state() is not RunState.IDLE

    def running_state(self) -> RunState:
        """Read whether a migration runs: yes, no, or unobservable (Redis down).

        ``get_status`` answers a default "not running" status on an outage, which would let
        a second migration start; this reads Redis directly so the outage stays visible.
        """
        client = self.redis_client
        if not client:
            return RunState.UNKNOWN
        try:
            status_json = client.get(self._get_key("status"))
            if not status_json:
                return RunState.IDLE
            running = bool(json.loads(status_json).get("running", False))
        except (RedisError, ValueError) as e:
            logger.error(f"Could not read migration running state: {e}", exc_info=True)
            return RunState.UNKNOWN
        return RunState.RUNNING if running else RunState.IDLE

    def start_migration(self, total_files: int, task_id: str | None = None) -> bool:
        """Mark a migration as started.

        Args:
            total_files: Total number of files to migrate.
            task_id: Celery task ID of the orchestrator task.

        Returns:
            True if migration was started successfully.
        """
        if not self.redis_client:
            logger.warning("Redis not available, cannot track migration progress")
            return False

        try:
            status: MigrationStatus = {
                "running": True,
                "total_files": total_files,
                "processed_files": 0,
                "failed_files": [],
                "started_at": datetime.now(UTC).isoformat(),
                "completed_at": None,
                "orchestrator_task_id": task_id,
                "last_updated": datetime.now(UTC).isoformat(),
            }

            status_key = self._get_key("status")
            # Set with 24-hour TTL as a safety measure
            self.redis_client.set(status_key, json.dumps(status), ex=MIGRATION_STATUS_TTL_SECONDS)
            # Clear any previous completion flag
            self.redis_client.delete(self._get_key("completed"))
            self.redis_client.delete(self._get_key("progress"), self._get_key("failed"))
            logger.info(f"Started migration tracking: {total_files} files")
            return True

        except (RedisError, ValueError) as e:
            logger.error(f"Error starting migration tracking: {e}", exc_info=True)
            return False

    # Progress counters live OUTSIDE the status JSON blob (loadrun2 R1): the old
    # script GET/cjson.decode/linear-scan/re-encode/SET the whole blob per call
    # (43 ms at 30k failures, 76% of Redis time). Redis data types
    # (https://redis.io/docs/latest/develop/data-types/) give O(1) updates:
    # a hash for counters (HINCRBY) and a SET for deduplicated failed UUIDs,
    # capped so a library with many missing objects cannot grow it unbounded.
    # Lua stays only to keep "no-op unless the migration was started" atomic
    # (https://redis.io/docs/latest/develop/programmability/eval-intro/).
    _INCREMENT_LUA = """
    if redis.call("EXISTS", KEYS[1]) == 0 then return 0 end
    local n = redis.call("HINCRBY", KEYS[2], "processed_files", 1)
    redis.call("HSET", KEYS[2], "last_updated", ARGV[3])
    if ARGV[2] == "1" then
        redis.call("HINCRBY", KEYS[2], "failed_count", 1)
        if ARGV[1] ~= "" and redis.call("SCARD", KEYS[3]) < tonumber(ARGV[5]) then
            redis.call("SADD", KEYS[3], ARGV[1])
            redis.call("EXPIRE", KEYS[3], ARGV[4])
        end
    end
    redis.call("EXPIRE", KEYS[2], ARGV[4])
    return n
    """

    def increment_processed(self, success: bool = True, file_uuid: str | None = None) -> bool:
        """Atomically increment the processed file count using a Redis Lua script.

        This is safe to call concurrently from multiple Celery workers —
        the Lua script executes atomically within Redis.

        Args:
            success: Whether the file was processed successfully.
            file_uuid: UUID of the file (required if success=False).

        Returns:
            True if the increment was successful.
        """
        if not self.redis_client:
            return False

        try:
            status_key = self._get_key("status")
            self.redis_client.eval(
                self._INCREMENT_LUA,
                3,  # number of KEYS
                status_key,
                self._get_key("progress"),
                self._get_key("failed"),
                file_uuid or "",
                "1" if not success else "0",
                datetime.now(UTC).isoformat(),
                MIGRATION_STATUS_TTL_SECONDS,
                MAX_RECORDED_FAILED_FILES,
            )
            return True

        except (RedisError, ValueError) as e:
            logger.error(f"Error incrementing processed count: {e}", exc_info=True)
            return False

    def complete_migration(self, success: bool = True) -> bool:
        """Mark the migration as complete.

        Uses SETNX on a completion flag to ensure only one caller
        executes the completion logic, even with concurrent batch tasks.

        Args:
            success: Whether the migration completed successfully.

        Returns:
            True if this caller was the one that marked completion.
            False if already completed by another caller, or on error.
        """
        if not self.redis_client:
            return False

        try:
            # Atomic guard: only the first caller to set this key wins
            completion_key = self._get_key("completed")
            if not self.redis_client.set(completion_key, "1", nx=True, ex=3600):
                logger.debug("Migration already marked complete by another caller")
                return False

            status = self.get_status()
            status["running"] = False
            status["completed_at"] = datetime.now(UTC).isoformat()
            status["last_updated"] = datetime.now(UTC).isoformat()

            status_key = self._get_key("status")
            # Keep completed status for 1 hour (UI can show "complete" message)
            self.redis_client.set(status_key, json.dumps(status), ex=3600)

            processed = status.get("processed_files", 0)
            total = status.get("total_files", 0)
            failed = len(status.get("failed_files", []))
            logger.info(f"Migration completed: {processed}/{total} processed, {failed} failed")
            return True

        except (RedisError, ValueError) as e:
            logger.error(f"Error completing migration: {e}", exc_info=True)
            return False

    def clear_status(self) -> bool:
        """Clear the migration status from Redis.

        Returns:
            True if the status was cleared successfully.
        """
        if not self.redis_client:
            return False

        try:
            status_key = self._get_key("status")
            self.redis_client.delete(status_key)
            self.redis_client.delete(self._get_key("completed"))
            self.redis_client.delete(self._get_key("progress"), self._get_key("failed"))
            logger.info("Migration status cleared")
            return True

        except RedisError as e:
            logger.error(f"Error clearing migration status: {e}", exc_info=True)
            return False

    def force_stop(self) -> bool:
        """Force stop a running migration by marking it as not running.

        This does not actually stop running Celery tasks, but prevents
        new tasks from being dispatched and marks the migration as stopped.

        Returns:
            True if the status was updated successfully.
        """
        if not self.redis_client:
            return False

        try:
            status = self.get_status()
            if not status.get("running"):
                logger.warning("No migration running to stop")
                return False

            status["running"] = False
            status["completed_at"] = datetime.now(UTC).isoformat()
            status["last_updated"] = datetime.now(UTC).isoformat()

            status_key = self._get_key("status")
            self.redis_client.set(status_key, json.dumps(status), ex=3600)
            logger.warning("Migration force stopped")
            return True

        except (RedisError, ValueError) as e:
            logger.error(f"Error force stopping migration: {e}", exc_info=True)
            return False


# Global instance for easy access
migration_progress = MigrationProgressService()
