"""Usage event recording (billing + product analytics spine).

``record_event`` is safe to call from anywhere (API handlers, Celery tasks,
hooks): failures are contained and never break the calling feature, and an
``idempotency_key`` makes writers replay/retry-safe (duplicate keys are
silently ignored). Events must contain IDs/counts only — never transcript
content (PII hygiene).
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
    """Insert a usage event row. Returns True if recorded, False if skipped.

    Duplicate ``idempotency_key`` -> skipped (already recorded by a previous
    attempt). Any other failure is logged and swallowed — usage accounting
    must never break the feature that emitted the event.

    Session-safety: failure paths call ``db.rollback()``, which discards ANY
    uncommitted work on the session. Call this with a dedicated session (a
    fresh ``session_scope()``), never a caller's live session holding pending
    writes — a duplicate-key skip would silently roll those writes back.
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
        logger.exception(f"Failed to record usage event '{event_type}' (contained)")
        return False
