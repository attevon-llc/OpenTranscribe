"""Read-side enforcement of the privacy policy: what a response may still show (issue #1280).

Turning a privacy layer off HIDES existing data and never deletes it (plan D5), so every read
that could surface a voiceprint-derived or inferred value passes through here. The policy
itself (who may have what) is :mod:`app.services.privacy.policy`; this module only asks it and
shapes the answer.

Two rules govern what is hidden while a scope's switch is off:

* **Voiceprints off**: kNN suggestion reads return nothing, and a *stored* suggestion that a
  voiceprint wrote (``suggestion_source`` in ``voice_match`` / ``profile_match``) and nobody
  verified is omitted. A name and profile link a voiceprint path wrote
  (``speaker.identity_source``, see ``utils/speaker_identity_source``) are omitted too: an
  auto-accepted match looks exactly like a person's confirmation otherwise. Named profiles,
  avatars, typed names and person-made links are user assertions, not biometric data, and
  stay.
* **Gender off**: gender the model inferred is omitted (``null``) wherever it is serialized or
  used to decide something. A gender a person confirmed (``gender_confirmed_by_user``) is
  theirs and stays.

Every decision is taken for the *record's* scope (the file's owner and organization, never
request state) and fails closed: an unreadable file, a resolver error or a hook error hides.

The row helpers use ``set_committed_value`` so a redacted attribute is never flushed: the
database keeps the real value, which is how re-enabling the layer restores visibility.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING
from typing import Any

from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm.attributes import set_committed_value

from app.services.privacy import policy
from app.services.privacy.consent import has_voiceprint_consent_hooks
from app.services.privacy.policy import Decision
from app.utils.speaker_identity_source import MACHINE_IDENTITY_SOURCES
from app.utils.speaker_identity_source import VOICEPRINT
from app.utils.speaker_identity_source import hides_link
from app.utils.speaker_identity_source import hides_name
from app.utils.speaker_labels import canonical_speaker_label_for_row
from app.utils.speaker_labels import is_embedding_suggestion_source

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement
    from sqlalchemy.orm import Session

    from app.models.media import Speaker

logger = logging.getLogger(__name__)

#: Error code of the 409 a voiceprint-creating or voiceprint-recomputing request gets while
#: its scope is off. The reason (``locked_by``) rides in the same body (plan D8).
VOICEPRINTS_DISABLED_CODE = "voiceprints_disabled"

_DENY = Decision(False, "instance")

Scope = tuple[int, int | None]

#: Keys of the STORED ``Speaker.attribute_confidence`` bag that are derived from the inferred
#: gender: the model's confidence, and the voice-versus-name alignment judged from it. These
#: are the stored names; the response fields ``gender_alignment`` / ``gender_alignment_hint``
#: are split out of them by ``schemas.media.split_attribute_confidence`` (the purge drops the
#: same three keys).
_INFERRED_GENDER_KEYS = frozenset({"gender", "alignment", "alignment_hint"})


class PrivacyView:
    """Policy answers for one unit of work (a request, a task step), memoised.

    A list page can span hundreds of files owned by a handful of (owner, organization)
    scopes. The view reads each file's scope once (one batched query) and resolves the policy
    once per scope, so a page costs a few queries rather than one per row.

    Not thread-safe and not meant to outlive its unit of work: the memo is exactly the
    staleness the entry gates promise not to have.
    """

    def __init__(self, db: Session) -> None:
        self._db = db
        self._file_scope: dict[int, Scope | None] = {}
        self._voiceprints: dict[Scope | tuple[Scope, int], Decision] = {}
        self._gender: dict[Scope, Decision] = {}

    def preload_files(self, file_ids: Iterable[int]) -> None:
        """Read the scope of every not-yet-known file in one query. Unreadable = unknown."""
        from app.models.media import MediaFile

        wanted = {int(f) for f in file_ids if f is not None} - set(self._file_scope)
        if not wanted:
            return
        try:
            # A SAVEPOINT: this read runs inside the caller's transaction, and a failed
            # statement aborts the whole transaction on PostgreSQL. Rolled back to the
            # savepoint, the files read as off and the caller can still issue queries.
            rows = policy._isolated(
                self._db,
                lambda: (
                    self._db.query(MediaFile.id, MediaFile.user_id, MediaFile.organization_id)
                    .filter(MediaFile.id.in_(wanted))
                    .all()
                ),
            )
        except Exception:
            logger.exception("privacy view: could not read file scopes; denying")
            rows = []
        found: dict[int, Scope] = {
            int(r[0]): (int(r[1]), int(r[2]) if r[2] is not None else None) for r in rows
        }
        for fid in wanted:
            self._file_scope[fid] = found.get(fid)

    def file_scope(self, file_id: int) -> Scope | None:
        """(owner, organization) of a file, or None when it cannot be read."""
        self.preload_files([file_id])
        return self._file_scope.get(int(file_id))

    def voiceprints_for_scope(
        self, user_id: int, organization_id: int | None, media_file_id: int | None = None
    ) -> Decision:
        """May voiceprints exist for this scope? Layers plus the ``create`` consent hooks.

        The layers decide per (owner, organization), so one answer serves every file of a
        scope. A consent hook is handed the file and may refuse one file of a scope, so while
        any hook is registered the answer is remembered per file instead.
        """
        scope = (int(user_id), organization_id)
        key: Scope | tuple[Scope, int] = scope
        if media_file_id is not None and has_voiceprint_consent_hooks():
            key = (scope, int(media_file_id))
        if key not in self._voiceprints:
            self._voiceprints[key] = policy.voiceprint_decision(
                self._db,
                user_id=scope[0],
                organization_id=scope[1],
                media_file_id=media_file_id,
            )
        return self._voiceprints[key]

    def gender_for_scope(self, user_id: int, organization_id: int | None) -> Decision:
        """May gender be inferred for this scope?"""
        scope = (int(user_id), organization_id)
        if scope not in self._gender:
            self._gender[scope] = policy.gender_decision(
                self._db, user_id=scope[0], organization_id=scope[1]
            )
        return self._gender[scope]

    def voiceprints(self, file_id: int) -> Decision:
        """Voiceprint decision for a file's owner and organization (unknown file = denied)."""
        scope = self.file_scope(file_id)
        if scope is None:
            return _DENY
        return self.voiceprints_for_scope(scope[0], scope[1], int(file_id))

    def gender(self, file_id: int) -> Decision:
        """Gender decision for a file's owner and organization (unknown file = denied)."""
        scope = self.file_scope(file_id)
        if scope is None:
            return _DENY
        return self.gender_for_scope(scope[0], scope[1])

    def voiceprints_off_file_ids(self, file_ids: Iterable[int]) -> set[int]:
        """The subset of ``file_ids`` whose voiceprints are off (or whose scope is unknown)."""
        ids = {int(f) for f in file_ids if f is not None}
        self.preload_files(ids)
        return {fid for fid in ids if not self.voiceprints(fid).enabled}

    def label_for_row(self, row: Any) -> str:
        """The canonical label for a speaker row, with this view's voiceprint answer applied."""
        return canonical_speaker_label_for_row(row, self)

    def speaker_gender(self, speaker: Speaker) -> str | None:
        """The gender to ACT on or SHOW for a speaker: confirmed always, inferred if allowed."""
        gender = speaker.predicted_gender
        if not gender:
            return None
        if speaker.gender_confirmed_by_user:
            return str(gender)
        return str(gender) if self.gender(int(speaker.media_file_id)).enabled else None


def voiceprints_disabled_error(decision: Decision) -> HTTPException:
    """The 409 for a request that would create or recompute a voiceprint (plan D8)."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": VOICEPRINTS_DISABLED_CODE, "locked_by": decision.locked_by},
    )


def require_voiceprints(db: Session, *, user_id: int, organization_id: int | None) -> None:
    """Raise the 409 unless voiceprints are allowed for this (owner, organization) scope."""
    decision = policy.voiceprint_decision(db, user_id=user_id, organization_id=organization_id)
    if not decision.enabled:
        raise voiceprints_disabled_error(decision)


def hides_voiceprint_suggestion(speaker: Any) -> bool:
    """A stored suggestion a voiceprint wrote that is not a person's verified word (D5).

    Hidden when nobody verified it, and also when ``verified`` was written by a voiceprint
    path (``identity_source = 'voiceprint'``): an auto-accepted match sets ``verified`` too,
    and its suggestion is the evidence of the match.

    ``getattr`` for ``verified`` / ``identity_source``: a columns-only row that did not select
    ``verified`` cannot prove a person verified the suggestion, so it reads as unverified (the
    hiding side).
    """
    if not is_embedding_suggestion_source(getattr(speaker, "suggestion_source", None)):
        return False
    return (
        getattr(speaker, "verified", None) is not True
        or getattr(speaker, "identity_source", None) == VOICEPRINT
    )


def voiceprints_off_for_row(row: Any, db: Session | PrivacyView | None = None) -> bool:
    """Are voiceprints off for the FILE this speaker row belongs to? Fails closed.

    The question every label surface asks before leaving a voiceprint-derived name out
    (``utils.speaker_labels.canonical_speaker_label_for_row``). True (off) when the file's
    policy cannot be read or the row names a file but no session can be found to ask; False
    for a value that names no file or was constructed in memory and never persisted.

    Args:
        row: A ``Speaker`` or a columns-only row exposing ``media_file_id``.
        db: A session, or a :class:`PrivacyView` to share its memo across rows. Omitted: the
            session the row is attached to.
    """
    file_id = getattr(row, "media_file_id", None)
    if file_id is None:
        return False  # not a persisted record: nothing to scope it to
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm import object_session

    state = sa_inspect(row, raiseerr=False)
    if db is None and state is not None and state.transient:
        return False  # a value somebody constructed, never read from a file
    session = db if db is not None else (object_session(row) if state is not None else None)
    if session is None:
        return True  # names a file but nothing can be asked: hide
    try:
        view = session if isinstance(session, PrivacyView) else PrivacyView(session)
        return not view.voiceprints(int(file_id)).enabled
    except Exception:
        logger.exception("privacy view: could not decide the label of file %s; hiding", file_id)
        return True


def hide_machine_identities(view: Session | PrivacyView, speakers: Iterable[Speaker]) -> list[Any]:
    """Withhold, on the loaded rows, the names and links a voiceprint made (#1280 D5).

    For each row whose FILE has voiceprints off: a ``voiceprint`` identity loses its
    ``display_name`` and ``verified`` flag, and any machine identity (``voiceprint`` or
    ``voiceprint_link``) loses its ``profile_id`` / ``profile``. A ``human`` or NULL identity
    is never touched. Values are set with ``set_committed_value``: the database keeps them and
    re-enabling voiceprints shows them again.

    Reads only ``identity_source``, ``media_file_id``, ``display_name``, ``profile_id`` and
    ``verified``, so it is safe on ``load_only`` rows that load those five.

    Returns:
        The rows it changed.
    """
    view = view if isinstance(view, PrivacyView) else PrivacyView(view)
    rows = [s for s in speakers if s.identity_source in MACHINE_IDENTITY_SOURCES]
    if not rows:
        return []
    view.preload_files(int(s.media_file_id) for s in rows)
    changed: list[Any] = []
    for speaker in rows:
        enabled = view.voiceprints(int(speaker.media_file_id)).enabled
        source = speaker.identity_source
        if not hides_link(source, enabled):  # hides_name implies hides_link
            continue
        if hides_name(source, enabled):
            set_committed_value(speaker, "display_name", None)
            set_committed_value(speaker, "verified", False)
        set_committed_value(speaker, "profile_id", None)
        set_committed_value(speaker, "profile", None)
        changed.append(speaker)
    return changed


def redact_speaker_rows(db: Session | PrivacyView, speakers: Iterable[Speaker]) -> None:
    """Hide, on the loaded rows, what the owner's current policy forbids showing.

    Call it after the last mutation and before the rows are serialized. The rows are changed
    with ``set_committed_value``: the in-memory object reads redacted, the database row is
    untouched and the attribute is never flushed.

    Order: machine-made names and links first (:func:`hide_machine_identities`), then the
    voiceprint suggestion and inferred gender rules. The status fields
    (``computed_status``, ``status_text``, ``status_color``, ``resolved_display_name``) of
    every row whose name, link or suggestion changed are recomputed on the redacted row, so
    a response never carries a status derived from what was just withheld.
    """
    from app.services.speaker_status_service import SpeakerStatusService

    view = db if isinstance(db, PrivacyView) else PrivacyView(db)
    rows = list(speakers)
    view.preload_files(int(s.media_file_id) for s in rows)
    relabelled = {id(s): s for s in hide_machine_identities(view, rows)}
    for speaker in rows:
        file_id = int(speaker.media_file_id)
        if hides_voiceprint_suggestion(speaker) and not view.voiceprints(file_id).enabled:
            set_committed_value(speaker, "suggested_name", None)
            set_committed_value(speaker, "suggestion_source", None)
            set_committed_value(speaker, "confidence", None)
            relabelled[id(speaker)] = speaker
        if (
            not speaker.gender_confirmed_by_user
            and (speaker.predicted_gender or speaker.attribute_confidence)
            and not view.gender(file_id).enabled
        ):
            set_committed_value(speaker, "predicted_gender", None)
            bag = speaker.attribute_confidence
            if isinstance(bag, dict):
                kept = {k: v for k, v in bag.items() if k not in _INFERRED_GENDER_KEYS}
                set_committed_value(speaker, "attribute_confidence", kept or None)
    for speaker in relabelled.values():
        SpeakerStatusService.add_computed_status(speaker)


def machine_identity_clauses(
    view: Session | PrivacyView, file_ids_select: Any
) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    """SQL twins of :func:`hide_machine_identities` for queries that never load rows.

    Returns ``(name_hidden, link_hidden)``: boolean expressions over ``Speaker`` that are true
    for a speaker whose ``display_name`` (resp. profile link) a voiceprint made and whose
    file's scope has voiceprints off. Use them through :func:`visible_display_name_expr` /
    :func:`visible_profile_id_expr`, or as ``not_(name_hidden)`` in a filter.

    The off scopes are decided in Python (the policy has layers and hooks SQL cannot ask):
    one query reads the DISTINCT ``(owner, organization)`` of the files in
    ``file_ids_select`` that carry a machine identity, and each scope is asked once through
    the view. Scopes are matched with ``or_`` of per-scope conditions, never
    ``tuple_(...).in_(...)``: ``(user, NULL) IN (...)`` is NULL in SQL and would silently let
    every personal-workspace row through.

    Args:
        view: A session or a :class:`PrivacyView` (its memo is shared).
        file_ids_select: A selectable of ``media_file.id`` values bounding the query (for
            example the caller's accessible files).

    Returns:
        ``(false(), false())`` when no scope is off, so callers pay nothing further.
    """
    from sqlalchemy import and_
    from sqlalchemy import false
    from sqlalchemy import or_
    from sqlalchemy import select

    from app.models.media import MediaFile
    from app.models.media import Speaker

    view = view if isinstance(view, PrivacyView) else PrivacyView(view)
    scopes = view._db.execute(
        select(MediaFile.user_id, MediaFile.organization_id)
        .where(
            MediaFile.id.in_(file_ids_select),
            MediaFile.id.in_(
                select(Speaker.media_file_id).where(
                    Speaker.identity_source.in_(sorted(MACHINE_IDENTITY_SOURCES))
                )
            ),
        )
        .distinct()
    ).all()
    off = [
        (int(user_id), org_id)
        for user_id, org_id in scopes
        if not view.voiceprints_for_scope(
            int(user_id), int(org_id) if org_id is not None else None
        ).enabled
    ]
    if not off:
        return false(), false()
    in_off_scope = or_(
        *(
            and_(
                MediaFile.user_id == user_id,
                MediaFile.organization_id.is_(None)
                if org_id is None
                else MediaFile.organization_id == org_id,
            )
            for user_id, org_id in off
        )
    )
    off_files = select(MediaFile.id).where(MediaFile.id.in_(file_ids_select), in_off_scope)
    in_off_file = Speaker.media_file_id.in_(off_files)
    name_hidden = and_(Speaker.identity_source == VOICEPRINT, in_off_file)
    link_hidden = and_(Speaker.identity_source.in_(sorted(MACHINE_IDENTITY_SOURCES)), in_off_file)
    return name_hidden, link_hidden


def visible_display_name_expr(name_hidden: ColumnElement[bool]) -> ColumnElement[Any]:
    """``speaker.display_name``, or NULL where ``name_hidden`` (see :func:`machine_identity_clauses`)."""
    from sqlalchemy import case

    from app.models.media import Speaker

    return case((name_hidden, None), else_=Speaker.display_name)


def visible_profile_id_expr(link_hidden: ColumnElement[bool]) -> ColumnElement[Any]:
    """``speaker.profile_id``, or NULL where ``link_hidden`` (see :func:`machine_identity_clauses`)."""
    from sqlalchemy import case

    from app.models.media import Speaker

    return case((link_hidden, None), else_=Speaker.profile_id)


def derived_profile_genders(db: Session, profiles: Iterable[Any]) -> dict[int, str]:
    """Majority gender of each profile's linked speakers, as the policy allows it.

    A profile whose owner scope has gender off is voted on by the speakers whose gender a
    person confirmed only; any other profile uses every inferred value as before. Returns
    ``profile_id -> gender`` for profiles that have one.
    """
    from sqlalchemy import func

    from app.models.media import Speaker

    rows = list(profiles)
    if not rows:
        return {}
    view = PrivacyView(db)
    off_ids = [
        int(p.id)
        for p in rows
        if not view.gender_for_scope(int(p.user_id), p.organization_id).enabled
    ]
    result: dict[int, str] = {}
    for confirmed_only, ids in (
        (False, [int(p.id) for p in rows if int(p.id) not in set(off_ids)]),
        (True, off_ids),
    ):
        if not ids:
            continue
        query = db.query(
            Speaker.profile_id,
            Speaker.predicted_gender,
            func.count().label("cnt"),
            func.row_number()
            .over(partition_by=Speaker.profile_id, order_by=func.count().desc())
            .label("rn"),
        ).filter(Speaker.profile_id.in_(ids), Speaker.predicted_gender.isnot(None))
        if confirmed_only:
            query = query.filter(Speaker.gender_confirmed_by_user.is_(True))
        sub = query.group_by(Speaker.profile_id, Speaker.predicted_gender).subquery()
        for pid, gender in (
            db.query(sub.c.profile_id, sub.c.predicted_gender).filter(sub.c.rn == 1).all()
        ):
            result[int(pid)] = str(gender)
    return result


def organizations_with_voiceprints_off(
    db: Session, user_id: int, organization_ids: Iterable[int | None]
) -> set[int | None]:
    """Which of a user's organization scopes (None = personal) have voiceprints off."""
    view = PrivacyView(db)
    return {
        org for org in set(organization_ids) if not view.voiceprints_for_scope(user_id, org).enabled
    }
