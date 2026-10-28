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
step. This is the CURRENT state only; the append-only history is issue #1336 (v0.7.0).

While a file's voiceprints are off, the provenance of a machine identity (and the detail of a
voiceprint suggestion) is withheld exactly like the name and link it describes
(:func:`hides_provenance`); a person's own record is never withheld.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any

from app.utils.speaker_identity_detail import ORIGIN_ACCEPTED_SUGGESTION
from app.utils.speaker_identity_detail import ORIGIN_USER_SKIPPED
from app.utils.speaker_identity_detail import ORIGIN_VOICE_SUGGESTION_SKIPPED
from app.utils.speaker_identity_detail import accepted_suggestion_snapshot
from app.utils.speaker_identity_detail import human_detail
from app.utils.speaker_identity_detail import voiceprint_detail
from app.utils.speaker_labels import is_assigned_speaker_name
from app.utils.speaker_labels import is_embedding_suggestion_source

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
    there is no longer named in it (only the current state is kept; the history is issue #1336).
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


def after_unlink(speaker: Any) -> None:
    """The speaker's profile link was just removed; settle what the remaining name is.

    An emptied name leaves nothing to describe (NULL). A ``voiceprint_link`` row's
    remaining name predates the link the machine added, so the row reverts to NULL
    (unknown, read as a person's). Anything else is unchanged: a ``voiceprint`` row still
    carries the machine-written name. Reverting to NULL clears the recorded time, person and
    detail with it: there is no identity left to describe.
    """
    name_left = is_assigned_speaker_name(speaker.display_name)
    if not name_left or speaker.identity_source == VOICEPRINT_LINK:
        _stamp(speaker, None, user_id=None, detail=None)


def after_name_cleared(speaker: Any) -> None:
    """A person emptied the speaker's name without unlinking its profile.

    With no link left there is no identity to describe (NULL). With a link left, a
    ``voiceprint`` row keeps the machine's link and becomes ``voiceprint_link`` (the name the
    machine wrote is gone; the link stays hideable and purgeable); a ``human`` or
    ``voiceprint_link`` row is unchanged. A name still present is not a clearing.
    """
    if is_assigned_speaker_name(speaker.display_name):
        return
    if speaker.profile_id is None:
        _stamp(speaker, None, user_id=None, detail=None)
    elif speaker.identity_source == VOICEPRINT:
        speaker.identity_source = VOICEPRINT_LINK


def after_skip(speaker: Any) -> None:
    """A person dismissed this speaker from the Inbox, which sets ``verified`` without choosing a name.

    A skip is a review marker, not a confirmation, so it must not turn a voiceprint's
    suggestion into something a person vouched for. A speaker that carries such a suggestion
    and has no name or profile link of its own is marked ``voiceprint``: the privacy rules then
    treat its ``verified`` flag and its suggestion as the voiceprint's (hidden while off,
    cleared by a purge, which returns it to the Inbox). A speaker that already has a name or
    a link keeps the provenance whoever wrote it recorded.
    """
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


def record_suggestion(
    speaker: Any,
    *,
    source: str,
    name: str | None,
    confidence: float | None,
    detail: dict[str, Any],
) -> None:
    """Store a recommendation and where it came from, together.

    The only way a recommendation is written: ``suggested_name``, ``suggestion_source``,
    ``confidence`` and ``suggestion_detail`` always agree. ``detail`` is built by
    :func:`app.utils.speaker_identity_detail.voiceprint_detail` (``voice_match`` /
    ``profile_match``) or :func:`~app.utils.speaker_identity_detail.llm_detail`
    (``llm_analysis``). A suggestion is never applied by this function.
    """
    speaker.suggested_name = name
    speaker.suggestion_source = source
    speaker.confidence = confidence
    speaker.suggestion_detail = detail


def clear_suggestion(speaker: Any) -> None:
    """Withdraw the pending recommendation: the name, its confidence and its recorded origin.

    ``suggestion_source`` is deliberately left alone, as every caller before this helper did:
    ``task_detection_service`` reads it standalone as "has LLM speaker identification already
    run on this file" and no display reader trusts it without a ``suggested_name``.
    """
    speaker.suggested_name = None
    speaker.confidence = None
    speaker.suggestion_detail = None


def mark_review_skipped(speaker: Any) -> None:
    """An Inbox skip: remember the speaker was reviewed, but only when no source is recorded.

    Overwriting a real source (``llm_analysis``) would erase the marker
    ``task_detection_service`` reads. The detail says only that a person skipped it.
    """
    if speaker.suggestion_source:
        return
    speaker.suggestion_source = "user_skipped"
    speaker.suggestion_detail = {"origin": ORIGIN_USER_SKIPPED}


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

    from app.models.media import Speaker

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
