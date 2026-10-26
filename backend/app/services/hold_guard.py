"""The ONE answer to "is this file held?" (issue #1276).

A file can be held by the platform (``media_file.legal_hold``, set by the operator's takedown)
or by an organization (an active ``content_hold`` covering it, set by an organization
administrator). Every code path that would destroy a file or restart its clock asks THIS module,
so a new hold source is one change here and not eleven guards to find. The read of
``MediaFile.legal_hold`` outside this module, the takedown services and the schemas is a bug:
``tests/unit/test_hold_guard_single_source.py`` fails on it.

Two forms, for the two shapes of caller:

* **Per file** (:func:`hold_state`, :func:`assert_not_held`): a request that names files.
* **In a query** (:func:`held_pred`, :func:`not_held_pred`): a sweep that selects files, where
  loading each row to ask would be the N+1 this exists to avoid.

What a refusal tells the caller (:func:`refusal_detail`) depends on who is asking. Everybody
learns that the file is held and by whom (``platform`` or ``organization``): a person has to know
whom to ask. Only an organization administrator is given the hold uuids, because the uuid is
the handle for the release screen, and the reason is never in a refusal at all.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy import and_
from sqlalchemy import exists
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session

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
#: which is exactly what a hold exists to prevent; EDITING it is a different act (it is audited
#: and stays allowed), so the text says so.
REPROCESS_REFUSAL = (
    "refused: the file is under an active legal hold, so its transcript cannot be regenerated. "
    "Release the hold before reprocessing. Editing the transcript is still allowed."
)

HELD_BY_PLATFORM = "platform"
HELD_BY_ORGANIZATION = "organization"


@dataclass(frozen=True)
class HoldState:
    """Who holds one file. ``held`` is true when anyone does."""

    platform: bool
    org_hold_uuids: tuple[str, ...] = ()

    @property
    def held(self) -> bool:
        return self.platform or bool(self.org_hold_uuids)

    @property
    def held_by(self) -> list[str]:
        """``["platform"]``, ``["organization"]`` or both, in that order; empty when free."""
        by = []
        if self.platform:
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
        uuids: list[str] = []
        for state in self.states.values():
            uuids.extend(u for u in state.org_hold_uuids if u not in uuids)
        return HoldState(
            platform=any(s.platform for s in self.states.values()), org_hold_uuids=tuple(uuids)
        )


def _active_hold_covers_file() -> ColumnElement[bool]:
    """EXISTS an unreleased organization hold covering the enclosing query's ``MediaFile``."""
    return exists().where(
        ContentHoldFile.media_file_id == MediaFile.id,
        ContentHold.id == ContentHoldFile.hold_id,
        ContentHold.released_at.is_(None),
    )


def held_pred() -> ColumnElement[bool]:
    """SQL predicate: the ``MediaFile`` row is held by the platform or by an organization."""
    return or_(MediaFile.legal_hold.is_(True), _active_hold_covers_file())


def not_held_pred() -> ColumnElement[bool]:
    """SQL predicate: the ``MediaFile`` row is held by nobody. ``legal_hold`` is NOT NULL."""
    return and_(MediaFile.legal_hold.is_(False), ~_active_hold_covers_file())


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
    org: dict[int, list[str]] = {}
    for file_id, hold_uuid in db.execute(
        select(ContentHoldFile.media_file_id, ContentHold.uuid)
        .join(ContentHold, ContentHold.id == ContentHoldFile.hold_id)
        .where(ContentHoldFile.media_file_id.in_(ids), ContentHold.released_at.is_(None))
        .order_by(ContentHold.id)
    ):
        org.setdefault(int(file_id), []).append(str(hold_uuid))
    return {
        file_id: HoldState(platform=file_id in platform, org_hold_uuids=tuple(org.get(file_id, ())))
        for file_id in ids
        if file_id in platform or file_id in org
    }


def hold_state(db: Session, file_id: int) -> HoldState:
    """Who holds ``file_id`` right now (a free file is a state with ``held == False``)."""
    return hold_states(db, [file_id]).get(int(file_id), HoldState(platform=False))


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


def refusal_detail(
    state: HoldState, *, reveal_uuids: bool, message: str | None = None
) -> dict[str, Any]:
    """The ``detail`` body of a 409 for a held file.

    ``held_by`` is for everybody. ``holds`` (the uuids an organization administrator releases
    by) is added only when ``reveal_uuids`` is true. The hold's reason never appears.
    """
    detail: dict[str, Any] = {
        "error": LEGAL_HOLD_ERROR_CODE,
        "message": message or LEGAL_HOLD_REFUSAL,
        "held_by": state.held_by,
    }
    if reveal_uuids and state.org_hold_uuids:
        detail["holds"] = list(state.org_hold_uuids)
    return detail
