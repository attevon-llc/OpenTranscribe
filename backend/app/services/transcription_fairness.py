"""Per-user fairness for the transcription queues (concurrency audit F3, plan R2).

Every upload enters the pipeline at the same Celery priority, and within one priority the
Redis transport is FIFO. A user who uploads a folder of 500 files therefore put 500 runs in
front of every other user's single file, for days at 5-60 minutes a file.

The fix uses the priorities the queues already have (``core/celery.py``:
``priority_steps`` 0-9, ``queue_order_strategy: priority``; Celery, "Routing Tasks: Redis
Message Priorities", https://docs.celeryq.dev/en/stable/userguide/routing.html#redis-message-priorities):
an owner's first ``threshold`` active runs go in at the normal priority, and every run past
that at one step lower (``CPUPriority.PIPELINE_BACKLOG`` / ``GPUPriority.USER_BACKLOG``). A
newcomer's file then overtakes the bulk import's tail instead of waiting behind it, while
the bulk owner still has ``threshold`` runs at the front so their own work keeps moving.

What counts as the owner's backlog: their ``transcription`` task rows still ``pending`` or
``in_progress`` (running or queued), counted before the new run's row is written. The count
reads the active-only partial index on ``task``, so it stays cheap at any library size.

Deliberately simple, and not a full fair scheduler: a run's priority is decided once, at
dispatch, so a backlog that drains later is not promoted, and many users each under the
threshold share the normal priority FIFO as before. The threshold is the DB-backed setting
``transcription.fair_share_threshold`` (default
:data:`~app.core.constants.DEFAULT_TRANSCRIPTION_FAIR_SHARE_THRESHOLD`); 0 turns it off.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_TRANSCRIPTION_FAIR_SHARE_THRESHOLD
from app.core.constants import TRANSCRIPTION_FAIR_SHARE_THRESHOLD_KEY
from app.models.media import Task
from app.services.system_settings_service import get_setting_int
from app.utils.task_utils import TASK_STATUS_IN_PROGRESS
from app.utils.task_utils import TASK_STATUS_PENDING


def fair_share_threshold(db: Session) -> int:
    """The configured threshold; 0 (or a negative value) means no demotion."""
    return get_setting_int(
        db, TRANSCRIPTION_FAIR_SHARE_THRESHOLD_KEY, DEFAULT_TRANSCRIPTION_FAIR_SHARE_THRESHOLD
    )


def active_transcriptions(db: Session, user_id: int) -> int:
    """How many of ``user_id``'s transcription runs are queued or running."""
    return int(
        db.query(func.count(Task.id))
        .filter(
            Task.user_id == user_id,
            Task.task_type == "transcription",
            Task.status.in_((TASK_STATUS_PENDING, TASK_STATUS_IN_PROGRESS)),
        )
        .scalar()
        or 0
    )


def over_fair_share(db: Session, user_id: int) -> bool:
    """Whether a new run for ``user_id`` goes in behind other users' work.

    Call before the new run's task row is written: with a threshold of 5, the owner's
    first five active runs keep the normal priority and the sixth is demoted.
    """
    threshold = fair_share_threshold(db)
    if threshold <= 0:
        return False
    return active_transcriptions(db, user_id) >= threshold
