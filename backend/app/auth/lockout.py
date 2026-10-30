"""
Account lockout management module (NIST AC-7 compliant).

Implements account lockout tracking for security compliance:
- Track failed login attempts per user (by email/username)
- Lock account after configurable threshold
- Progressive lockout with increasing durations
- Admin unlock capability
- Periodic cleanup of expired lockouts

This implementation uses Redis-backed storage for distributed deployments,
with automatic fallback to thread-safe in-memory storage when Redis is unavailable
(``auth/lockout_store.py``). The admin inspection/unlock/reset surface and the
periodic sweep live in ``auth/lockout_admin.py``.
"""

import json
import logging
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TypedDict

from app.auth.degradation import record_degradation
from app.auth.lockout_store import _get_memory_fallback_store
from app.auth.lockout_store import _get_store
from app.auth.utils import mask_identifier
from app.core.process_auth_settings import get_process_auth_settings


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
    (``api/endpoints/admin_accounts.py``) clears the lockout with ``unlock_account(user.email)``
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
        f"Super admin account {mask_identifier(identifier)} had failed login "
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
            f"Successful login for {mask_identifier(record.identifier)}, "
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
        f"Account locked: {mask_identifier(identifier)}, "
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
        f"Failed login attempt for {mask_identifier(identifier)}: "
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
                    f"Login attempt on locked account: {mask_identifier(identifier)}, "
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
                f"Lockout record for {mask_identifier(identifier)} changed concurrently, "
                "recomputing from the winning value"
            )

        logger.warning(
            f"Lockout write for {mask_identifier(identifier)} lost {CAS_MAX_RETRIES} "
            "compare-and-set races; falling back to in-memory tracking"
        )
    except Exception as e:
        logger.warning(f"Redis lockout transaction failed, falling back to in-memory: {e}")

    record_degradation("account_lockout", "local")
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
                f"Login attempt on locked account: {mask_identifier(identifier)}, "
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
                    f"Successful login for {mask_identifier(identifier)}, "
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
                f"Failed login attempt for {mask_identifier(identifier)}: "
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
                    f"Account locked: {mask_identifier(identifier)}, "
                    f"lockout #{record.lockout_count}, "
                    f"duration: {duration_minutes} minutes, "
                    f"until: {unlock_time.isoformat()}"
                )

            store._data[key] = json.dumps(record.to_dict())
            return is_locked, unlock_time
