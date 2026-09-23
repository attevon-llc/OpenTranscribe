"""Usage event recording (billing + product analytics spine).

``record_event`` is callable from anywhere (API handlers, Celery tasks, hooks),
and an ``idempotency_key`` makes writers replay/retry-safe (a duplicate key is
reported, not raised). Events must contain IDs/counts only — never transcript
content (PII hygiene).

**Containment is the CALLER's decision, not this module's** (issue #981). A write
that genuinely failed raises; it does not return ``False``. ``False`` means, and
only means, "already recorded". Folding the two into one return value made a DB
outage indistinguishable from a successful dedupe, so a caller could neither
retry nor alarm on silently-dropped billable usage. A caller for whom accounting
must never break its feature wraps the call itself — see
``services.chat.usage.record_chat_usage``, which does exactly that.
"""

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def record_event(
    db: Session,
    *,
    event_type: str,
    quantity: Decimal | float | int = 1,
    unit: str | None = None,
    user_id: int | None = None,
    organization_id: int | None = None,
    file_id: int | None = None,
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Insert a usage event row. THREE outcomes, all distinguishable (issue #981).

    * ``True`` — the row was written and committed.
    * ``False`` — a duplicate ``idempotency_key``: this event was **already**
      recorded by an earlier attempt. The accounting is correct; there is
      nothing to retry.
    * **Raises** — the write failed for any other reason (DB unreachable,
      connection dropped, an unrelated constraint). The event is NOT recorded.

    That third case used to return ``False`` as well, which is what made it
    invisible: a caller could not tell "already counted" from "never counted",
    so billable usage vanished with no retry and no alarm. The session is still
    rolled back and the failure still logged before the exception leaves — only
    the swallowing is gone. Callers that must not fail on an accounting error
    wrap this call in their own ``try``/``except`` (``chat.usage``'s recorder is
    the worked example).

    Session-safety: BOTH failure paths call ``db.rollback()``, which discards ANY
    uncommitted work on the session. Call this with a dedicated session (a
    fresh ``session_scope()``), never a caller's live session holding pending
    writes — a duplicate-key skip would silently roll those writes back.

    Args:
        db: A dedicated session (see session-safety above).
        event_type: Dotted event name, e.g. ``"transcription.hours"``.
        quantity: Amount to record, coerced to ``Decimal`` via ``str``.
        unit: Unit of ``quantity`` (``"hours"``, ``"tokens"``, ...), or None.
        user_id: Acting user, when the event has one.
        organization_id: Tenant scope, when the event has one.
        file_id: Media file the event relates to, when it has one.
        idempotency_key: Replay guard. A repeat of a key already stored returns
            ``False`` instead of writing a second row.
        metadata: IDs/counts only — never transcript content.

    Returns:
        True when the row was written; False when ``idempotency_key`` was
        already recorded.

    Raises:
        Exception: Whatever the write failed with, after rollback and logging.
            Reaching here means the event was not recorded.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models.usage_event import UsageEvent

    try:
        db.add(
            UsageEvent(
                event_type=event_type,
                quantity=Decimal(str(quantity)),
                unit=unit,
                user_id=user_id,
                organization_id=organization_id,
                file_id=file_id,
                idempotency_key=idempotency_key,
                event_metadata=metadata,
            )
        )
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        logger.info(f"Usage event already recorded (idempotency_key={idempotency_key})")
        return False
    except Exception:
        db.rollback()
        # Logged AND re-raised, deliberately: the log is for the operator, the
        # exception is for the caller, and returning False here would have told
        # the caller the event was already safely recorded (issue #981).
        logger.exception(f"Failed to record usage event '{event_type}'")
        raise
