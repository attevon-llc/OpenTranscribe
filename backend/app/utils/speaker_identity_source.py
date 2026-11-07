"""Where a speaker's identity came from: a person, or a voiceprint (issue #1280).

``Speaker.identity_source`` records who wrote the speaker's ``display_name`` and
``profile_id``. It is the only way to tell a name a voiceprint match wrote from a name a
person typed or confirmed: an auto-accepted voice match and a person's Inbox accept leave a
byte-identical row otherwise (``display_name``, ``verified = True``, ``profile_id`` and the
suggestion fields all equal). The privacy policy needs the difference, because switching
voiceprints off hides what a voiceprint produced and a purge deletes it, while a person's
assertion is never touched.

Values (enforced by ``ck_speaker_identity_source``, migration ``v449``):

======================  ==========================================================  =========================
value                   meaning                                                     while voiceprints are off
======================  ==========================================================  =========================
``voiceprint``          a voiceprint path wrote the name AND the profile link       name and link hidden
``voiceprint_link``     a voiceprint path added only the link; the name was there   link hidden, name shown
``human``               a person typed, accepted, assigned or confirmed it          nothing hidden
``NULL``                written before v449 (unknown), or never named               nothing hidden (= human)
======================  ==========================================================  =========================

``human`` is recorded rather than left NULL so that NULL keeps meaning "we do not know":
a later, opt-in classification of legacy rows remains possible. Every reader treats
``human`` and NULL identically.

Every writer of ``display_name`` / ``profile_id`` goes through one of the ``record_*``
helpers below (or ``after_unlink``); they are pure functions on the row and never touch the
database. Readers ask :func:`hides_name` / :func:`hides_link` / :func:`visible_profile_id`.

What is recorded with it (issue #1280 follow-up). The same helpers also stamp, on the same
row, ``identity_source_at`` (when the current source was recorded), ``identity_source_user_id``
(the person who made or confirmed it; NULL for a task or when unknown) and
``identity_source_detail`` (the origin: see :mod:`app.utils.speaker_identity_detail`). Every
writer of ``suggested_name`` / ``suggestion_source`` goes through :func:`record_suggestion`,
:func:`clear_suggestion` or :func:`mark_review_skipped`, which keep ``suggestion_detail`` in
step. This is the CURRENT state only.

The append-only history (issue #1336) hangs off the same helpers. They stay pure, but each also
leaves a small attribution note on the row (:data:`HISTORY_NOTES_KEY` in the row's SQLAlchemy
``InstanceState.info``: the person, an explicit change kind, an origin record), which the flush
listener in :mod:`app.models.speaker_identity_event` turns into a ``speaker_identity_event`` row
in the same transaction. The helpers that do not re-record the source (:func:`after_unlink`,
:func:`after_name_cleared`, :func:`clear_suggestion`, :func:`mark_review_skipped`,
:func:`after_skip`) therefore take a REQUIRED ``user_id``: every caller states who acted
(``None`` = a task). :func:`record_profile_rename` and :func:`record_speaker_merge` exist only to
attribute changes that leave the source as it is.

While a file's voiceprints are off, the provenance of a machine identity (and the detail of a
voiceprint suggestion) is withheld exactly like the name and link it describes
(:func:`hides_provenance`); a person's own record is never withheld.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm.attributes import flag_dirty

from app.utils.speaker_identity_detail import ORIGIN_ACCEPTED_SUGGESTION
from app.utils.speaker_identity_detail import ORIGIN_USER_SKIPPED
from app.utils.speaker_identity_detail import ORIGIN_VOICE_SUGGESTION_SKIPPED
from app.utils.speaker_identity_detail import accepted_suggestion_snapshot
from app.utils.speaker_identity_detail import human_detail
from app.utils.speaker_identity_detail import voiceprint_detail
from app.utils.speaker_identity_history import ORIGIN_SPEAKER_MERGED
from app.utils.speaker_name_rules import is_assigned_speaker_name
from app.utils.speaker_name_rules import is_embedding_suggestion_source

VOICEPRINT = "voiceprint"
VOICEPRINT_LINK = "voiceprint_link"
HUMAN = "human"

#: Every value the CHECK admits (NULL aside). ``tests/unit/test_v449_migration_consistency.py``
#: pins the migration's CHECK and the model's to this set.
IDENTITY_SOURCES: frozenset[str] = frozenset({VOICEPRINT, VOICEPRINT_LINK, HUMAN})

#: Values that mean a voiceprint path wrote at least the profile link.
MACHINE_IDENTITY_SOURCES: frozenset[str] = frozenset({VOICEPRINT, VOICEPRINT_LINK})

#: ``system_settings`` key under which ``v449`` records how many existing rows its backfill
#: recognised as voiceprint-made (owner decision Q1: the count is quoted by the upgrade note).
#: Value: JSON ``{"revision": ..., "stamped": <int>, "recorded_at": <timestamp>}``.
BACKFILL_RECORD_SETTING_KEY = "privacy.identity_source_backfill"

#: Key of the attribution notes in a row's ``InstanceState.info`` (consumed at flush).
HISTORY_NOTES_KEY = "speaker_identity_history_notes"


def _note(speaker: Any, **note: Any) -> None:
    """Leave who acted (and, optionally, what kind of change it was) for the history row.

    A plain object (a unit test's stand-in) has no instance state and is never flushed, so there
    is nothing to note. The row is marked dirty so a change that writes no column (a speaker
    merge, a skip) still reaches the flush listener.
    """
    state = sa_inspect(speaker, raiseerr=False)
    if state is None:
        return
    state.info.setdefault(HISTORY_NOTES_KEY, []).append(
        {k: v for k, v in note.items() if v is not None}
    )
    if state.session_id is not None and state.key is not None:
        flag_dirty(speaker)


def _stamp(
    speaker: Any, source: str | None, *, user_id: int | None, detail: dict[str, Any] | None
) -> None:
    """Write ``identity_source`` and the three fields that describe it, together."""
    speaker.identity_source = source
    if source is None:
        speaker.identity_source_at = None
        speaker.identity_source_user_id = None
        speaker.identity_source_detail = None
        return
    speaker.identity_source_at = datetime.now(UTC)
    speaker.identity_source_user_id = user_id
    speaker.identity_source_detail = detail


def record_machine_identity(
    speaker: Any, *, previous_display_name: str | None, detail: dict[str, Any]
) -> None:
    """A voiceprint path just wrote this speaker's name and profile link.

    ``previous_display_name`` is the ``display_name`` BEFORE the write. When it already
    named somebody and the machine wrote the same name back (case-insensitively), the
    machine only added the link: the row becomes ``voiceprint_link`` and the name stays
    the person's. A row already marked ``voiceprint`` stays so.

    ``detail`` is built by :func:`app.utils.speaker_identity_detail.voiceprint_detail`. No
    person is recorded (``identity_source_user_id`` is NULL: a task wrote it).

    Known, accepted edge: a re-match that overwrites a DIFFERENT, typed name (existing
    matcher behaviour) is recorded as ``voiceprint``; the name it wrote is the machine's.
    """
    new_name = speaker.display_name
    if speaker.identity_source == VOICEPRINT:
        source = VOICEPRINT
    elif (
        is_assigned_speaker_name(previous_display_name)
        and isinstance(new_name, str)
        and str(previous_display_name).strip().casefold() == new_name.strip().casefold()
    ):
        source = VOICEPRINT_LINK
    else:
        source = VOICEPRINT
    _stamp(speaker, source, user_id=None, detail=detail)


def record_machine_link(speaker: Any, *, detail: dict[str, Any]) -> None:
    """A voiceprint path linked this speaker to a profile without writing its name.

    The row now records the machine's link, so the person who typed the name that was already
    there is no longer named in it (the earlier record stays in the identity history, #1336).
    """
    source = VOICEPRINT if speaker.identity_source == VOICEPRINT else VOICEPRINT_LINK
    _stamp(speaker, source, user_id=None, detail=detail)


def record_human_identity(
    speaker: Any,
    *,
    user_id: int | None,
    origin: str,
    origin_task: str | None = None,
    keep_recorded: bool = False,
) -> None:
    """A person typed, accepted, assigned or confirmed this speaker's name or link.

    Args:
        speaker: The row.
        user_id: The person (``user.id``), or ``None`` when no person's id is available (a
            system path acting for a person who cannot be named).
        origin: One of ``speaker_identity_detail.HUMAN_ORIGINS``.
        origin_task: The Celery task, when the write happens in one.
        keep_recorded: A later step of the SAME action (the background profile assignment that
            follows a rename) must not overwrite the record the request already made, which
            holds the accepted suggestion the request has since cleared. Honoured only when the
            speaker is already ``human`` with a timestamp.
    """
    _note(speaker, user_id=user_id)
    if keep_recorded and speaker.identity_source == HUMAN and speaker.identity_source_at:
        return
    detail = human_detail(
        origin,
        accepted_suggestion=accepted_suggestion_snapshot(
            speaker, require_name_match=origin != ORIGIN_ACCEPTED_SUGGESTION
        ),
        origin_task=origin_task,
    )
    _stamp(speaker, HUMAN, user_id=user_id, detail=detail)


def after_unlink(speaker: Any, *, user_id: int | None) -> None:
    """The speaker's profile link was just removed; settle what the remaining name is.

    ``user_id`` is the person who removed it (``None``: a task), for the history.

    An emptied name leaves nothing to describe (NULL). A ``voiceprint_link`` row's
    remaining name predates the link the machine added, so the row reverts to NULL
    (unknown, read as a person's). Anything else is unchanged: a ``voiceprint`` row still
    carries the machine-written name. Reverting to NULL clears the recorded time, person and
    detail with it: there is no identity left to describe.
    """
    _note(speaker, user_id=user_id)
    name_left = is_assigned_speaker_name(speaker.display_name)
    if not name_left or speaker.identity_source == VOICEPRINT_LINK:
        _stamp(speaker, None, user_id=None, detail=None)


def after_name_cleared(speaker: Any, *, user_id: int | None) -> None:
    """A person emptied the speaker's name without unlinking its profile.

    ``user_id`` is that person, for the history.

    With no link left there is no identity to describe (NULL). With a link left, a
    ``voiceprint`` row keeps the machine's link and becomes ``voiceprint_link`` (the name the
    machine wrote is gone; the link stays hideable and purgeable); a ``human`` or
    ``voiceprint_link`` row is unchanged. A name still present is not a clearing.
    """
    _note(speaker, user_id=user_id)
    if is_assigned_speaker_name(speaker.display_name):
        return
    if speaker.profile_id is None:
        _stamp(speaker, None, user_id=None, detail=None)
    elif speaker.identity_source == VOICEPRINT:
        speaker.identity_source = VOICEPRINT_LINK


def after_skip(speaker: Any, *, user_id: int | None) -> None:
    """A person dismissed this speaker from the Inbox, which sets ``verified`` without choosing a name.

    A skip is a review marker, not a confirmation, so it must not turn a voiceprint's
    suggestion into something a person vouched for. A speaker that carries such a suggestion
    and has no name or profile link of its own is marked ``voiceprint``: the privacy rules then
    treat its ``verified`` flag and its suggestion as the voiceprint's (hidden while off,
    cleared by a purge, which returns it to the Inbox). A speaker that already has a name or
    a link keeps the provenance whoever wrote it recorded.

    ``user_id`` is the person who skipped, for the history (the row records the skip as theirs).
    """
    _note(speaker, user_id=user_id, kind="skipped")
    if not is_embedding_suggestion_source(speaker.suggestion_source):
        return
    if speaker.profile_id is not None or is_assigned_speaker_name(speaker.display_name):
        return
    _stamp(
        speaker,
        VOICEPRINT,
        user_id=None,
        detail=voiceprint_detail(
            ORIGIN_VOICE_SUGGESTION_SKIPPED, confidence=getattr(speaker, "confidence", None)
        ),
    )


def is_person_verified(speaker: Any) -> bool:
    """Is this speaker's identity a person's word: verified, and not made by a voiceprint path?

    ``verified`` alone does not say who verified it (an auto-accepted match sets it too), so a
    machine identity (``voiceprint`` / ``voiceprint_link``) is never a person's. NULL reads as a
    person's, like everywhere else.
    """
    return (
        getattr(speaker, "verified", None) is True
        and getattr(speaker, "identity_source", None) not in MACHINE_IDENTITY_SOURCES
    )


def record_suggestion(
    speaker: Any,
    *,
    source: str,
    name: str | None,
    confidence: float | None,
    detail: dict[str, Any],
    identity_follows: bool = False,
) -> bool:
    """Store a recommendation and where it came from, together.

    The only way a recommendation is written: ``suggested_name``, ``suggestion_source``,
    ``confidence`` and ``suggestion_detail`` always agree. ``detail`` is built by
    :func:`app.utils.speaker_identity_detail.voiceprint_detail` (``voice_match`` /
    ``profile_match``) or :func:`~app.utils.speaker_identity_detail.llm_detail`
    (``llm_analysis``). A suggestion is never applied by this function.

    A voiceprint suggestion (``voice_match`` / ``profile_match``) is NOT recorded over a
    speaker a person verified: it would put a voice comparison on a person's own record, where
    switching voiceprints off hides it only by an extra rule and a purge has to find it
    separately. Pass ``identity_follows=True`` when the caller replaces the identity with the
    voiceprint's in the same step (an auto-accept), so the row is the machine's by the end.

    Returns:
        ``True`` when the recommendation was written, ``False`` when it was refused.
    """
    if (
        is_embedding_suggestion_source(source)
        and not identity_follows
        and is_person_verified(speaker)
    ):
        return False
    speaker.suggested_name = name
    speaker.suggestion_source = source
    speaker.confidence = confidence
    speaker.suggestion_detail = detail
    return True


def clear_suggestion(speaker: Any, *, user_id: int | None, rejected: bool = False) -> None:
    """Withdraw the pending recommendation: the name, its confidence and its recorded origin.

    ``user_id`` is the person who withdrew it (``None``: a task); ``rejected`` records the
    change as that person's explicit rejection rather than a side effect.

    ``suggestion_source`` is deliberately left alone, as every caller before this helper did:
    ``task_detection_service`` reads it standalone as "has LLM speaker identification already
    run on this file" and no display reader trusts it without a ``suggested_name``.
    """
    _note(speaker, user_id=user_id, kind="suggestion_rejected" if rejected else None)
    speaker.suggested_name = None
    speaker.confidence = None
    speaker.suggestion_detail = None


def mark_review_skipped(speaker: Any, *, user_id: int | None) -> None:
    """An Inbox skip: remember the speaker was reviewed, but only when no source is recorded.

    Overwriting a real source (``llm_analysis``) would erase the marker
    ``task_detection_service`` reads. The detail says only that a person skipped it. The skip is
    recorded in the history as ``user_id``'s either way.
    """
    _note(speaker, user_id=user_id, kind="skipped")
    if speaker.suggestion_source:
        return
    speaker.suggestion_source = "user_skipped"
    speaker.suggestion_detail = {"origin": ORIGIN_USER_SKIPPED}


def record_profile_rename(speaker: Any, *, user_id: int | None) -> None:
    """A person renamed the profile this speaker is linked to, and the speaker shows the new name.

    Who made the speaker's identity does not change (the source, person and detail stay); the
    history records the rename as ``user_id``'s.
    """
    _note(speaker, user_id=user_id)


def record_speaker_merge(speaker: Any, *, user_id: int | None, merged: Any) -> None:
    """``speaker`` absorbed the segments of ``merged``, which is then deleted with its history.

    The identity is unchanged, so this writes only the history row (``merge``): the absorbed
    speaker's uuid, and the name it showed. A name a voiceprint wrote is not copied (it is
    voiceprint output a purge must be able to remove); the diarizer label stands in for it.
    """
    name = merged.display_name
    if not is_assigned_speaker_name(name) or merged.identity_source == VOICEPRINT:
        name = merged.name
    _note(
        speaker,
        user_id=user_id,
        kind="merge",
        before={"merged_name": name},
        detail={"origin": ORIGIN_SPEAKER_MERGED, "merged_speaker_uuid": str(merged.uuid)},
    )


def cleared_provenance_values() -> dict[str, None]:
    """The columns a BULK ``UPDATE ... SET`` nulls to forget an identity's record.

    For the audited purge, which reverts machine identities in SQL rather than through the
    row helpers: ``identity_source`` and everything that describes it (time, person, detail)
    go together, never one without the others.
    """
    return {
        "identity_source": None,
        "identity_source_at": None,
        "identity_source_user_id": None,
        "identity_source_detail": None,
    }


def cleared_suggestion_values(where: Any = None) -> dict[str, Any]:
    """The columns a BULK ``UPDATE ... SET`` changes to withdraw a recommendation.

    ``suggested_name``, ``suggestion_source``, ``confidence`` and ``suggestion_detail`` always
    clear together. ``where`` is an optional SQL condition: rows that satisfy it are cleared and
    the others keep their value (the purge clears a voiceprint suggestion and keeps an LLM one).
    ``None`` clears unconditionally.
    """
    from sqlalchemy import case

    from app.models.speaker import Speaker

    columns = {
        "suggested_name": Speaker.suggested_name,
        "suggestion_source": Speaker.suggestion_source,
        "confidence": Speaker.confidence,
        "suggestion_detail": Speaker.suggestion_detail,
    }
    if where is None:
        return {name: None for name in columns}
    return {name: case((where, None), else_=column) for name, column in columns.items()}


def hides_name(identity_source: str | None, voiceprints_enabled: bool) -> bool:
    """Must the speaker's ``display_name`` be withheld under this switch?"""
    return not voiceprints_enabled and identity_source == VOICEPRINT


def hides_provenance(identity_source: str | None, voiceprints_enabled: bool) -> bool:
    """Must the speaker's identity time, person and detail be withheld under this switch?

    The detail of a machine identity names the profile or speaker the voice matched, so it is
    hidden with the link it describes; a person's own record is never hidden.
    """
    return hides_link(identity_source, voiceprints_enabled)


def hides_link(identity_source: str | None, voiceprints_enabled: bool) -> bool:
    """Must the speaker's profile link be withheld under this switch?"""
    return not voiceprints_enabled and identity_source in MACHINE_IDENTITY_SOURCES


def visible_profile_id(
    profile_id: int | None, identity_source: str | None, voiceprint_names: bool
) -> int | None:
    """The profile link a reader may show or index: ``None`` when a voiceprint made it and
    the file's voiceprints are off."""
    if profile_id is None or hides_link(identity_source, voiceprint_names):
        return None
    return profile_id
