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

from app.core.auth_settings import get_process_auth_settings
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

        # `is not None`, never truthiness (issue #810). A redis-py client is always truthy,
        # so this worked — but it made the fallback decision depend on a client's __bool__
        # rather than on whether one exists. Any client-like object defining __len__ (a test
        # stub recording calls, a future wrapper) would read as falsy while non-None, and
        # this would silently return the IN-MEMORY store while `_redis_client` was set:
        # per-replica lockout counting, i.e. N times the configured threshold before anyone
        # is locked out, with nothing raising.
        return _redis_client if _redis_client is not None else _in_memory_store


def _get_memory_fallback_store() -> InMemoryLockoutStore:
    """Get the in-memory store, creating it if Redis was healthy until now.

    ``_get_store()`` returns the *Redis* client whenever one exists, so it must never
    be used to supply the fallback: ``_check_and_record_attempt_memory`` reaches into
    ``store._lock`` / ``store._data``, which a ``redis.Redis`` does not have. Passing
    the Redis client there raised ``AttributeError`` out of ``check_and_record_attempt``
    and turned every login into an HTTP 500 for as long as Redis was unhealthy.
    """
    global _in_memory_store

    with _store_lock:
        if _in_memory_store is None:
            _in_memory_store = InMemoryLockoutStore()
        return _in_memory_store


def _record_degradation(control: str, fallback: str) -> None:
    """Count a security control running without its shared state store.

    Imported lazily and never allowed to raise: a broken metrics backend must not be
    able to turn into a failed login. Mirrors ``token_service._record_degradation``.
    """
    try:
        from app.core.metrics import security_state_degraded_total

        security_state_degraded_total.labels(control=control, fallback=fallback).inc()
    except Exception:  # pragma: no cover - metrics must never break auth
        logger.debug("Could not record security degradation metric", exc_info=True)


#: Compare-and-set write for the lockout record.
#:
#: ``WATCH`` cannot guard this operation. ``redis.Redis.watch()`` issues WATCH on a
#: connection from the pool, while ``redis.Redis.pipeline()`` acquires a *different*
#: connection for its MULTI/EXEC — so the previous "atomic" implementation was a plain
#: read-modify-write and two concurrent failed logins could both read
#: ``failed_attempts = 4`` and both write 5, letting an attacker exceed the threshold.
#: A server-side script has no such split: the GET and the SET run in one Redis
#: execution with nothing interleaved. The write applies only when the stored value is
#: still byte-identical to the one the caller read; on conflict the current value comes
#: back so the retry recomputes from fresh state without an extra round trip. A key that
#: TTL'd out mid-flight also reports a conflict, so the retry re-creates it rather than
#: resurrecting a record the server has already dropped.
_CAS_LUA = """
local current = redis.call('GET', KEYS[1])
if ARGV[4] == '1' then
  if current then return {0, current} end
elseif current ~= ARGV[1] then
  return {0, current or ''}
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return {1, ''}
"""

#: How many times a losing writer recomputes before degrading to in-memory tracking.
CAS_MAX_RETRIES = 5

_cas_script = None
_cas_script_client = None


def _get_cas_script(store):
    """Get the registered CAS script for ``store``.

    ``register_script`` hashes locally (no round trip), but caching keeps the SHA
    stable so repeated calls hit ``EVALSHA``. A race here is harmless: both threads
    build equivalent script objects.
    """
    global _cas_script, _cas_script_client

    if _cas_script is None or _cas_script_client is not store:
        _cas_script = store.register_script(_CAS_LUA)
        _cas_script_client = store
    return _cas_script


def _decode_stored(value: str | bytes | None) -> str | None:
    """Normalize a Redis value to ``str``/``None`` (clients may not decode responses)."""
    if value is None or value == "" or value == b"":
        return None
    if isinstance(value, bytes):
        return value.decode()
    return value


def _cas_write(
    script, key: str, expected: str | None, record: LockoutRecord
) -> tuple[bool, str | None]:
    """Persist ``record`` only if ``key`` still holds ``expected``.

    Args:
        script: Registered ``_CAS_LUA`` script
        key: Storage key for the record
        expected: The exact value read at the start of this attempt (None if absent)
        record: The mutated record to write

    Returns:
        Tuple of (written, current_value): ``current_value`` is the value that beat us
        when ``written`` is False.
    """
    ttl = _record_ttl_seconds()
    written, current = script(
        keys=[key],
        args=[
            expected or "",
            json.dumps(record.to_dict()),
            ttl,
            "1" if expected is None else "0",
        ],
    )
    return bool(int(written)), _decode_stored(current)


def _record_ttl_seconds() -> int:
    """How long a lockout record survives in the store.

    The maximum lockout duration plus 24 h, so a record can never expire while
    the account it locks is still locked — an expiring record silently unlocks
    the account. Resolved live because ``account_lockout_max_duration_minutes``
    is admin-editable: pinning the TTL at the value the process started with
    would under-cover a lockout the admin has since made longer.

    Returns:
        Time-to-live in seconds.
    """
    return (get_process_auth_settings().account_lockout_max_duration_minutes + 1440) * 60


def _lockout_key(identifier: str) -> str:
    """Get the Redis/store key for a lockout record."""
    return f"{LOCKOUT_PREFIX}{identifier}"


def normalize_identifier(identifier: str) -> str:
    """Normalize identifier for consistent lookups.

    Public because it is already effectively public API through
    :func:`canonical_identifier`, and the admin locked-accounts endpoints
    (issue #570) need to re-normalize a path-encoded identifier before looking
    it up.

    Args:
        identifier: Email or username

    Returns:
        Lowercase identifier
    """
    return identifier.lower().strip()


#: Private alias kept for every existing in-module call site.
_normalize_identifier = normalize_identifier


def canonical_identifier(submitted: str, account_email: str | None) -> str:
    """Collapse every alias of one account onto a single lockout bucket.

    Lockout was keyed on the **submitted string**, so an account reachable both as
    ``person@example.com`` and as its ``ldap_uid`` had two independent counters and
    an attacker got ``2 x ACCOUNT_LOCKOUT_THRESHOLD`` attempts against it. Keying on
    the resolved account's email collapses those aliases onto one counter, which is
    what NIST AC-7 counts: attempts *against an account*, not against a spelling.

    Why the email and not the UUID: the admin unlock endpoint
    (``api/endpoints/admin.py``) clears the lockout with ``unlock_account(user.email)``
    and the periodic/inspection helpers surface identifiers to operators. Email keeps
    one key space for writers and readers; a UUID key would silently orphan every
    admin unlock.

    Enumeration safety — the two properties that make the fallback safe:

    * **Bucket names do not distinguish existence.** When no account resolves, the
      bucket is the normalized submitted string. For the overwhelmingly common case
      (the login form, where the submitted string *is* the email) the unknown-account
      bucket is byte-identical to the one that account would have used had it
      existed, so the choice of bucket reveals nothing. The alias case can only be
      correlated by an attacker who already knows the account's email address.
    * **No new timing signal.** The caller resolves the account **once,
      unconditionally, on the same code path for a hit and a miss**
      (``login._resolve_lockout_account``) — the very lookup the lockout-exemption
      check already performed on every attempt. This function itself does no I/O.

    Args:
        submitted: The identifier as typed by the caller (email or LDAP uid).
        account_email: Email of the account this attempt resolved to, or ``None``
            when no account matched.

    Returns:
        The normalized lockout key for this attempt.
    """
    return _normalize_identifier(account_email or submitted)


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
    auth = get_process_auth_settings()
    base_duration = auth.account_lockout_duration_minutes
    max_duration = auth.account_lockout_max_duration_minutes

    if not auth.account_lockout_progressive:
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
    ttl = _record_ttl_seconds()
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
    if not get_process_auth_settings().account_lockout_enabled:
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


def _apply_successful_login(
    record: LockoutRecord, locked_until_dt: datetime | None
) -> tuple[bool, None]:
    """Clear failed attempts after successful login.

    Mutates ``record`` only — the caller is responsible for persisting it, so the
    write can be made conditional (see ``_cas_write``).

    Args:
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
    if record.failed_attempts < get_process_auth_settings().account_lockout_threshold:
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


def _apply_failed_login(
    record: LockoutRecord, identifier: str, now: datetime
) -> tuple[bool, datetime | None]:
    """Increment failed attempts and check lockout threshold.

    Mutates ``record`` only — the caller persists it conditionally so two concurrent
    failures cannot both write the same attempt count.

    Args:
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
        f"attempt {record.failed_attempts}/"
        f"{get_process_auth_settings().account_lockout_threshold}"
    )

    return _check_lockout_threshold(record, now, identifier)


def _check_and_record_attempt_redis(
    store, key: str, identifier: str, success: bool, now: datetime
) -> tuple[bool, datetime | None]:
    """
    Atomic check-and-record backed by a compare-and-set Lua script.

    The read-modify-write is made atomic by the ``_CAS_LUA`` script: the record is
    written only if Redis still holds exactly the value this call read. A losing
    writer gets the winner's value back and recomputes from it, so two concurrent
    failed logins produce attempts 4 and 5 rather than both writing 5.

    Falls back to the in-memory store (never to a non-atomic Redis write) if Redis
    is unreachable or contention outlasts the retry budget.
    """
    try:
        script = _get_cas_script(store)
        raw = _decode_stored(store.get(key))

        for _ in range(CAS_MAX_RETRIES):
            record = (
                LockoutRecord.from_dict(json.loads(raw))
                if raw
                else LockoutRecord(identifier=identifier)
            )
            locked_until_dt = record.get_locked_until_datetime()

            # Currently locked: nothing to write, so no CAS is needed.
            if locked_until_dt and now < locked_until_dt:
                logger.warning(
                    f"Login attempt on locked account: {_mask_identifier(identifier)}, "
                    f"locked until {locked_until_dt.isoformat()}"
                )
                return True, locked_until_dt

            # If lockout has expired, reset failed attempts but keep lockout_count
            if locked_until_dt and now >= locked_until_dt:
                _handle_expired_lockout(record, now)

            result: tuple[bool, datetime | None]
            if success:
                result = _apply_successful_login(record, locked_until_dt)
            else:
                result = _apply_failed_login(record, identifier, now)

            written, current = _cas_write(script, key, raw, record)
            if written:
                return result

            raw = current
            logger.debug(
                f"Lockout record for {_mask_identifier(identifier)} changed concurrently, "
                "recomputing from the winning value"
            )

        logger.warning(
            f"Lockout write for {_mask_identifier(identifier)} lost {CAS_MAX_RETRIES} "
            "compare-and-set races; falling back to in-memory tracking"
        )
    except Exception as e:
        logger.warning(f"Redis lockout transaction failed, falling back to in-memory: {e}")

    _record_degradation("account_lockout", "local")
    return _check_and_record_attempt_memory(
        _get_memory_fallback_store(), key, identifier, success, now
    )


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

            threshold = get_process_auth_settings().account_lockout_threshold
            logger.info(
                f"Failed login attempt for {_mask_identifier(identifier)}: "
                f"attempt {record.failed_attempts}/{threshold}"
            )

            # Check if threshold reached
            is_locked = False
            unlock_time = None
            if record.failed_attempts >= threshold:
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
    lockout_enabled = get_process_auth_settings().account_lockout_enabled

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
            "lockout_enabled": lockout_enabled,
        }

    locked_until_dt = record.get_locked_until_datetime()
    is_locked = locked_until_dt is not None and now < locked_until_dt and lockout_enabled

    return {
        "identifier": record.identifier,
        "is_locked": is_locked,
        "failed_attempts": record.failed_attempts,
        "lockout_count": record.lockout_count,
        "locked_until": record.locked_until,
        "first_failed_attempt": record.first_failed_attempt,
        "last_failed_attempt": record.last_failed_attempt,
        "admin_unlocked_at": record.admin_unlocked_at,
        "lockout_enabled": lockout_enabled,
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
    if not get_process_auth_settings().account_lockout_enabled:
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


def current_store_kind() -> str:
    """Which backend :func:`_get_store` is currently serving from.

    Issue #570 (§A.1.3, A.4.1): on the in-memory fallback, a listing built from
    this process's store is *that replica's* view, not the deployment's — a
    known-real degraded state since issue #810's ``is not None`` fix, not a
    hypothetical one. The admin locked-accounts panel surfaces this so an
    operator does not act on a partial view believing it is complete.

    Returns:
        ``"redis"`` when a Redis client is in use, else ``"memory"``.
    """
    store = _get_store()
    return "memory" if isinstance(store, InMemoryLockoutStore) else "redis"


#: Page size used internally while a cursor scan gathers ``limit`` on-topic
#: records (i.e. records for which ``store.get`` had somehow raced away by the
#: time a listing key resolved). Bounded so a mostly-empty keyspace can't spin.
_SCAN_BATCH_MAX_ROUNDS = 20


def iter_lockout_records(cursor: str | None, limit: int) -> tuple[list[LockoutRecord], str | None]:
    """Cursor-scan the lockout keyspace, bounded to ``limit`` records per page.

    Issue #570 (§A.2): the deleted ``get_all_locked_accounts`` used
    ``store.keys(pattern)`` — a whole-keyspace primitive (``redis.Redis.keys()``
    blocks on the entire key space) reachable from a page that could be
    polled. This is a proper cursor-based scan instead, with a matching
    cursor-shaped path for the in-memory fallback (the fallback's cursor is a
    plain integer offset into a stable ``sorted()`` key list — it is
    process-local and small, so sorting on every call is deliberately fine).

    Args:
        cursor: Opaque cursor from a previous call, or ``None``/``"0"`` to start.
        limit: Maximum number of records to return in this page.

    Returns:
        ``(records, next_cursor)`` — ``next_cursor`` is ``None`` when the scan
        has exhausted the keyspace.
    """
    store = _get_store()
    records: list[LockoutRecord] = []

    if isinstance(store, InMemoryLockoutStore):
        with store._lock:
            all_keys = sorted(k for k in store._data if k.startswith(LOCKOUT_PREFIX))
            start = int(cursor) if cursor else 0
            page_keys = all_keys[start : start + limit]
            for key in page_keys:
                data = store._data.get(key)
                if data:
                    records.append(LockoutRecord.from_dict(json.loads(data)))
            next_start = start + len(page_keys)
            next_cursor = str(next_start) if next_start < len(all_keys) else None
        return records, next_cursor

    # Redis: native SCAN. `count` is a hint, not a hard cap, so loop until this
    # page has `limit` records or the cursor comes back to 0 (scan exhausted) —
    # never a single unbounded call.
    redis_cursor = int(cursor) if cursor else 0
    found_keys: list[str] = []
    rounds = 0
    while len(found_keys) < limit and rounds < _SCAN_BATCH_MAX_ROUNDS:
        redis_cursor, batch = store.scan(
            cursor=redis_cursor, match=f"{LOCKOUT_PREFIX}*", count=limit
        )
        found_keys.extend(_decode_stored(k) or "" for k in batch)
        rounds += 1
        if redis_cursor == 0:
            break

    page_keys = found_keys[:limit]
    if page_keys:
        values = store.mget(page_keys)
        for value in values:
            decoded = _decode_stored(value)
            if decoded:
                records.append(LockoutRecord.from_dict(json.loads(decoded)))

    next_cursor = str(redis_cursor) if redis_cursor != 0 else None
    return records, next_cursor


def list_locked_accounts(
    cursor: str | None = None,
    limit: int = 100,
    *,
    include_unlocked: bool = False,
) -> tuple[list[LockoutInfo], str | None, bool]:
    """List currently-locked (or, with ``include_unlocked``, all recent) records.

    Issue #570 (§A.4.1). Reads each scanned record once and builds its
    ``LockoutInfo`` from the record already in hand — the deleted
    ``get_all_locked_accounts`` read every record twice (parse, then a second
    ``store.get`` inside ``get_lockout_info``).

    Args:
        cursor: Opaque pagination cursor from a previous call.
        limit: Page size passed through to the underlying scan.
        include_unlocked: When False (default), only records whose
            ``locked_until`` is still in the future are returned. The record
            set is "recent failed-login activity", which is broader than
            "locked" — this flag is for an operator diagnosing a near-threshold
            account, not the default queue view.

    Returns:
        ``(accounts, next_cursor, lockout_enabled)``.
    """
    lockout_enabled = get_process_auth_settings().account_lockout_enabled
    now = datetime.now(UTC)

    scanned, next_cursor = iter_lockout_records(cursor, limit)

    accounts: list[LockoutInfo] = []
    for record in scanned:
        locked_until_dt = record.get_locked_until_datetime()
        is_locked = locked_until_dt is not None and now < locked_until_dt and lockout_enabled
        if not include_unlocked and not is_locked:
            continue
        accounts.append(
            {
                "identifier": record.identifier,
                "is_locked": is_locked,
                "failed_attempts": record.failed_attempts,
                "lockout_count": record.lockout_count,
                "locked_until": record.locked_until,
                "first_failed_attempt": record.first_failed_attempt,
                "last_failed_attempt": record.last_failed_attempt,
                "admin_unlocked_at": record.admin_unlocked_at,
                "lockout_enabled": lockout_enabled,
            }
        )

    return accounts, next_cursor, lockout_enabled


class LockoutResetOutcome(TypedDict):
    """Result of :func:`reset_lockout_state`."""

    found: bool
    written: bool
    previous_lockout_count: int
    was_locked: bool
    unlocked: bool


def reset_lockout_state(identifier: str, *, clear_lock: bool = False) -> LockoutResetOutcome:
    """Reset an account's progressive lockout counter, through the CAS path.

    Issue #570 (§A.2, §A.4.2): the deleted ``reset_lockout_count`` was a bare
    ``_get_record``/``_save_record`` read-modify-write — the exact pre-CAS shape
    ``check_and_record_attempt`` was rebuilt around ``_CAS_LUA`` to close (two
    concurrent writers could both read and clobber each other). This goes
    through :func:`_cas_write` so a reset participates in the same
    compare-and-set discipline, retried up to :data:`CAS_MAX_RETRIES` times
    before reporting ``written=False`` (the caller maps that to **409**, never a
    silent partial write).

    It does not create a record for an unknown identifier — the caller maps
    ``found=False`` to **404**.

    J-A1: always clears ``lockout_count`` and ``failed_attempts``. ``locked_until``
    is left intact unless ``clear_lock=True`` — keeping
    ``POST /admin/users/{uuid}/unlock`` as the single "unlock now" verb. Any
    write re-stamps the record's TTL (``_save_record``/``_cas_write`` always do);
    that is deliberate on the counting path and applies here too.

    Args:
        identifier: Email or username (re-normalized here).
        clear_lock: Also clear ``locked_until`` — an explicit opt-in, not the
            default.

    Returns:
        A :class:`LockoutResetOutcome`. ``found=False`` when no record exists
        (nothing is written). ``written=False`` when the CAS budget was
        exhausted (nothing is written; retry).
    """
    identifier = normalize_identifier(identifier)
    store = _get_store()

    if isinstance(store, InMemoryLockoutStore):
        record = _get_record(identifier)
        if record is None:
            return {
                "found": False,
                "written": False,
                "previous_lockout_count": 0,
                "was_locked": False,
                "unlocked": False,
            }
        now = datetime.now(UTC)
        locked_until_dt = record.get_locked_until_datetime()
        was_locked = locked_until_dt is not None and now < locked_until_dt
        previous_count = record.lockout_count
        record.lockout_count = 0
        record.failed_attempts = 0
        record.admin_unlocked_at = now.isoformat()
        if clear_lock:
            record.set_locked_until(None)
        _save_record(record)
        return {
            "found": True,
            "written": True,
            "previous_lockout_count": previous_count,
            "was_locked": was_locked,
            "unlocked": clear_lock and was_locked,
        }

    key = _lockout_key(identifier)
    script = _get_cas_script(store)
    now = datetime.now(UTC)

    for _ in range(CAS_MAX_RETRIES):
        raw = _decode_stored(store.get(key))
        if raw is None:
            return {
                "found": False,
                "written": False,
                "previous_lockout_count": 0,
                "was_locked": False,
                "unlocked": False,
            }
        record = LockoutRecord.from_dict(json.loads(raw))
        locked_until_dt = record.get_locked_until_datetime()
        was_locked = locked_until_dt is not None and now < locked_until_dt
        previous_count = record.lockout_count
        record.lockout_count = 0
        record.failed_attempts = 0
        record.admin_unlocked_at = now.isoformat()
        if clear_lock:
            record.set_locked_until(None)

        written, _current = _cas_write(script, key, raw, record)
        if written:
            return {
                "found": True,
                "written": True,
                "previous_lockout_count": previous_count,
                "was_locked": was_locked,
                "unlocked": clear_lock and was_locked,
            }
        # Lost the race — recompute from the value that beat us, next iteration.

    logger.warning(
        f"Lockout reset for {_mask_identifier(identifier)} lost {CAS_MAX_RETRIES} "
        "compare-and-set races in a row"
    )
    return {
        "found": True,
        "written": False,
        "previous_lockout_count": 0,
        "was_locked": False,
        "unlocked": False,
    }
