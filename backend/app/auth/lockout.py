"""
Account lockout management module (NIST AC-7 compliant).

Implements account lockout tracking for security compliance:
- Track failed login attempts per user (by email/username)
- Lock account after configurable threshold
- Progressive lockout with increasing durations
- Admin unlock capability
- Periodic cleanup of expired lockouts

This implementation uses Redis-backed storage for distributed deployments,
with automatic fallback to thread-safe in-memory storage when Redis is unavailable.
"""

import contextlib
import json
import logging
import threading
import time
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TypedDict

from app.core.config import settings


class LockoutInfo(TypedDict):
    """Type definition for lockout information returned by get_lockout_info."""

    identifier: str
    is_locked: bool
    failed_attempts: int
    lockout_count: int
    locked_until: str | None  # ISO format datetime string
    first_failed_attempt: str | None  # ISO format datetime string
    last_failed_attempt: str | None  # ISO format datetime string
    admin_unlocked_at: str | None  # ISO format datetime string
    lockout_enabled: bool


logger = logging.getLogger(__name__)


# Redis key prefix for lockout records
LOCKOUT_PREFIX = "lockout:"


# Progressive lockout duration multipliers
# 1st lockout: base duration, 2nd: 2x, 3rd: 4x, 4th+: max duration
PROGRESSIVE_MULTIPLIERS = [1, 2, 4]


@dataclass
class LockoutRecord:
    """Record tracking failed login attempts and lockout status for an account."""

    identifier: str
    failed_attempts: int = 0
    lockout_count: int = 0  # Number of times account has been locked out
    locked_until: str | None = None  # ISO format datetime string
    first_failed_attempt: str | None = None  # ISO format datetime string
    last_failed_attempt: str | None = None  # ISO format datetime string
    # Track when admin manually unlocked (for audit purposes)
    admin_unlocked_at: str | None = None  # ISO format datetime string

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LockoutRecord":
        """Create from dictionary (JSON deserialization)."""
        return cls(**data)

    def get_locked_until_datetime(self) -> datetime | None:
        """Get locked_until as datetime object."""
        if self.locked_until:
            return datetime.fromisoformat(self.locked_until)
        return None

    def set_locked_until(self, dt: datetime | None) -> None:
        """Set locked_until from datetime object."""
        self.locked_until = dt.isoformat() if dt else None


def _get_redis_client():
    """
    Get a Redis client connection for lockout storage.

    Returns:
        Optional[redis.Redis]: Redis client or None if unavailable.
    """
    try:
        import redis

        client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        # Test connection
        client.ping()
        return client
    except ImportError:
        logger.warning("Redis package not available for lockout storage, using in-memory")
        return None
    except Exception as e:
        logger.warning(f"Redis connection failed for lockout storage: {e}")
        return None


class InMemoryLockoutStore:
    """
    Thread-safe in-memory storage for lockout records.

    Warning:
        This store does not persist across restarts and does not work
        in distributed deployments. Use Redis for production.
    """

    def __init__(self):
        self._data: dict[str, str] = {}  # key -> JSON string
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        """Get a value."""
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Set a value (ex parameter ignored for in-memory)."""
        with self._lock:
            self._data[key] = value

    def delete(self, key: str) -> int:
        """Delete a key, returning 1 if deleted, 0 if not found."""
        with self._lock:
            if key in self._data:
                del self._data[key]
                return 1
            return 0

    def keys(self, pattern: str) -> list[str]:
        """Get keys matching a pattern (simple prefix matching)."""
        prefix = pattern.rstrip("*")
        with self._lock:
            return [k for k in self._data if k.startswith(prefix)]


# Singleton stores
_redis_client = None
_in_memory_store: InMemoryLockoutStore | None = None
_store_initialized = False
_store_lock = threading.Lock()
_last_redis_probe: float = 0.0

#: How long to stay on the in-memory fallback before re-probing Redis.
REDIS_REPROBE_SECONDS = 30.0


def _get_store():
    """Get the storage backend (Redis, or the in-memory fallback while it is down).

    Re-probes Redis instead of latching (issue #284 A1.16). The fallback used to be
    permanent for the process lifetime: one transient Redis failure at first use and
    that replica counted failed logins **in its own memory forever**, even after Redis
    came back. Behind a load balancer that is an auth-throttling bypass — each replica
    tracks its own counter, so an attacker gets N x the allowed attempts, and lockouts
    stop being visible across replicas at all.

    Probing is rate-limited to one attempt per ``REDIS_REPROBE_SECONDS`` so a hard Redis
    outage doesn't add a connection attempt to every single login.
    """
    global _redis_client, _in_memory_store, _store_initialized, _last_redis_probe

    with _store_lock:
        if not _store_initialized:
            _redis_client = _get_redis_client()
            if _redis_client is None:
                logger.warning(
                    "Using in-memory lockout storage. "
                    "Lockout state will not persist across restarts and will not work in distributed deployments."
                )
                _in_memory_store = InMemoryLockoutStore()
            _store_initialized = True
            _last_redis_probe = time.monotonic()
        elif _redis_client is None:
            # On the fallback — retry Redis, but not on every call.
            now = time.monotonic()
            if now - _last_redis_probe >= REDIS_REPROBE_SECONDS:
                _last_redis_probe = now
                recovered = _get_redis_client()
                if recovered is not None:
                    logger.info("Redis recovered — resuming distributed lockout storage")
                    _redis_client = recovered

        return _redis_client if _redis_client else _in_memory_store


def _lockout_key(identifier: str) -> str:
    """Get the Redis/store key for a lockout record."""
    return f"{LOCKOUT_PREFIX}{identifier}"


def _normalize_identifier(identifier: str) -> str:
    """Normalize identifier for consistent lookups.

    Args:
        identifier: Email or username

    Returns:
        Lowercase identifier
    """
    return identifier.lower().strip()


def _mask_identifier(identifier: str) -> str:
    """Mask identifier for safe logging to prevent sensitive data exposure.

    Delegates to shared utility. Kept as module-private alias for
    backward compatibility with existing callers.
    """
    from app.auth.utils import mask_identifier

    return mask_identifier(identifier)


def _get_lockout_duration_minutes(lockout_count: int) -> int:
    """Calculate lockout duration based on lockout count and settings.

    Implements progressive lockout if enabled:
    - 1st lockout: ACCOUNT_LOCKOUT_DURATION_MINUTES (default 15)
    - 2nd lockout: 2x base (30 minutes)
    - 3rd lockout: 4x base (60 minutes)
    - 4th+ lockout: ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES (default 1440 = 24 hours)

    Args:
        lockout_count: Number of times account has been locked out (0-based before increment)

    Returns:
        Lockout duration in minutes
    """
    base_duration = settings.ACCOUNT_LOCKOUT_DURATION_MINUTES
    max_duration = settings.ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES

    if not settings.ACCOUNT_LOCKOUT_PROGRESSIVE:
        return base_duration

    # lockout_count is the count before this lockout, so 0 = first lockout
    if lockout_count < len(PROGRESSIVE_MULTIPLIERS):
        duration = base_duration * PROGRESSIVE_MULTIPLIERS[lockout_count]
    else:
        # 4th+ lockout: use max duration
        duration = max_duration

    return min(duration, max_duration)


def _get_record(identifier: str) -> LockoutRecord | None:
    """Get lockout record from storage.

    Args:
        identifier: Normalized identifier

    Returns:
        LockoutRecord or None if not found
    """
    store = _get_store()
    key = _lockout_key(identifier)
    data = store.get(key)
    if data:
        return LockoutRecord.from_dict(json.loads(data))
    return None


def _save_record(record: LockoutRecord) -> None:
    """Save lockout record to storage.

    Args:
        record: LockoutRecord to save
    """
    store = _get_store()
    key = _lockout_key(record.identifier)
    # Set TTL to max lockout duration + 24 hours for cleanup
    ttl = (settings.ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES + 1440) * 60
    store.set(key, json.dumps(record.to_dict()), ex=ttl)


def _record_attempt_audit_only(identifier: str) -> None:
    """Record a failed login attempt for audit purposes without triggering lockout.

    Used for super admin accounts that are exempt from lockout to preserve
    emergency access. All attempts are still logged for NIST AC-7 compliance.

    Args:
        identifier: Email or username of the account
    """
    identifier = _normalize_identifier(identifier)
    now = datetime.now(UTC)

    record = _get_record(identifier)
    if not record:
        record = LockoutRecord(identifier=identifier)

    record.failed_attempts += 1
    record.last_failed_attempt = now.isoformat()
    if record.first_failed_attempt is None:
        record.first_failed_attempt = now.isoformat()

    # Save record WITHOUT setting locked_until
    _save_record(record)

    logger.warning(
        f"Super admin account {_mask_identifier(identifier)} had failed login "
        f"attempt #{record.failed_attempts} — exempt from lockout"
    )


def check_and_record_attempt(
    identifier: str, success: bool, exempt_from_lockout: bool = False
) -> tuple[bool, datetime | None]:
    """
    Atomically check lockout status and record login attempt result.

    This function provides atomic check-and-record behavior to prevent race conditions
    between checking lockout status and recording failed attempts.

    Args:
        identifier: Email or username of the account
        success: True if login was successful, False if failed
        exempt_from_lockout: If True, record attempts for audit but never lock the account.
            Used for super admin accounts to preserve emergency access (NIST AC-7 compliant).

    Returns:
        Tuple of (is_locked, unlock_time):
        - is_locked: True if account is/becomes locked, False otherwise
        - unlock_time: When the lockout expires (None if not locked)
    """
    if not settings.ACCOUNT_LOCKOUT_ENABLED:
        return False, None

    if exempt_from_lockout:
        if not success:
            _record_attempt_audit_only(identifier)
        return False, None

    identifier = _normalize_identifier(identifier)
    now = datetime.now(UTC)
    store = _get_store()
    key = _lockout_key(identifier)

    # Use Redis WATCH/MULTI for atomic operations if available
    if hasattr(store, "pipeline"):
        return _check_and_record_attempt_redis(store, key, identifier, success, now)
    else:
        return _check_and_record_attempt_memory(store, key, identifier, success, now)


def _handle_expired_lockout(record: LockoutRecord, now: datetime) -> None:
    """Reset record if lockout has expired.

    Resets failed attempts but preserves lockout_count for progressive tracking.

    Args:
        record: The lockout record to update
        now: Current UTC datetime
    """
    record.failed_attempts = 0
    record.set_locked_until(None)
    record.first_failed_attempt = now.isoformat()


def _handle_successful_login(
    store, key: str, record: LockoutRecord, locked_until_dt: datetime | None
) -> tuple[bool, None]:
    """Clear failed attempts after successful login.

    Args:
        store: Redis store with pipeline support
        key: Storage key for the record
        record: The lockout record to update
        locked_until_dt: Previous lockout datetime (for logging)

    Returns:
        Tuple of (False, None) indicating not locked
    """
    if record.failed_attempts > 0 or locked_until_dt:
        logger.info(
            f"Successful login for {_mask_identifier(record.identifier)}, "
            f"clearing {record.failed_attempts} failed attempts"
        )
    record.failed_attempts = 0
    record.set_locked_until(None)
    record.first_failed_attempt = None
    record.last_failed_attempt = None
    record.admin_unlocked_at = None

    ttl = (settings.ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES + 1440) * 60
    pipe = store.pipeline(True)  # Transaction mode
    try:
        pipe.set(key, json.dumps(record.to_dict()), ex=ttl)
        pipe.execute()
    except Exception:
        pipe.reset()
        raise
    return False, None


def _check_lockout_threshold(
    record: LockoutRecord, now: datetime, identifier: str
) -> tuple[bool, datetime | None]:
    """Check if lockout threshold is reached and apply lockout if needed.

    Args:
        record: The lockout record (will be modified if threshold reached)
        now: Current UTC datetime
        identifier: User identifier for logging

    Returns:
        Tuple of (is_locked, unlock_time)
    """
    if record.failed_attempts < settings.ACCOUNT_LOCKOUT_THRESHOLD:
        return False, None

    duration_minutes = _get_lockout_duration_minutes(record.lockout_count)
    unlock_time = now + timedelta(minutes=duration_minutes)
    record.set_locked_until(unlock_time)
    record.lockout_count += 1

    logger.warning(
        f"Account locked: {_mask_identifier(identifier)}, "
        f"lockout #{record.lockout_count}, "
        f"duration: {duration_minutes} minutes, "
        f"until: {unlock_time.isoformat()}"
    )
    return True, unlock_time


def _handle_failed_login(
    store, key: str, record: LockoutRecord, identifier: str, now: datetime
) -> tuple[bool, datetime | None]:
    """Increment failed attempts and check lockout threshold.

    Args:
        store: Redis store with pipeline support
        key: Storage key for the record
        record: The lockout record to update
        identifier: User identifier for logging
        now: Current UTC datetime

    Returns:
        Tuple of (is_locked, unlock_time)
    """
    record.failed_attempts += 1
    record.last_failed_attempt = now.isoformat()
    record.admin_unlocked_at = None

    if record.first_failed_attempt is None:
        record.first_failed_attempt = now.isoformat()

    logger.info(
        f"Failed login attempt for {_mask_identifier(identifier)}: "
        f"attempt {record.failed_attempts}/{settings.ACCOUNT_LOCKOUT_THRESHOLD}"
    )

    is_locked, unlock_time = _check_lockout_threshold(record, now, identifier)

    ttl = (settings.ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES + 1440) * 60
    pipe = store.pipeline(True)  # Transaction mode
    try:
        pipe.set(key, json.dumps(record.to_dict()), ex=ttl)
        pipe.execute()
    except Exception:
        pipe.reset()
        raise
    return is_locked, unlock_time


def _check_and_record_attempt_redis(
    store, key: str, identifier: str, success: bool, now: datetime
) -> tuple[bool, datetime | None]:
    """
    Atomic check-and-record using Redis transactions.

    Uses optimistic locking with WATCH to ensure atomicity.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            store.watch(key)

            data = store.get(key)
            record = (
                LockoutRecord.from_dict(json.loads(data))
                if data
                else LockoutRecord(identifier=identifier)
            )
            locked_until_dt = record.get_locked_until_datetime()

            # Check if currently locked
            if locked_until_dt and now < locked_until_dt:
                store.unwatch()
                logger.warning(
                    f"Login attempt on locked account: {_mask_identifier(identifier)}, "
                    f"locked until {locked_until_dt.isoformat()}"
                )
                return True, locked_until_dt

            # If lockout has expired, reset failed attempts but keep lockout_count
            if locked_until_dt and now >= locked_until_dt:
                _handle_expired_lockout(record, now)

            if success:
                return _handle_successful_login(store, key, record, locked_until_dt)

            return _handle_failed_login(store, key, record, identifier, now)

        except Exception as e:
            if "WATCH" in str(type(e).__name__).upper() or "watch" in str(e).lower():
                logger.debug(
                    f"Lockout record modified by another client, retrying (attempt {attempt + 1})"
                )
                continue
            logger.warning(f"Redis transaction failed, using non-atomic fallback: {e}")
            with contextlib.suppress(Exception):
                store.unwatch()
            break

    # Fallback to non-atomic behavior after retries exhausted
    return _check_and_record_attempt_memory(_get_store(), key, identifier, success, now)


def _check_and_record_attempt_memory(
    store, key: str, identifier: str, success: bool, now: datetime
) -> tuple[bool, datetime | None]:
    """
    Check-and-record for in-memory store using thread locking.

    The InMemoryLockoutStore already uses internal locking, but we need
    to lock around the entire read-modify-write operation.
    """
    # For in-memory store, use its internal lock for the entire operation
    with store._lock:
        # Get current record (bypass the store's get to avoid double-locking)
        data = store._data.get(key)
        if data:
            record = LockoutRecord.from_dict(json.loads(data))
        else:
            record = LockoutRecord(identifier=identifier)

        locked_until_dt = record.get_locked_until_datetime()

        # Check if currently locked
        if locked_until_dt and now < locked_until_dt:
            logger.warning(
                f"Login attempt on locked account: {_mask_identifier(identifier)}, "
                f"locked until {locked_until_dt.isoformat()}"
            )
            return True, locked_until_dt

        # If lockout has expired, reset failed attempts but keep lockout_count
        if locked_until_dt and now >= locked_until_dt:
            record.failed_attempts = 0
            record.set_locked_until(None)
            record.first_failed_attempt = now.isoformat()

        if success:
            # Successful login - clear failed attempts
            if record.failed_attempts > 0 or locked_until_dt:
                logger.info(
                    f"Successful login for {_mask_identifier(identifier)}, "
                    f"clearing {record.failed_attempts} failed attempts"
                )
            record.failed_attempts = 0
            record.set_locked_until(None)
            record.first_failed_attempt = None
            record.last_failed_attempt = None
            record.admin_unlocked_at = None

            store._data[key] = json.dumps(record.to_dict())
            return False, None
        else:
            # Failed login - increment attempts
            record.failed_attempts += 1
            record.last_failed_attempt = now.isoformat()
            record.admin_unlocked_at = None

            if record.first_failed_attempt is None:
                record.first_failed_attempt = now.isoformat()

            logger.info(
                f"Failed login attempt for {_mask_identifier(identifier)}: "
                f"attempt {record.failed_attempts}/{settings.ACCOUNT_LOCKOUT_THRESHOLD}"
            )

            # Check if threshold reached
            is_locked = False
            unlock_time = None
            if record.failed_attempts >= settings.ACCOUNT_LOCKOUT_THRESHOLD:
                duration_minutes = _get_lockout_duration_minutes(record.lockout_count)
                unlock_time = now + timedelta(minutes=duration_minutes)
                record.set_locked_until(unlock_time)
                record.lockout_count += 1
                is_locked = True

                logger.warning(
                    f"Account locked: {_mask_identifier(identifier)}, "
                    f"lockout #{record.lockout_count}, "
                    f"duration: {duration_minutes} minutes, "
                    f"until: {unlock_time.isoformat()}"
                )

            store._data[key] = json.dumps(record.to_dict())
            return is_locked, unlock_time


# Legacy functions for backward compatibility
def record_failed_attempt(identifier: str) -> None:
    """Record a failed login attempt for the given identifier.

    DEPRECATED: Use check_and_record_attempt() for atomic operations.

    If the threshold is reached, the account will be locked out.
    Lockout duration increases progressively if ACCOUNT_LOCKOUT_PROGRESSIVE is True.

    Args:
        identifier: Email or username of the account
    """
    check_and_record_attempt(identifier, success=False)


def record_successful_login(identifier: str) -> None:
    """Record a successful login, clearing failed attempts.

    DEPRECATED: Use check_and_record_attempt() for atomic operations.

    This resets the failed attempt counter but preserves the lockout_count
    for progressive lockout tracking.

    Args:
        identifier: Email or username of the account
    """
    check_and_record_attempt(identifier, success=True)


def is_account_locked(identifier: str) -> tuple[bool, datetime | None]:
    """Check if an account is currently locked out.

    Note: For atomic check-and-record, use check_and_record_attempt() instead.

    Args:
        identifier: Email or username of the account

    Returns:
        Tuple of (is_locked, unlock_time):
        - is_locked: True if account is locked, False otherwise
        - unlock_time: When the lockout expires (None if not locked)
    """
    if not settings.ACCOUNT_LOCKOUT_ENABLED:
        return False, None

    identifier = _normalize_identifier(identifier)
    now = datetime.now(UTC)

    record = _get_record(identifier)
    if not record:
        return False, None

    locked_until_dt = record.get_locked_until_datetime()
    if locked_until_dt is None:
        return False, None

    if now >= locked_until_dt:
        # Lockout has expired
        return False, None

    return True, locked_until_dt


def get_lockout_info(identifier: str) -> LockoutInfo:
    """Get detailed lockout information for an account.

    Useful for admin API to view account status.

    Args:
        identifier: Email or username of the account

    Returns:
        Dictionary with lockout information:
        - identifier: The normalized identifier
        - is_locked: Whether account is currently locked
        - failed_attempts: Current failed attempt count
        - lockout_count: Number of times account has been locked
        - locked_until: When lockout expires (ISO format or None)
        - first_failed_attempt: First failed attempt time (ISO format or None)
        - last_failed_attempt: Last failed attempt time (ISO format or None)
        - admin_unlocked_at: When admin unlocked account (ISO format or None)
        - lockout_enabled: Whether lockout is enabled in settings
    """
    identifier = _normalize_identifier(identifier)
    now = datetime.now(UTC)

    record = _get_record(identifier)
    if not record:
        return {
            "identifier": identifier,
            "is_locked": False,
            "failed_attempts": 0,
            "lockout_count": 0,
            "locked_until": None,
            "first_failed_attempt": None,
            "last_failed_attempt": None,
            "admin_unlocked_at": None,
            "lockout_enabled": settings.ACCOUNT_LOCKOUT_ENABLED,
        }

    locked_until_dt = record.get_locked_until_datetime()
    is_locked = (
        locked_until_dt is not None and now < locked_until_dt and settings.ACCOUNT_LOCKOUT_ENABLED
    )

    return {
        "identifier": record.identifier,
        "is_locked": is_locked,
        "failed_attempts": record.failed_attempts,
        "lockout_count": record.lockout_count,
        "locked_until": record.locked_until,
        "first_failed_attempt": record.first_failed_attempt,
        "last_failed_attempt": record.last_failed_attempt,
        "admin_unlocked_at": record.admin_unlocked_at,
        "lockout_enabled": settings.ACCOUNT_LOCKOUT_ENABLED,
    }


def unlock_account(identifier: str) -> bool:
    """Manually unlock an account (admin function).

    Resets the lockout but preserves the lockout_count for audit purposes.

    Args:
        identifier: Email or username of the account

    Returns:
        True if account was unlocked, False if account was not locked
    """
    identifier = _normalize_identifier(identifier)
    now = datetime.now(UTC)

    record = _get_record(identifier)
    if not record:
        logger.info(f"Admin unlock requested for unknown account: {_mask_identifier(identifier)}")
        return False

    locked_until_dt = record.get_locked_until_datetime()
    if locked_until_dt is None or now >= locked_until_dt:
        logger.info(
            f"Admin unlock requested for non-locked account: {_mask_identifier(identifier)}"
        )
        return False

    logger.warning(
        f"Admin unlocking account: {_mask_identifier(identifier)}, "
        f"was locked until {locked_until_dt.isoformat()}, "
        f"lockout #{record.lockout_count}"
    )

    record.set_locked_until(None)
    record.failed_attempts = 0
    record.admin_unlocked_at = now.isoformat()
    # Note: lockout_count is preserved for audit trail

    _save_record(record)
    return True


def cleanup_expired_lockouts() -> int:
    """Clean up expired lockout records to free memory.

    For Redis backend, records have TTL and are cleaned automatically.
    For in-memory backend, removes records where:
    - Lockout has expired AND no failed attempts in the last 24 hours

    This should be called periodically (e.g., via Celery beat task).

    Returns:
        Number of records cleaned up
    """
    if not settings.ACCOUNT_LOCKOUT_ENABLED:
        return 0

    store = _get_store()

    # Redis handles TTL-based cleanup automatically
    if hasattr(store, "pipeline"):
        return 0

    # For in-memory store, manually clean up
    now = datetime.now(UTC)
    cleanup_threshold = now - timedelta(hours=24)
    cleaned = 0

    with store._lock:
        keys_to_remove = []

        for key in list(store._data.keys()):
            if not key.startswith(LOCKOUT_PREFIX):
                continue

            data = store._data.get(key)
            if not data:
                continue

            record = LockoutRecord.from_dict(json.loads(data))
            locked_until_dt = record.get_locked_until_datetime()

            # Only cleanup if:
            # 1. Not currently locked
            # 2. Last activity was more than 24 hours ago
            is_expired = locked_until_dt is None or now >= locked_until_dt
            last_activity = (
                datetime.fromisoformat(record.last_failed_attempt)
                if record.last_failed_attempt
                else (
                    datetime.fromisoformat(record.first_failed_attempt)
                    if record.first_failed_attempt
                    else None
                )
            )

            if is_expired and (last_activity is None or last_activity < cleanup_threshold):
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del store._data[key]
            cleaned += 1

    if cleaned > 0:
        logger.info(f"Cleaned up {cleaned} expired lockout records")

    return cleaned


def get_all_locked_accounts() -> list[LockoutInfo]:
    """Get all currently locked accounts.

    Useful for admin dashboard to see all locked accounts.

    Returns:
        List of lockout info dictionaries for all locked accounts
    """
    if not settings.ACCOUNT_LOCKOUT_ENABLED:
        return []

    store = _get_store()
    now = datetime.now(UTC)
    locked_accounts = []

    # Get all lockout keys
    keys = store.keys(f"{LOCKOUT_PREFIX}*")

    for key in keys:
        data = store.get(key)
        if not data:
            continue

        record = LockoutRecord.from_dict(json.loads(data))
        locked_until_dt = record.get_locked_until_datetime()

        if locked_until_dt and now < locked_until_dt:
            locked_accounts.append(get_lockout_info(record.identifier))

    return locked_accounts


def reset_lockout_count(identifier: str) -> bool:
    """Reset the lockout count for an account (admin function).

    This resets the progressive lockout counter, so the next lockout
    will use the base duration again.

    Args:
        identifier: Email or username of the account

    Returns:
        True if lockout count was reset, False if account not found
    """
    identifier = _normalize_identifier(identifier)

    record = _get_record(identifier)
    if not record:
        return False

    old_count = record.lockout_count
    record.lockout_count = 0

    _save_record(record)

    logger.info(f"Admin reset lockout count for {_mask_identifier(identifier)}: {old_count} -> 0")
    return True
