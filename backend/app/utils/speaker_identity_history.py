"""What one change to a speaker's identity records in its history (issue #1336). Pure functions.

The flush listener in :mod:`app.models.speaker_identity_event` hands :func:`derive_event` the
speaker's changed fields (old and new value, from SQLAlchemy's attribute history), the row's
values after the change, and the attribution notes the writer left through the helpers of
:mod:`app.utils.speaker_identity_source`. It returns the row to append, or ``None`` when nothing
the history records changed. Nothing here touches the database.

What a row says
---------------
* ``kind``: what happened (``EVENT_KINDS`` in the model). A writer's explicit kind (a merge, a
  rejected suggestion, a purge) wins; otherwise it is read from the fields: a name beats a link,
  a link beats a suggestion, and a re-recorded source with no value change is a confirmation.
* who: the person the helper recorded (``identity_source_user_id`` on a re-recorded identity, or
  the ``user_id`` a helper was given); no person means ``system`` (a task or a model).
* which model: ``model`` and ``confidence`` from the origin record the helper stored
  (``identity_source_detail`` / ``suggestion_detail``: ids only, never a name), which is copied
  as ``detail``.
* ``voiceprint``: the change is something a voiceprint produced (a machine identity or link, or a
  voice-match suggestion). Those rows are withheld while the file's voiceprints are off and
  deleted by a voiceprint purge.
* ``before`` / ``after``: only the fields that changed. A value a voiceprint wrote is never copied
  into a row that is not itself a voiceprint row: it is named in ``withheld`` instead, so a purge
  that deletes the voiceprint rows leaves no copy behind.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.utils.speaker_identity_detail import ORIGIN_ACCEPTED_SUGGESTION
from app.utils.speaker_identity_detail import ORIGIN_CLUSTER_MERGED
from app.utils.speaker_identity_detail import ORIGIN_VOICE_SUGGESTION_SKIPPED
from app.utils.speaker_name_rules import is_assigned_speaker_name
from app.utils.speaker_name_rules import is_embedding_suggestion_source

#: The speaker columns whose changes are recorded (each mapped with ``active_history``).
TRACKED_FIELDS: tuple[str, ...] = (
    "display_name",
    "profile_id",
    "suggested_name",
    "suggestion_source",
    "identity_source",
)

#: Field name in ``before`` / ``after`` for each tracked column.
_VALUE_KEYS = {
    "display_name": "name",
    "profile_id": "profile_id",
    "suggested_name": "suggested_name",
    "suggestion_source": "suggestion_source",
    "identity_source": "identity_source",
}

_MACHINE_SOURCES = frozenset({"voiceprint", "voiceprint_link"})

#: Kinds that describe the recommendation, not the identity.
_SUGGESTION_KINDS = frozenset({"suggested", "suggestion_cleared", "suggestion_rejected", "skipped"})

ORIGIN_PURGE = "voiceprint_purge"
ORIGIN_SPEAKER_MERGED = "speaker_merged"


@dataclass(frozen=True)
class EventDraft:
    """The values of one history row, before it is stamped with the speaker and tenant."""

    kind: str
    actor_user_id: int | None
    origin: str | None
    model: str | None
    confidence: float | None
    voiceprint: bool
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    detail: dict[str, Any] | None

    @property
    def actor_kind(self) -> str:
        return "user" if self.actor_user_id is not None else "system"


def _note_value(notes: Sequence[Mapping[str, Any]], key: str) -> Any:
    """The last value a note gave for ``key`` (later notes describe the later step)."""
    for note in reversed(notes):
        if note.get(key) is not None:
            return note[key]
    return None


def _kind(
    changes: Mapping[str, tuple[Any, Any]], *, origin: str | None, restamped: bool
) -> str | None:
    if "display_name" in changes:
        if not is_assigned_speaker_name(changes["display_name"][1]):
            return "name_cleared"
        if origin == ORIGIN_ACCEPTED_SUGGESTION:
            return "suggestion_accepted"
        return "merge" if origin == ORIGIN_CLUSTER_MERGED else "rename"
    if "profile_id" in changes:
        if changes["profile_id"][1] is None:
            return "unlink"
        if origin == ORIGIN_ACCEPTED_SUGGESTION:
            return "suggestion_accepted"
        return "merge" if origin == ORIGIN_CLUSTER_MERGED else "link"
    if "suggested_name" in changes or "suggestion_source" in changes:
        new_name = changes.get("suggested_name", (None, None))[1]
        if new_name:
            return "suggested"
        if changes.get("suggestion_source", (None, None))[1] == "user_skipped":
            return "skipped"
        return "suggestion_cleared" if "suggested_name" in changes else None
    if restamped or "identity_source" in changes:
        return "skipped" if origin == ORIGIN_VOICE_SUGGESTION_SKIPPED else "confirmed"
    return None


def _withheld(field: str, old_row: Mapping[str, Any]) -> bool:
    """Was the OLD value of ``field`` written by a voiceprint?"""
    old_source = old_row.get("identity_source")
    if field == "display_name":
        return old_source == "voiceprint"
    if field == "profile_id":
        return old_source in _MACHINE_SOURCES
    if field == "suggested_name":
        return is_embedding_suggestion_source(old_row.get("suggestion_source"))
    return False


def _values(
    changes: Mapping[str, tuple[Any, Any]], old_row: Mapping[str, Any], *, voiceprint: bool
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    withheld: list[str] = []
    for field in TRACKED_FIELDS:
        if field not in changes:
            continue
        old, new = changes[field]
        key = _VALUE_KEYS[field]
        if not voiceprint and old is not None and _withheld(field, old_row):
            withheld.append(key)
        else:
            before[key] = old
        after[key] = new
    if withheld:
        before["withheld"] = withheld
    return (before or None), (after or None)


def _model(detail: Mapping[str, Any] | None) -> str | None:
    if not detail:
        return None
    model = detail.get("embedding_model") or detail.get("model")
    if model is None:
        accepted = detail.get("accepted_suggestion")
        llm = accepted.get("llm") if isinstance(accepted, Mapping) else None
        model = llm.get("model") if isinstance(llm, Mapping) else None
    return str(model)[:255] if model else None


def _confidence(detail: Mapping[str, Any] | None) -> float | None:
    value = detail.get("confidence") if detail else None
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def derive_event(
    changes: Mapping[str, tuple[Any, Any]],
    row: Mapping[str, Any],
    notes: Sequence[Mapping[str, Any]] = (),
    *,
    restamped: bool = False,
) -> EventDraft | None:
    """The history row one flush of one speaker writes, or ``None`` when there is nothing to say.

    Args:
        changes: Tracked field -> ``(old, new)``, only for fields whose value changed.
        row: The speaker's values AFTER the change (``identity_source``,
            ``identity_source_user_id``, ``identity_source_detail``, ``suggestion_source``,
            ``suggestion_detail``).
        notes: The attribution notes the helpers left (``user_id``, ``kind``, ``detail``,
            ``before``), oldest first.
        restamped: The helpers recorded a new identity source in this change
            (``identity_source_at`` was set), even when no value changed (a confirmation).
    """
    forced_kind = _note_value(notes, "kind")
    if not changes and not restamped and not forced_kind:
        return None

    identity_recorded = restamped and row.get("identity_source") is not None
    identity_detail = row.get("identity_source_detail") if identity_recorded else None
    identity_origin = identity_detail.get("origin") if identity_detail else None
    kind = forced_kind or _kind(changes, origin=identity_origin, restamped=restamped)
    if kind is None:
        return None

    note_detail = _note_value(notes, "detail")
    if note_detail is not None:
        detail = note_detail
    elif kind in _SUGGESTION_KINDS and kind != "skipped":
        detail = row.get("suggestion_detail") if kind == "suggested" else None
    else:
        detail = identity_detail

    actor = row.get("identity_source_user_id") if identity_recorded else None
    if actor is None:
        actor = _note_value(notes, "user_id")

    if kind == "suggested":
        voiceprint = is_embedding_suggestion_source(row.get("suggestion_source"))
    elif kind in ("skipped", "purge", "suggestion_cleared", "suggestion_rejected"):
        voiceprint = False
    else:
        # A machine identity stays a voiceprint row even when a person's action set it off (a
        # cluster join): the name and link it holds are the voiceprint's.
        voiceprint = identity_recorded and row.get("identity_source") in _MACHINE_SOURCES

    old_row = {
        "identity_source": changes.get("identity_source", (row.get("identity_source"),))[0],
        "suggestion_source": changes.get("suggestion_source", (row.get("suggestion_source"),))[0],
    }
    before, after = _values(changes, old_row, voiceprint=voiceprint)
    noted_before = _note_value(notes, "before")
    if noted_before:
        before = {**(before or {}), **noted_before}
    origin = detail.get("origin") if isinstance(detail, Mapping) else None
    return EventDraft(
        kind=kind,
        actor_user_id=int(actor) if actor is not None else None,
        origin=str(origin)[:64] if origin else None,
        model=_model(detail),
        confidence=_confidence(detail),
        voiceprint=voiceprint,
        before=before,
        after=after,
        detail=dict(detail) if isinstance(detail, Mapping) else None,
    )
