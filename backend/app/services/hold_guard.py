"""The ONE answer to "is this file held?" (issue #1276).

A file can be held by the platform (``media_file.legal_hold``, set by the operator's takedown,
or an active ``content_hold`` with ``authority = 'platform'``, placed by an instance administrator
without a takedown) or by an organization (an active ``content_hold`` with ``authority =
'organization'``, set by an organization administrator). Every code path that would destroy a file or restart its clock asks THIS module,
so a new hold source is one change here and not eleven guards to find. The read of
``MediaFile.legal_hold`` outside this module, the takedown services and the schemas is a bug:
``tests/unit/test_hold_guard_single_source.py`` fails on it.

Two forms, for the two shapes of caller:

* **Per file** (:func:`hold_state`, :func:`assert_not_held`): a request that names files.
* **In a query** (:func:`held_pred`, :func:`not_held_pred`): a sweep that selects files, where
  loading each row to ask would be the N+1 this exists to avoid.

What a refusal tells the caller (:func:`refusal_detail`) depends on who is asking. Everybody
learns that the file is held and by whom (``platform`` or ``organization``): a person has to know
whom to ask. The hold uuids are the handle for a release screen, so an organization
administrator is given their organization's and an instance administrator the platform's, never
the other's; the reason is never in a refusal at all.

**A hold freezes the file's content** (legal-hold plan, docs/design/legal_hold_ux_freeze_plan.md
§3). Under FRCP 37(e) a party must take reasonable steps to preserve information; this
deployment keeps no transcript versions (issue #46), so an edit to a held file would overwrite
the only copy of what was held. Every content write therefore refuses a held file
(:func:`assert_content_mutable`, or ``app.api.hold_refusal.refuse_if_frozen`` on a route) and
every background write skips a frozen one (:func:`frozen_states`, :func:`not_held_pred`);
``tests/unit/test_hold_freeze_coverage.py`` fails on a write path that does neither. Revisit
when #46 ships: with append-only versions, edits could be allowed and every version preserved.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy import Select
from sqlalchemy import and_
from sqlalchemy import exists
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.content_hold import AUTHORITY_ORGANIZATION
from app.models.content_hold import AUTHORITY_PLATFORM
from app.models.content_hold import ContentHold
from app.models.content_hold import ContentHoldFile
from app.models.media import MediaFile

#: Machine-readable code every API surface answers with when an action is declined because the
#: file is held: the single delete, the account delete, a cancelled upload, a reprocess and a
#: collection change alike. One constant, so a client handles one refusal and not five spellings.
LEGAL_HOLD_ERROR_CODE = "FILE_UNDER_LEGAL_HOLD"

#: Refusal text reported by ``purge_media_file`` for a held file. A module constant because
#: callers that translate the refusal into a response should not re-spell it.
LEGAL_HOLD_REFUSAL = (
    "refused: the file is under an active legal hold and cannot be deleted. "
    "Release the hold before deleting."
)

#: Refusal text for reprocessing or retrying a held file. Reprocessing replaces the transcript,
#: which is exactly what a hold exists to prevent.
REPROCESS_REFUSAL = (
    "refused: the file is under an active legal hold, so its transcript cannot be regenerated. "
    "Release the hold before reprocessing."
)

#: Refusal text for deleting the summary or a speaker of a held file. Both are rebuilt only by
#: reprocessing, which a hold also refuses, so deleting them would be unrecoverable.
DESTROY_DERIVED_REFUSAL = (
    "refused: the file is under an active legal hold, so its speakers and summary cannot be "
    "deleted. Release the hold first."
)

#: Refusal text for any other change to a held file's content (an edit, a rename, a tag, a
#: collection change, a new summary). Fixed text: a refusal never echoes an exception.
FREEZE_REFUSAL = (
    "refused: the file is under an active legal hold, so its content is preserved exactly as "
    "it was. You can still read, search, play, export and download it, and add a comment. An "
    "administrator must release the hold before it can be changed."
)

#: The closed vocabulary of ``content.hold.refused``'s ``details.action``. The first four were
#: written before the freeze and are kept verbatim: they are already in tenants' audit logs.
FREEZE_ACTIONS: tuple[str, ...] = (
    "delete",
    "cancel_upload",
    "summary_delete",
    "speaker_delete",
    "segment_edit",
    "segment_speaker",
    "speaker_merge",
    "speaker_create",
    "speaker_update",
    "speaker_link",
    "speaker_verify",
    "speaker_gender",
    "file_update",
    "summary_generate",
    "identify_speakers",
    "topic_extract",
    "topic_apply",
    "topic_dismiss",
    "auto_label",
    "redaction_rescan",
    "tag_add",
    "tag_remove",
    "tag_merge",
    "tag_delete",
    "collection_add",
    "remove_from_collection",
    "delete_collection",
    "comment_edit",
    "comment_delete",
    "reprocess",
    "retry",
    "recover",
    "profile_delete",
)

#: Statuses of a file whose initial processing run has not finished. A hold placed during that
#: run preserves the content as of its end, so the run itself is not a change to held content.
_IN_FLIGHT_STATUSES = frozenset({"pending", "processing"})

HELD_BY_PLATFORM = "platform"
HELD_BY_ORGANIZATION = "organization"


@dataclass(frozen=True)
class HoldState:
    """Who holds one file. ``held`` is true when anyone does.

    ``platform_flag`` is the takedown's ``media_file.legal_hold``; ``platform_hold_uuids`` the
    active platform ``content_hold`` rows. Both report as ``platform`` on the wire.
    """

    platform_flag: bool
    org_hold_uuids: tuple[str, ...] = ()
    platform_hold_uuids: tuple[str, ...] = ()

    @property
    def held(self) -> bool:
        return self.platform_flag or bool(self.org_hold_uuids) or bool(self.platform_hold_uuids)

    @property
    def held_by(self) -> list[str]:
        """``["platform"]``, ``["organization"]`` or both, in that order; empty when free."""
        by = []
        if self.platform_flag or self.platform_hold_uuids:
            by.append(HELD_BY_PLATFORM)
        if self.org_hold_uuids:
            by.append(HELD_BY_ORGANIZATION)
        return by


class HoldRefusedError(Exception):
    """An action was refused because at least one of its files is held."""

    def __init__(self, states: dict[int, HoldState]):
        super().__init__("one or more files are under a legal hold")
        self.states = states

    @property
    def file_ids(self) -> list[int]:
        return sorted(self.states)

    def merged(self) -> HoldState:
        """One state covering every refused file, for a response that names them together."""
        org: list[str] = []
        platform: list[str] = []
        for state in self.states.values():
            org.extend(u for u in state.org_hold_uuids if u not in org)
            platform.extend(u for u in state.platform_hold_uuids if u not in platform)
        return HoldState(
            platform_flag=any(s.platform_flag for s in self.states.values()),
            org_hold_uuids=tuple(org),
            platform_hold_uuids=tuple(platform),
        )


def _active_hold_covers_file() -> ColumnElement[bool]:
    """EXISTS an unreleased hold (any authority) covering the enclosing query's ``MediaFile``."""
    return exists().where(
        ContentHoldFile.media_file_id == MediaFile.id,
        ContentHold.id == ContentHoldFile.hold_id,
        ContentHold.released_at.is_(None),
    )


def held_pred() -> ColumnElement[bool]:
    """SQL predicate: the ``MediaFile`` row is held by the platform or by an organization.

    A platform ``content_hold`` row is covered by the same EXISTS as an organization's.
    """
    return or_(MediaFile.legal_hold.is_(True), _active_hold_covers_file())


def not_held_pred() -> ColumnElement[bool]:
    """SQL predicate: the ``MediaFile`` row is held by nobody. ``legal_hold`` is NOT NULL."""
    return and_(MediaFile.legal_hold.is_(False), ~_active_hold_covers_file())


def unheld_file_ids() -> Select[tuple[int]]:
    """``SELECT id`` of the files nobody holds, for ``<file column>.in_(...)`` in a write."""
    return select(MediaFile.id).where(not_held_pred())


def held_file_ids() -> Select[tuple[int]]:
    """``SELECT id`` of the held files, for ``<file column>.in_(...)``."""
    return select(MediaFile.id).where(held_pred())


def hold_states(db: Session, file_ids: Iterable[int]) -> dict[int, HoldState]:
    """The state of each named file; files held by nobody are absent from the result."""
    ids = sorted({int(i) for i in file_ids})
    if not ids:
        return {}
    platform = {
        int(row[0])
        for row in db.execute(
            select(MediaFile.id).where(MediaFile.id.in_(ids), MediaFile.legal_hold.is_(True))
        )
    }
    rows: dict[str, dict[int, list[str]]] = {AUTHORITY_ORGANIZATION: {}, AUTHORITY_PLATFORM: {}}
    for file_id, hold_uuid, authority in db.execute(
        select(ContentHoldFile.media_file_id, ContentHold.uuid, ContentHold.authority)
        .join(ContentHold, ContentHold.id == ContentHoldFile.hold_id)
        .where(ContentHoldFile.media_file_id.in_(ids), ContentHold.released_at.is_(None))
        .order_by(ContentHold.id)
    ):
        rows[str(authority)].setdefault(int(file_id), []).append(str(hold_uuid))
    org, by_platform = rows[AUTHORITY_ORGANIZATION], rows[AUTHORITY_PLATFORM]
    return {
        file_id: HoldState(
            platform_flag=file_id in platform,
            org_hold_uuids=tuple(org.get(file_id, ())),
            platform_hold_uuids=tuple(by_platform.get(file_id, ())),
        )
        for file_id in ids
        if file_id in platform or file_id in org or file_id in by_platform
    }


def hold_state(db: Session, file_id: int) -> HoldState:
    """Who holds ``file_id`` right now (a free file is a state with ``held == False``)."""
    return hold_states(db, [file_id]).get(int(file_id), HoldState(platform_flag=False))


def is_held(file: MediaFile) -> bool:
    """Whether a loaded ``MediaFile`` is held by anyone.

    For callers that have the row and no session in hand (``stamp_completion_time``). A row
    with no session to ask (a detached instance) is judged by the platform flag alone.
    """
    from sqlalchemy.orm import object_session

    session = object_session(file)
    if session is None or file.id is None:
        return bool(file.legal_hold)
    return hold_state(session, int(file.id)).held


def assert_not_held(db: Session, file_ids: Iterable[int]) -> None:
    """Raise :class:`HoldRefusedError` naming every held file among ``file_ids``; else return."""
    held = hold_states(db, file_ids)
    if held:
        raise HoldRefusedError(held)


def revealed_uuids(
    state: HoldState, *, reveal_uuids: bool, reveal_platform_uuids: bool = False
) -> list[str]:
    """The hold uuids this caller may be given: organization ones, platform ones, or both."""
    uuids: list[str] = []
    if reveal_uuids:
        uuids.extend(state.org_hold_uuids)
    if reveal_platform_uuids:
        uuids.extend(state.platform_hold_uuids)
    return uuids


def refusal_detail(
    state: HoldState,
    *,
    reveal_uuids: bool,
    reveal_platform_uuids: bool = False,
    message: str | None = None,
) -> dict[str, Any]:
    """The ``detail`` body of a 409 for a held file.

    ``held_by`` is for everybody. ``holds`` (the uuids a release screen takes) carries the
    organization's when ``reveal_uuids`` (an administrator of that organization) and the
    platform's when ``reveal_platform_uuids`` (an instance administrator). The hold's reason
    never appears.
    """
    detail: dict[str, Any] = {
        "error": LEGAL_HOLD_ERROR_CODE,
        "message": message or LEGAL_HOLD_REFUSAL,
        "held_by": state.held_by,
    }
    holds = revealed_uuids(
        state, reveal_uuids=reveal_uuids, reveal_platform_uuids=reveal_platform_uuids
    )
    if holds:
        detail["holds"] = holds
    return detail


def is_frozen_state(state: HoldState | None, status: object) -> bool:
    """Whether a file in ``state`` with processing ``status`` is frozen (held and not in flight)."""
    if state is None or not state.held:
        return False
    value = getattr(status, "value", status)
    return str(value) not in _IN_FLIGHT_STATUSES


def frozen_states(db: Session, file_ids: Iterable[int]) -> dict[int, HoldState]:
    """The held files among ``file_ids`` whose initial run has finished (one query for status).

    For background work: a task dispatched for a file held while it was still being processed
    finishes that run; anything after it is a separate write and is skipped.
    """
    held = hold_states(db, file_ids)
    if not held:
        return {}
    statuses: dict[int, object] = {
        int(file_id): status
        for file_id, status in db.execute(
            select(MediaFile.id, MediaFile.status).where(MediaFile.id.in_(held))
        )
    }
    return {
        file_id: state
        for file_id, state in held.items()
        if is_frozen_state(state, statuses.get(file_id))
    }


def is_frozen(db: Session, file_id: int) -> bool:
    """Whether one file is frozen; the per-file form of :func:`frozen_states`."""
    return int(file_id) in frozen_states(db, [file_id])


def assert_content_mutable(db: Session, file_ids: Iterable[int]) -> None:
    """Raise :class:`HoldRefusedError` when any of ``file_ids`` is held; else return.

    The service-level freeze guard (a request-scoped write). It refuses whenever the file is
    held, like :func:`assert_not_held`: a person cannot edit a file that is still processing,
    so the in-flight nuance of :func:`frozen_states` is for background work only.
    """
    assert_not_held(db, file_ids)
