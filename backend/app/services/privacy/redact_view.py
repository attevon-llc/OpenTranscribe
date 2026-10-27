"""Read-side enforcement of the privacy policy: what a response may still show (issue #1280).

Turning a privacy layer off HIDES existing data and never deletes it (plan D5), so every read
that could surface a voiceprint-derived or inferred value passes through here. The policy
itself (who may have what) is :mod:`app.services.privacy.policy`; this module only asks it and
shapes the answer.

Two rules govern what is hidden while a scope's switch is off:

* **Voiceprints off**: kNN suggestion reads return nothing, and a *stored* suggestion that a
  voiceprint wrote (``suggestion_source`` in ``voice_match`` / ``profile_match``) and nobody
  verified is omitted. Named profiles, avatars and user-verified links are user assertions,
  not biometric data, and stay.
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
from app.services.privacy.policy import Decision
from app.utils.speaker_labels import is_embedding_suggestion_source

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.media import Speaker

logger = logging.getLogger(__name__)

#: Error code of the 409 a voiceprint-creating or voiceprint-recomputing request gets while
#: its scope is off. The reason (``locked_by``) rides in the same body (plan D8).
VOICEPRINTS_DISABLED_CODE = "voiceprints_disabled"

_DENY = Decision(False, "instance")

Scope = tuple[int, int | None]

#: Keys of ``Speaker.attribute_confidence`` that are derived from the inferred gender (the
#: model's confidence, and the voice-versus-metadata alignment judged from it).
_INFERRED_GENDER_KEYS = frozenset({"gender", "gender_alignment", "gender_alignment_hint"})


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
        self._voiceprints: dict[Scope, Decision] = {}
        self._gender: dict[Scope, Decision] = {}

    def preload_files(self, file_ids: Iterable[int]) -> None:
        """Read the scope of every not-yet-known file in one query. Unreadable = unknown."""
        from app.models.media import MediaFile

        wanted = {int(f) for f in file_ids if f is not None} - set(self._file_scope)
        if not wanted:
            return
        try:
            rows = (
                self._db.query(MediaFile.id, MediaFile.user_id, MediaFile.organization_id)
                .filter(MediaFile.id.in_(wanted))
                .all()
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
        """May voiceprints exist for this scope? Layers plus the ``create`` consent hooks."""
        scope = (int(user_id), organization_id)
        if scope not in self._voiceprints:
            self._voiceprints[scope] = policy.voiceprint_decision(
                self._db,
                user_id=scope[0],
                organization_id=scope[1],
                media_file_id=media_file_id,
            )
        return self._voiceprints[scope]

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


def hides_voiceprint_suggestion(speaker: Speaker) -> bool:
    """A stored suggestion a voiceprint wrote that nobody verified (the D5 predicate)."""
    return (
        is_embedding_suggestion_source(speaker.suggestion_source) and speaker.verified is not True
    )


def redact_speaker_rows(db: Session | PrivacyView, speakers: Iterable[Speaker]) -> None:
    """Hide, on the loaded rows, what the owner's current policy forbids showing.

    Call it after the last mutation and before the rows are serialized. The rows are changed
    with ``set_committed_value``: the in-memory object reads redacted, the database row is
    untouched and the attribute is never flushed.
    """
    view = db if isinstance(db, PrivacyView) else PrivacyView(db)
    rows = list(speakers)
    view.preload_files(int(s.media_file_id) for s in rows)
    for speaker in rows:
        file_id = int(speaker.media_file_id)
        if hides_voiceprint_suggestion(speaker) and not view.voiceprints(file_id).enabled:
            set_committed_value(speaker, "suggested_name", None)
            set_committed_value(speaker, "suggestion_source", None)
            set_committed_value(speaker, "confidence", None)
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
