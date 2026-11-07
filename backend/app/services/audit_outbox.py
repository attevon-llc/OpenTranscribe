"""Transactional outbox for governance audit events (v459, issue #1329).

The pattern is the Transactional Outbox (microservices.io, "Pattern: Transactional outbox"): the
event is written as a row of ``audit_outbox`` in the SAME database transaction as the change it
describes, so the change and its record commit or roll back together, and a relay later copies
the row to the audit sink (``AuditLogger.deliver``: OpenSearch plus the audit log line).

* :func:`stage` adds the row to the caller's session. Call it BEFORE the commit. It raises like
  any other write in the transaction: an event that cannot be recorded fails the change with it.
* :func:`deliver_staged` runs right after the commit and tries to deliver what that session
  staged, so the event is normally in the audit viewer as soon as before. A sink outage or a
  database error there is logged, not raised: the rows stay pending for the relay.
* :func:`relay` (the ``audit.outbox_relay`` beat task, every minute) delivers every due pending
  row, retrying with exponential backoff. Rows are claimed with ``FOR UPDATE SKIP LOCKED`` (the
  PostgreSQL ``SELECT`` docs: "skipping locked rows provides an inconsistent view of the data, so
  this is not suitable for general purpose work, but can be used to avoid lock contention with
  multiple consumers accessing a queue-like table"); the claim pushes ``next_attempt_at`` forward
  as a lease and commits at once, so no transaction is held across the sink round trip (issue
  #440) and a relay that dies mid-pass releases its rows when the lease lapses.
* Delivery is idempotent: the sink document id is the row's ``event_uuid`` and the write is
  create-only, so a resend after a crash between the write and the ``sent`` mark indexes nothing.
* A row pending past ``AUDIT_OUTBOX_STUCK_AFTER_SECONDS``, or one the sink rejected, is stuck:
  the relay logs it at ERROR and ``audit_outbox_stuck_events`` counts it at scrape time.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.auth.audit import AuditEventRejectedError
from app.auth.audit import AuditSinkUnavailableError
from app.auth.audit import audit_logger
from app.core.constants import AUDIT_OUTBOX_BATCH_SIZE
from app.core.constants import AUDIT_OUTBOX_LEASE_SECONDS
from app.core.constants import AUDIT_OUTBOX_RETRY_BASE_SECONDS
from app.core.constants import AUDIT_OUTBOX_RETRY_MAX_SECONDS
from app.core.constants import AUDIT_OUTBOX_SENT_RETENTION_DAYS
from app.core.constants import AUDIT_OUTBOX_STUCK_AFTER_SECONDS
from app.models.audit_outbox import LAST_ERROR_MAX_LENGTH
from app.models.audit_outbox import AuditOutbox
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: ``Session.info`` key holding the event uuids a session staged, for :func:`deliver_staged`.
_STAGED_KEY = "audit_outbox_staged"

#: Most relay passes one beat tick makes; a deeper backlog continues on the next tick.
MAX_PASSES_PER_RUN = 20

_CLAIM_SQL = """
UPDATE audit_outbox SET next_attempt_at = now() + make_interval(secs => :lease)
WHERE id IN (
    SELECT id FROM audit_outbox
    WHERE status = 'pending' AND next_attempt_at <= now() {only}
    ORDER BY id
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
)
RETURNING id, event_uuid, event_type, payload, attempts
"""

_STUCK_SQL = """
SELECT count(*) FROM audit_outbox
WHERE status = 'rejected'
   OR (status = 'pending' AND created_at < now() - make_interval(secs => :stuck))
"""


@dataclass
class RelayResult:
    """What one relay pass did, by outcome."""

    claimed: int = 0
    sent: int = 0
    already_in_sink: int = 0
    retrying: int = 0
    rejected: int = 0
    deferred: int = 0


@dataclass(frozen=True)
class _Claimed:
    id: int
    event_uuid: str
    event_type: str
    payload: dict[str, Any]
    attempts: int


def stage(db: Session, **log_kwargs: Any) -> str | None:
    """Add a governance audit event to ``db``'s transaction; return its uuid.

    Takes exactly ``AuditLogger.log``'s keyword arguments. The event document is built now
    (timestamp, request id, client address) and stored as the row's payload. Returns None and
    stages nothing while audit logging is off, as ``log`` writes nothing then.
    """
    event = audit_logger.build_event(**log_kwargs)
    if event is None:
        return None
    event_uuid = uuid7()
    payload = json.loads(json.dumps(event, default=str))
    db.add(
        AuditOutbox(event_uuid=event_uuid, event_type=str(payload["event_type"]), payload=payload)
    )
    # Flushed now so a row the database refuses fails the request here, before its commit.
    db.flush()
    db.info.setdefault(_STAGED_KEY, []).append(str(event_uuid))
    return str(event_uuid)


def deliver_staged(db: Session) -> None:
    """After the commit: try to deliver what ``db`` staged.

    A sink outage is handled by :func:`relay`; a database error is logged and swallowed. Either
    way the change has committed and its rows stay pending for the beat relay.

    Uses its own short sessions, never ``db``: the caller's session may still hold work of its
    own. A uuid whose transaction rolled back matches no row and is simply dropped.
    """
    try:
        staged = list(db.info.pop(_STAGED_KEY, None) or ())
        if staged:
            relay(only=staged)
    except SQLAlchemyError:
        logger.warning(
            "audit outbox: immediate delivery failed; the relay will retry", exc_info=True
        )


def retry_delay_seconds(attempts: int) -> int:
    """Seconds to wait before attempt ``attempts + 1`` (exponential, capped)."""
    exponent = max(attempts - 1, 0)
    return int(min(AUDIT_OUTBOX_RETRY_BASE_SECONDS * (2**exponent), AUDIT_OUTBOX_RETRY_MAX_SECONDS))


def _default_session_factory() -> Session:
    from app.db.base import SessionLocal

    return SessionLocal()


def _claim(
    session_factory: Callable[[], Session], only: list[str] | None, limit: int
) -> list[_Claimed]:
    sql = _CLAIM_SQL.format(only="AND event_uuid = ANY(CAST(:only AS uuid[]))" if only else "")
    params: dict[str, Any] = {"lease": AUDIT_OUTBOX_LEASE_SECONDS, "limit": limit}
    if only:
        params["only"] = only
    db = session_factory()
    try:
        rows = db.execute(text(sql), params).all()
        db.commit()
    finally:
        db.close()
    return sorted(
        (
            _Claimed(
                id=int(r.id),
                event_uuid=str(r.event_uuid),
                event_type=str(r.event_type),
                payload=dict(r.payload),
                attempts=int(r.attempts),
            )
            for r in rows
        ),
        key=lambda c: c.id,
    )


def _short_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:LAST_ERROR_MAX_LENGTH]


def _record(
    session_factory: Callable[[], Session],
    sent: list[int],
    failed: list[tuple[int, int, str]],
    rejected: list[tuple[int, str]],
    deferred: list[int],
    defer_seconds: int,
) -> None:
    """Write every outcome of one pass in one short transaction."""
    db = session_factory()
    try:
        if sent:
            db.execute(
                text(
                    "UPDATE audit_outbox SET status = 'sent', sent_at = now(), last_error = NULL "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": sent},
            )
        for row_id, delay, error in failed:
            db.execute(
                text(
                    "UPDATE audit_outbox SET attempts = attempts + 1, last_error = :e, "
                    "next_attempt_at = now() + make_interval(secs => :d) WHERE id = :i"
                ),
                {"i": row_id, "d": delay, "e": error},
            )
        for row_id, error in rejected:
            db.execute(
                text(
                    "UPDATE audit_outbox SET status = 'rejected', attempts = attempts + 1, "
                    "last_error = :e WHERE id = :i"
                ),
                {"i": row_id, "e": error},
            )
        if deferred:
            # Not attempted: the sink failed on an earlier row of this pass. Same wait, no
            # attempt counted.
            db.execute(
                text(
                    "UPDATE audit_outbox SET next_attempt_at = now() + make_interval(secs => :d) "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": deferred, "d": defer_seconds},
            )
        db.commit()
    finally:
        db.close()


def relay(
    *,
    only: list[str] | None = None,
    limit: int = AUDIT_OUTBOX_BATCH_SIZE,
    session_factory: Callable[[], Session] | None = None,
) -> RelayResult:
    """One relay pass: claim due pending rows, deliver each, record the outcomes.

    Args:
        only: Restrict the pass to these event uuids (:func:`deliver_staged`).
        limit: Most rows claimed.
        session_factory: Opens a session (default ``SessionLocal``); each claim and each record
            step uses its own, committed at once.

    The first sink failure ends the pass: the failing row is retried after its backoff and the
    rest of the claim waits the same time without an attempt counted, so a down sink is not
    asked once per row.
    """
    factory = session_factory or _default_session_factory
    result = RelayResult()
    claimed = _claim(factory, only, limit)
    result.claimed = len(claimed)
    if not claimed:
        return result

    sent: list[int] = []
    failed: list[tuple[int, int, str]] = []
    rejected: list[tuple[int, str]] = []
    deferred: list[int] = []
    defer_seconds = 0
    for index, row in enumerate(claimed):
        try:
            wrote = audit_logger.deliver(row.payload, row.event_uuid)
        except AuditEventRejectedError as error:
            logger.error(
                "audit outbox: the sink rejected event %s (%s); it stays in audit_outbox as "
                "'rejected': %s",
                row.event_uuid,
                row.event_type,
                error,
            )
            rejected.append((row.id, _short_error(error)))
            continue
        except AuditSinkUnavailableError as error:
            defer_seconds = retry_delay_seconds(row.attempts + 1)
            logger.warning(
                "audit outbox: delivery of event %s (%s) failed, retry in %ds: %s",
                row.event_uuid,
                row.event_type,
                defer_seconds,
                error,
            )
            failed.append((row.id, defer_seconds, _short_error(error)))
            deferred = [later.id for later in claimed[index + 1 :]]
            break
        sent.append(row.id)
        if not wrote:
            result.already_in_sink += 1
    _record(factory, sent, failed, rejected, deferred, defer_seconds)
    result.sent = len(sent)
    result.retrying = len(failed)
    result.rejected = len(rejected)
    result.deferred = len(deferred)
    return result


def count_stuck(db: Session) -> int:
    """Rows the sink has not received in ``AUDIT_OUTBOX_STUCK_AFTER_SECONDS``, or rejected."""
    return int(
        db.execute(text(_STUCK_SQL), {"stuck": AUDIT_OUTBOX_STUCK_AFTER_SECONDS}).scalar() or 0
    )


def prune_sent(db: Session, keep_days: int = AUDIT_OUTBOX_SENT_RETENTION_DAYS) -> int:
    """Delete ``sent`` rows older than ``keep_days`` (the sink holds them). Caller commits."""
    result = db.execute(
        text(
            "DELETE FROM audit_outbox WHERE status = 'sent' "
            "AND sent_at < now() - make_interval(days => :d)"
        ),
        {"d": int(keep_days)},
    )
    return int(getattr(result, "rowcount", 0) or 0)


_gauge_lock = threading.Lock()
_gauge_refreshed_at: float | None = None


def update_stuck_gauge(db: Session | None = None) -> int | None:
    """Set ``audit_outbox_stuck_events`` from the database; return the count (None on a DB error)."""
    try:
        from app.core.metrics import audit_outbox_stuck_events

        if db is not None:
            count = count_stuck(db)
        else:
            own = _default_session_factory()
            try:
                count = count_stuck(own)
            finally:
                own.close()
        audit_outbox_stuck_events.set(count)
        return count
    except SQLAlchemyError as exc:  # a scrape must never fail on a database issue
        logger.debug("audit outbox gauge sampling skipped: %s", exc)
        return None


def refresh_stuck_gauge() -> None:
    """:func:`update_stuck_gauge` at most once per ``JOB_METRICS_TTL_SECONDS`` (issue #1001)."""
    global _gauge_refreshed_at
    from app.core.backup_metrics import JOB_METRICS_TTL_SECONDS

    with _gauge_lock:
        now = time.monotonic()
        if _gauge_refreshed_at is not None and 0 <= now - _gauge_refreshed_at < (
            JOB_METRICS_TTL_SECONDS
        ):
            return
        _gauge_refreshed_at = now
    update_stuck_gauge()
