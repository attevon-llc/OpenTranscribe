"""
Migration progress tracking service using Redis.

This module provides a Redis-based service for tracking the progress of
long-running migrations like the v3 to v4 speaker embedding migration.
"""

import json
import logging
from datetime import UTC
from datetime import datetime
from typing import NotRequired
from typing import TypedDict
from typing import cast

import redis

from app.core.redis import get_redis

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
                self._redis_client = get_redis()
                self._redis_client.ping()
            except Exception as e:
                logger.error(f"Failed to connect to Redis: {e}")
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
                # lua-cjson cannot tell an empty JSON array from an empty object, so
                # _INCREMENT_LUA's round-trip re-encode of the whole status blob
                # silently turns "failed_files": [] into {} the moment
                # increment_processed() is called with zero failures recorded so
                # far (self-heals the instant a real failure is appended — see
                # test_migration_progress_service.py). Left uncorrected this
                # breaks the MigrationStatus.failed_files: list[str] contract for
                # every migration during the window before its first failure.
                if isinstance(parsed.get("failed_files"), dict):
                    parsed["failed_files"] = []
                return cast(MigrationStatus, parsed)
            return default_status

        except Exception as e:
            logger.error(f"Error getting migration status from Redis: {e}")
            return default_status

    def is_running(self) -> bool:
        """Check if a migration is currently running.

        Returns:
            True if a migration is in progress.
        """
        status = self.get_status()
        return status.get("running", False)

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
            logger.info(f"Started migration tracking: {total_files} files")
            return True

        except Exception as e:
            logger.error(f"Error starting migration tracking: {e}")
            return False

    # Lua script for atomic increment of processed_files + optional failed_files append.
    # Runs entirely within Redis — no race conditions between concurrent callers.
    _INCREMENT_LUA = """
    local key = KEYS[1]
    local file_uuid = ARGV[1]
    local is_failure = ARGV[2] == "1"
    local now = ARGV[3]

    local raw = redis.call("GET", key)
    if not raw then return 0 end

    local status = cjson.decode(raw)
    status["processed_files"] = (status["processed_files"] or 0) + 1
    status["last_updated"] = now

    if is_failure and file_uuid ~= "" then
        local failed = status["failed_files"] or {}
        -- check for duplicate
        local found = false
        for _, v in ipairs(failed) do
            if v == file_uuid then found = true; break end
        end
        if not found then
            table.insert(failed, file_uuid)
            status["failed_files"] = failed
        end
    end

    redis.call("SET", key, cjson.encode(status), "EX", ARGV[4])
    return status["processed_files"]
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
                1,  # number of KEYS
                status_key,
                file_uuid or "",
                "1" if not success else "0",
                datetime.now(UTC).isoformat(),
                MIGRATION_STATUS_TTL_SECONDS,
            )
            return True

        except Exception as e:
            logger.error(f"Error incrementing processed count: {e}")
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

        except Exception as e:
            logger.error(f"Error completing migration: {e}")
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
            logger.info("Migration status cleared")
            return True

        except Exception as e:
            logger.error(f"Error clearing migration status: {e}")
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

        except Exception as e:
            logger.error(f"Error force stopping migration: {e}")
            return False


# Global instance for easy access
migration_progress = MigrationProgressService()
