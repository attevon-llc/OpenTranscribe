"""Opt-in voiceprint retention: destroy a file's voiceprints once nobody has used them.

Off by default (``privacy.voiceprint_retention_months`` = 0). An operator who sets it (for
example to 24) gets a daily sweep that selects, per tick, at most :data:`MAX_FILES_PER_RUN`
files that are due, and hands them to the privacy purge as one ``PurgeScope(kind="files")``.

A file is due when ALL of these hold (``due_file_ids``):

* it was uploaded before the cutoff (``now - months``);
* every profile linked to one of its speakers has ``last_interaction_at`` before the cutoff
  (an unlinked speaker counts as due; a profile with no recorded interaction counts as fresh,
  so an unknown never destroys anything). The definition of an interaction is in
  ``services/speaker_profile_activity.py``;
* nobody holds it (``hold_guard.not_held_pred``: platform legal hold or an organization hold);
* it still has voiceprint documents (measured with the same counter the purge residual uses),
  which makes the sweep idempotent: a purged file is never selected again.

What is NOT deleted here: speaker profiles. A profile left with no contributing speakers after
its files are purged is a named person record; deleting it is a human decision. Such profiles
are LISTED (``stale_profiles_left_empty``) in the task result and the log, never removed.

The purge itself (relational half, OpenSearch documents, the profile centroid recompute and the
two passes) belongs to ``services/privacy/purge.py``; this module only selects and dispatches,
through :func:`dispatch_files_purge`, the one call site of that interface.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy import and_
from sqlalchemy import exists
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.celery import celery_app
from app.core.constants import DEFAULT_PRIVACY_VOICEPRINT_RETENTION_MONTHS
from app.core.constants import PRIVACY_VOICEPRINT_RETENTION_MONTHS_KEY
from app.core.constants import UtilityPriority

logger = logging.getLogger(__name__)

TASK_NAME = "privacy.voiceprint_retention_sweep"

#: Files handed to one purge per tick; the next tick takes the rest.
MAX_FILES_PER_RUN = 200
#: Candidates read per tick before the voiceprint-presence filter.
_CANDIDATE_FACTOR = 4
#: A month in the cutoff arithmetic. 30 days: a schedule counted in months is a policy
#: horizon, not a calendar computation, and erring a few days late never destroys early.
_DAYS_PER_MONTH = 30


def retention_months(db: Session) -> int:
    """The configured months; 0 (off) for an absent, invalid or negative value."""
    from app.services.system_settings_service import get_setting_int

    months = get_setting_int(
        db, PRIVACY_VOICEPRINT_RETENTION_MONTHS_KEY, DEFAULT_PRIVACY_VOICEPRINT_RETENTION_MONTHS
    )
    return max(0, months)


def cutoff_for(months: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=months * _DAYS_PER_MONTH)


def _fresh_linked_profile(cutoff: datetime):
    """EXISTS a speaker of the file linked to a profile used on/after the cutoff (or never stamped)."""
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.models.media import SpeakerProfile

    return exists(
        select(Speaker.id)
        .join(SpeakerProfile, SpeakerProfile.id == Speaker.profile_id)
        .where(
            Speaker.media_file_id == MediaFile.id,
            or_(
                SpeakerProfile.last_interaction_at.is_(None),
                SpeakerProfile.last_interaction_at >= cutoff,
            ),
        )
    )


def candidate_file_ids(db: Session, cutoff: datetime, limit: int) -> list[int]:
    """Files meeting the Postgres half of the rule, oldest first."""
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.services.hold_guard import not_held_pred

    rows = db.execute(
        select(MediaFile.id)
        .where(
            and_(
                MediaFile.upload_time.is_not(None),
                MediaFile.upload_time < cutoff,
                not_held_pred(),
                exists(select(Speaker.id).where(Speaker.media_file_id == MediaFile.id)),
                ~_fresh_linked_profile(cutoff),
            )
        )
        .order_by(MediaFile.upload_time, MediaFile.id)
        .limit(limit)
    ).all()
    return [int(r[0]) for r in rows]


def _has_voiceprints(db: Session, file_id: int) -> bool | None:
    """True / False, or None when the store could not be measured."""
    from app.models.media import Speaker
    from app.services.gdpr_erasure_service import count_speaker_docs_by_ids

    uuids = [
        str(u)
        for u in db.execute(select(Speaker.uuid).where(Speaker.media_file_id == file_id)).scalars()
    ]
    count = count_speaker_docs_by_ids(uuids, errors=[])
    return None if count is None else count > 0


def due_file_ids(
    db: Session,
    cutoff: datetime,
    *,
    limit: int = MAX_FILES_PER_RUN,
    has_voiceprints: Callable[[Session, int], bool | None] = _has_voiceprints,
) -> list[int]:
    """At most ``limit`` due files. An unmeasurable file is skipped this tick, not purged."""
    due: list[int] = []
    for file_id in candidate_file_ids(db, cutoff, limit * _CANDIDATE_FACTOR):
        if has_voiceprints(db, file_id):
            due.append(file_id)
            if len(due) >= limit:
                break
    return due


def stale_profiles_left_empty(db: Session, cutoff: datetime, file_ids: list[int]) -> list[str]:
    """Stale profiles all of whose speakers are in ``file_ids``: listed, never deleted."""
    from app.models.media import Speaker
    from app.models.media import SpeakerProfile

    if not file_ids:
        return []
    other_speaker = exists(
        select(Speaker.id).where(
            Speaker.profile_id == SpeakerProfile.id, Speaker.media_file_id.not_in(file_ids)
        )
    )
    in_batch = exists(
        select(Speaker.id).where(
            Speaker.profile_id == SpeakerProfile.id, Speaker.media_file_id.in_(file_ids)
        )
    )
    rows = db.execute(
        select(SpeakerProfile.uuid).where(
            SpeakerProfile.last_interaction_at < cutoff, in_batch, ~other_speaker
        )
    ).all()
    return sorted(str(r[0]) for r in rows)


def stale_profile_count(db: Session, cutoff: datetime) -> int:
    """Profiles not interacted with since the cutoff (the admin privacy page's count)."""
    from sqlalchemy import func

    from app.models.media import SpeakerProfile

    return int(
        db.execute(
            select(func.count()).where(SpeakerProfile.last_interaction_at < cutoff)
        ).scalar_one()
    )


def dispatch_files_purge(file_ids: list[int]) -> str:
    """Queue the two-pass privacy purge for these files. Returns the task id.

    The ONE call site of the files-scope purge interface (``PurgeScope(kind="files",
    file_ids=...)`` with ``reason="retention"``: a retention destruction runs whether or not
    matching is still enabled). Raises when this build's ``PurgeScope`` has no ``files`` kind,
    so a sweep can never report work it did not queue.
    """
    import uuid as uuid_pkg

    from app.services.privacy import purge
    from app.tasks.privacy_purge_task import privacy_purge_task

    # Built from the payload form so this compiles against a PurgeScope that predates the
    # files kind; files_scope_supported() stops the sweep before it gets here in that build.
    scope = purge.PurgeScope.from_payload({"kind": "files", "file_ids": list(file_ids)})
    task_id = str(uuid_pkg.uuid4())
    privacy_purge_task.apply_async(
        kwargs={
            "scope": scope.to_payload(),
            "targets": ["voiceprints"],
            "actor_user_id": None,
            "phase": 1,
            "purge_reason": "retention",
        },
        task_id=task_id,
    )
    return task_id


def files_scope_supported() -> bool:
    from app.services.privacy import purge

    return "files" in getattr(purge, "_SCOPE_KINDS", ())


def run_sweep(
    db: Session,
    *,
    now: datetime | None = None,
    dispatch: Callable[[list[int]], str] = dispatch_files_purge,
    has_voiceprints: Callable[[Session, int], bool | None] = _has_voiceprints,
    scope_supported: Callable[[], bool] = files_scope_supported,
) -> dict[str, Any]:
    """One tick. Pure orchestration over the selection and the dispatch seam."""
    months = retention_months(db)
    if months <= 0:
        return {"status": "disabled", "retention_months": 0}
    if not scope_supported():
        logger.error(
            "Voiceprint retention is set to %d month(s) but this build's privacy purge has no "
            "files scope; nothing was destroyed",
            months,
        )
        return {"status": "unavailable", "retention_months": months}
    cutoff = cutoff_for(months, now)
    file_ids = due_file_ids(db, cutoff, has_voiceprints=has_voiceprints)
    if not file_ids:
        return {"status": "ok", "retention_months": months, "files": 0}
    left_empty = stale_profiles_left_empty(db, cutoff, file_ids)
    task_id = dispatch(file_ids)
    if left_empty:
        logger.info(
            "Voiceprint retention: %d stale profile(s) will have no speakers left; kept for "
            "review, not deleted",
            len(left_empty),
        )
    return {
        "status": "ok",
        "retention_months": months,
        "files": len(file_ids),
        "truncated": len(file_ids) >= MAX_FILES_PER_RUN,
        "purge_task_id": task_id,
        "stale_profiles_left_empty": left_empty,
    }


@celery_app.task(bind=True, name=TASK_NAME, priority=UtilityPriority.BACKGROUND)
def voiceprint_retention_sweep(self) -> dict:  # noqa: ARG001 — bind=True signature
    """Daily: destroy the voiceprints of files nobody has used for the configured months."""
    from app.db.session_utils import session_scope

    with session_scope() as db:
        return run_sweep(db)
