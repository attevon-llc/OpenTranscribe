"""Has an erased organization member's data come back? (issue #1355, work package M)

An ``org_member`` erasure never deletes the ``user`` row, so "does the subject exist" cannot
detect a restore. The signal here is the rows themselves, bounded by time: an org-stamped row the
member owns whose creation time is **at or before** the erasure's ``completed_at``. A restore can
only bring back rows that existed before the erasure, and anything the member created afterwards is
newer than the cutoff, so a legitimate upload the next day is never mistaken for a resurrection.

The tables are exactly the ones ``gdpr_erasure_service.erase_org_member_data`` deletes by
``(user_id, organization_id)``; media files carry their timestamp as ``upload_time``, everything
else as ``created_at``. A row whose timestamp is NULL cannot be shown to predate the erasure, so it
is neither counted as resurrected nor deleted: it is reported (``untimed``) for review.
``ORG_MEMBER_RERUN_UNTIMED`` names tables with no timestamp column at all and why; it is empty
today and a test derives that from the models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.chat import ChatConversation
from app.models.custom_vocabulary import CustomVocabulary
from app.models.erasure import ErasureLedgerEntry
from app.models.media import Collection
from app.models.media import Comment
from app.models.media import MediaFile
from app.models.media import SpeakerCollection
from app.models.media import SpeakerProfile
from app.models.media import Tag
from app.models.media import Task
from app.models.prompt import SummaryPrompt
from app.models.prompt import UserSetting
from app.models.watch_source import WatchSource

#: ``(model, timestamp attribute)`` for every table the org-member erasure touches by
#: ``(user_id, organization_id)``. ``Comment`` and ``Task`` have no organization column of their
#: own: their tenant is the file's (see ``erase_org_member_data``), handled in :func:`_count`.
TIMED_TABLES: tuple[tuple[Any, str], ...] = (
    (MediaFile, "upload_time"),
    (SpeakerProfile, "created_at"),
    (Collection, "created_at"),
    (SpeakerCollection, "created_at"),
    (ChatConversation, "created_at"),
    (SummaryPrompt, "created_at"),
    (UserSetting, "created_at"),
    (CustomVocabulary, "created_at"),
    (WatchSource, "created_at"),
    (Tag, "created_at"),
    (Comment, "created_at"),
    (Task, "created_at"),
)

#: Tables the erasure touches that carry no creation timestamp, with the reason. Rows there cannot
#: be bounded by time, so a re-run skips them and says so.
ORG_MEMBER_RERUN_UNTIMED: dict[str, str] = {}


@dataclass(frozen=True)
class RerunState:
    #: Rows created at or before the cutoff: what a re-run would erase.
    old_rows: int
    #: Rows whose timestamp is NULL: left alone and reported.
    untimed_rows: int


def _count(db: Session, model: Any, ts_name: str, entry: ErasureLedgerEntry, *, old: bool) -> int:
    ts = getattr(model, ts_name)
    query = db.query(func.count()).select_from(model).filter(model.user_id == entry.subject_user_id)
    if model in (Comment, Task):
        org_files = db.query(MediaFile.id).filter(
            MediaFile.organization_id == entry.subject_organization_id
        )
        query = query.filter(model.media_file_id.in_(org_files.scalar_subquery()))
    else:
        query = query.filter(model.organization_id == entry.subject_organization_id)
    query = query.filter(ts <= entry.completed_at if old else ts.is_(None))
    return int(query.scalar() or 0)


def rerun_state(db: Session, entry: ErasureLedgerEntry) -> RerunState:
    """What the member has in the organization that predates (or cannot be dated against) the erasure."""
    if (
        entry.subject_user_id is None
        or entry.subject_organization_id is None
        or entry.completed_at is None
    ):
        return RerunState(0, 0)
    old = untimed = 0
    for model, ts_name in TIMED_TABLES:
        old += _count(db, model, ts_name, entry, old=True)
        untimed += _count(db, model, ts_name, entry, old=False)
    return RerunState(old, untimed)


def cutoff_of(entry: ErasureLedgerEntry) -> datetime | None:
    """The creation-time bound a re-run of this ``org_member`` entry applies, or ``None``.

    ``completed_at`` doubles as the bound once the entry has been resurrected (the ledger keeps it
    through a re-run), so a retry of a deferred re-run is still time-bounded.
    """
    if entry.subject_type == "org_member" and int(entry.resurrections_detected or 0) > 0:
        return entry.completed_at
    return None
