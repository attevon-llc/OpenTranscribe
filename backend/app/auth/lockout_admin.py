"""Admin and maintenance surface over the account lockout records (NIST AC-7).

Inspection (:func:`get_lockout_info`, :func:`list_locked_accounts`,
:func:`iter_lockout_records`, :func:`current_store_kind`), the admin unlock and counter reset
(:func:`unlock_account`, :func:`reset_lockout_state`, which goes through the same
compare-and-set write as the counting path), and the periodic in-memory sweep
(:func:`cleanup_expired_lockouts`). The counting policy lives in ``auth/lockout.py``; the
store in ``auth/lockout_store.py``.
"""

import json
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TypedDict

from app.auth.lockout import CAS_MAX_RETRIES
from app.auth.lockout import LOCKOUT_PREFIX
from app.auth.lockout import LockoutInfo
from app.auth.lockout import LockoutRecord
from app.auth.lockout import _cas_write
from app.auth.lockout import _decode_stored
from app.auth.lockout import _get_cas_script
from app.auth.lockout import _get_record
from app.auth.lockout import _lockout_key
from app.auth.lockout import _normalize_identifier
from app.auth.lockout import _save_record
from app.auth.lockout import normalize_identifier
from app.auth.lockout_store import InMemoryLockoutStore
from app.auth.lockout_store import _get_store
from app.auth.utils import mask_identifier
from app.core.process_auth_settings import get_process_auth_settings

logger = logging.getLogger(__name__)


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
        logger.info(f"Admin unlock requested for unknown account: {mask_identifier(identifier)}")
        return False

    locked_until_dt = record.get_locked_until_datetime()
    if locked_until_dt is None or now >= locked_until_dt:
        logger.info(f"Admin unlock requested for non-locked account: {mask_identifier(identifier)}")
        return False

    logger.warning(
        f"Admin unlocking account: {mask_identifier(identifier)}, "
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
        f"Lockout reset for {mask_identifier(identifier)} lost {CAS_MAX_RETRIES} "
        "compare-and-set races in a row"
    )
    return {
        "found": True,
        "written": False,
        "previous_lockout_count": 0,
        "was_locked": False,
        "unlocked": False,
    }
