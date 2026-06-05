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
from typing import Optional
from typing import Union

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def record_event(
    db: Session,
    *,
    event_type: str,
    quantity: Union[Decimal, float, int] = 1,
    unit: Optional[str] = None,
    user_id: Optional[int] = None,
    organization_id: Optional[int] = None,
    file_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> bool:
    """Insert a usage event row. Returns True if recorded, False if skipped.

    Duplicate ``idempotency_key`` -> skipped (already recorded by a previous
    attempt). Any other failure is logged and swallowed — usage accounting
    must never break the feature that emitted the event.
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
