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

**"Created" means "acquired by its current owner"** (:func:`owned_since`). An organization content
transfer (``org_content_transfer``) re-owns rows and keeps their creation time; judged by creation
alone, content handed to the member AFTER their erasure looked restored, and the re-run purged it.
The transfer stamps ``owner_since`` (v471), and every bound here, in the time-bounded erasure and in
the index phase reads ``coalesce(owner_since, <creation time>)``. Search documents carry no such
stamp, so the document-level erasures exclude the handed-over rows by id (:func:`acquired_after`).
``ORG_MEMBER_RERUN_UNTIMED`` names tables with no timestamp column at all and why; it is empty
today and a test derives that from the models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.chat import ChatConversation
from app.models.collection import Collection
from app.models.custom_vocabulary import CustomVocabulary
from app.models.erasure import ErasureLedgerEntry
from app.models.media import Comment
from app.models.media import MediaFile
from app.models.media import Task
from app.models.prompt import SummaryPrompt
from app.models.prompt import UserSetting
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerCollection
from app.models.speaker import SpeakerProfile
from app.models.tag import Tag
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


#: Ids per ``terms`` clause; OpenSearch refuses more than ``index.max_terms_count`` (65,536).
_TERMS_CHUNK = 5000


def owned_since(model: Any, ts_name: str) -> ColumnElement[Any]:
    """When the row's CURRENT owner got it: the transfer's ``owner_since``, else its creation."""
    ts: ColumnElement[Any] = getattr(model, ts_name)
    stamp = getattr(model, "owner_since", None)
    return ts if stamp is None else func.coalesce(stamp, ts)


@dataclass(frozen=True)
class AcquiredAfter:
    """Rows a member was handed in one organization after a cutoff, for the search stores.

    Their documents carry the member's ``user_id`` (the transfer restamps it) and their content's
    own creation time, so a time bound alone would select them.
    """

    file_ids: tuple[int, ...] = ()
    profile_ids: tuple[int, ...] = ()
    cluster_uuids: tuple[str, ...] = ()

    def must_not(self, store_key: str) -> list[dict[str, Any]]:
        """``must_not`` clauses that keep these rows' documents in the store ``store_key``."""
        if store_key == "os.speakers":
            fields: list[tuple[str, tuple[Any, ...]]] = [
                ("media_file_id", self.file_ids),
                ("profile_id", self.profile_ids),
                ("cluster_uuid", self.cluster_uuids),
            ]
        elif store_key in ("os.transcripts", "os.transcript_chunks"):
            fields = [("file_id", self.file_ids)]
        else:
            fields = []
        return [
            {"terms": {name: list(ids[i : i + _TERMS_CHUNK])}}
            for name, ids in fields
            for i in range(0, len(ids), _TERMS_CHUNK)
        ]


def acquired_after(
    db: Session, user_id: int, organization_id: int, cutoff: datetime
) -> AcquiredAfter:
    """What ``user_id`` was handed in ``organization_id`` after ``cutoff``."""

    def ids(model: Any, column: Any) -> list[Any]:
        return [
            row[0]
            for row in db.query(column).filter(
                model.user_id == user_id,
                model.organization_id == organization_id,
                model.owner_since > cutoff,
            )
        ]

    return AcquiredAfter(
        file_ids=tuple(int(i) for i in ids(MediaFile, MediaFile.id)),
        profile_ids=tuple(int(i) for i in ids(SpeakerProfile, SpeakerProfile.id)),
        cluster_uuids=tuple(str(u) for u in ids(SpeakerCluster, SpeakerCluster.uuid)),
    )


def _count(db: Session, model: Any, ts_name: str, entry: ErasureLedgerEntry, *, old: bool) -> int:
    ts = owned_since(model, ts_name)
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
