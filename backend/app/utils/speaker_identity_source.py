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
"""

from __future__ import annotations

from typing import Any

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


def record_machine_identity(speaker: Any, *, previous_display_name: str | None) -> None:
    """A voiceprint path just wrote this speaker's name and profile link.

    ``previous_display_name`` is the ``display_name`` BEFORE the write. When it already
    named somebody and the machine wrote the same name back (case-insensitively), the
    machine only added the link: the row becomes ``voiceprint_link`` and the name stays
    the person's. A row already marked ``voiceprint`` stays so.

    Known, accepted edge: a re-match that overwrites a DIFFERENT, typed name (existing
    matcher behaviour) is recorded as ``voiceprint``; the name it wrote is the machine's.
    """
    if speaker.identity_source == VOICEPRINT:
        return
    new_name = speaker.display_name
    if (
        is_assigned_speaker_name(previous_display_name)
        and isinstance(new_name, str)
        and str(previous_display_name).strip().casefold() == new_name.strip().casefold()
    ):
        speaker.identity_source = VOICEPRINT_LINK
        return
    speaker.identity_source = VOICEPRINT


def record_machine_link(speaker: Any) -> None:
    """A voiceprint path linked this speaker to a profile without writing its name."""
    if speaker.identity_source == VOICEPRINT:
        return
    speaker.identity_source = VOICEPRINT_LINK


def record_human_identity(speaker: Any) -> None:
    """A person typed, accepted, assigned or confirmed this speaker's name or link."""
    speaker.identity_source = HUMAN


def after_unlink(speaker: Any) -> None:
    """The speaker's profile link was just removed; settle what the remaining name is.

    An emptied name leaves nothing to describe (NULL). A ``voiceprint_link`` row's
    remaining name predates the link the machine added, so the row reverts to NULL
    (unknown, read as a person's). Anything else is unchanged: a ``voiceprint`` row still
    carries the machine-written name.
    """
    name_left = is_assigned_speaker_name(speaker.display_name)
    if not name_left or speaker.identity_source == VOICEPRINT_LINK:
        speaker.identity_source = None


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
        speaker.identity_source = None
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
    speaker.identity_source = VOICEPRINT


def hides_name(identity_source: str | None, voiceprints_enabled: bool) -> bool:
    """Must the speaker's ``display_name`` be withheld under this switch?"""
    return not voiceprints_enabled and identity_source == VOICEPRINT


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
