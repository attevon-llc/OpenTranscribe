"""Speaker identity history (issue #1336): the bulk writer, the purge's delete, and the reader.

ORM writes of a speaker's name, link or suggestion are recorded by the flush listener in
:mod:`app.models.speaker_identity_event`. A bulk ``UPDATE speaker`` bypasses the flush, so the
function that runs one records its rows here, in the same transaction
(``tests/unit/test_speaker_identity_source_discipline.py`` fails a bulk write that does not).

The rows are append-only in the database (``speaker_identity_event_guard``, v462). The only
deletes are the speaker's own ``ON DELETE CASCADE`` and :func:`forget_voiceprint_events`, which a
voiceprint purge runs under the ``opentranscribe.voiceprint_purge`` switch.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import bindparam
from sqlalchemy import delete
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import Integer

from app.models.speaker_identity_event import EVENT_KINDS
from app.models.speaker_identity_event import PURGE_SWITCH
from app.models.speaker_identity_event import SpeakerIdentityEvent

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from sqlalchemy.sql import Select


_BULK_INSERT = text(
    """
    INSERT INTO speaker_identity_event
        (uuid, speaker_id, organization_id, kind, actor_kind, actor_user_id, origin, voiceprint,
         detail, created_at)
    SELECT gen_random_uuid(), s.id, s.organization_id, :kind,
           CASE WHEN CAST(:user_id AS integer) IS NULL THEN 'system' ELSE 'user' END,
           CAST(:user_id AS integer), :origin, false, :detail, now()
    FROM speaker s WHERE s.id = ANY(:ids)
    """
).bindparams(
    bindparam("ids", type_=ARRAY(Integer)),
    bindparam("detail", type_=JSONB(none_as_null=True)),
)


def record_bulk_identity_events(
    db: Session,
    speaker_ids: Iterable[int],
    *,
    kind: str,
    user_id: int | None,
    detail: dict[str, Any],
) -> int:
    """Append one history row per speaker a bulk ``UPDATE`` changed. Does not commit.

    No before/after values: a bulk path does not read each row, and the only one (the voiceprint
    purge) removes values a voiceprint wrote, which must not be copied anywhere. ``detail`` is an
    origin record (ids only) with an ``origin`` key.

    Returns:
        The number of rows written.
    """
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown identity event kind {kind!r}")
    ids = sorted({int(i) for i in speaker_ids})
    if not ids:
        return 0
    result = db.execute(
        _BULK_INSERT,
        {
            "ids": ids,
            "kind": kind,
            "user_id": user_id,
            "origin": str(detail.get("origin") or "")[:64] or None,
            "detail": detail,
        },
    )
    return int(getattr(result, "rowcount", 0) or 0)


def forget_voiceprint_events(db: Session, speaker_ids: Select[Any]) -> int:
    """Delete the ``voiceprint`` history rows of these speakers (a voiceprint purge). No commit.

    The switch is set LOCAL to the transaction and put back right after, so nothing else that
    runs in it can delete history.

    Args:
        db: The purge's session.
        speaker_ids: A ``SELECT speaker.id`` of the purge's scope.

    Returns:
        The number of rows deleted.
    """
    previous = db.execute(
        text("SELECT coalesce(current_setting(:name, true), '')"), {"name": PURGE_SWITCH}
    ).scalar_one()
    db.execute(text("SELECT set_config(:name, 'on', true)"), {"name": PURGE_SWITCH})
    # A failed DELETE aborts the transaction, and the LOCAL setting ends with it.
    result = db.execute(
        delete(SpeakerIdentityEvent).where(
            SpeakerIdentityEvent.voiceprint.is_(True),
            SpeakerIdentityEvent.speaker_id.in_(speaker_ids),
        )
    )
    db.execute(
        text("SELECT set_config(:name, :value, true)"), {"name": PURGE_SWITCH, "value": previous}
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _profile_refs(db: Session, events: list[SpeakerIdentityEvent]) -> dict[int, dict[str, Any]]:
    """``profile_id`` -> ``{"uuid", "name"}`` for every profile the events name that still exists."""
    from app.models.speaker import SpeakerProfile

    wanted = {
        int(value["profile_id"])
        for e in events
        for value in (e.before_value, e.after_value)
        if value and isinstance(value.get("profile_id"), int)
    }
    if not wanted:
        return {}
    return {
        int(r[0]): {"uuid": str(r[1]), "name": r[2]}
        for r in db.query(SpeakerProfile.id, SpeakerProfile.uuid, SpeakerProfile.name)
        .filter(SpeakerProfile.id.in_(wanted))
        .all()
    }


def _public_values(
    value: dict[str, Any] | None, profiles: dict[int, dict[str, Any]]
) -> dict[str, Any] | None:
    """A stored before/after, with the internal profile id replaced by the profile's uuid/name."""
    if value is None:
        return None
    out = {k: v for k, v in value.items() if k != "profile_id"}
    if "profile_id" in value:
        pid = value["profile_id"]
        out["profile"] = (
            profiles.get(int(pid), {"uuid": None, "name": None, "deleted": True})
            if pid is not None
            else None
        )
    return out


def list_identity_history(
    db: Session, speaker: Any, viewer: Any, *, limit: int = 50
) -> list[dict[str, Any]]:
    """The speaker's history, newest first, as the viewer may see it.

    The caller has authorized the speaker (``require_speaker_access``: tenant, quarantine, share,
    platform grant). Here:

    * while the file's voiceprints are off, the ``voiceprint`` rows are withheld, exactly like
      the name, link and suggestion they describe (``hides_provenance``);
    * the person who acted is named only to a viewer who may see ``identity.by``
      (:func:`app.services.speaker_provenance_view.visible_actors`); otherwise ``actor.user`` is
      ``null`` and only ``actor.kind`` is given.

    Args:
        db: The request's session.
        speaker: The authorized speaker row.
        viewer: A ``ProvenanceViewer``.
        limit: At most this many rows.
    """
    from app.models.media import MediaFile
    from app.services.privacy.redact_view import PrivacyView
    from app.services.speaker_provenance_view import visible_actors

    query = db.query(SpeakerIdentityEvent).filter(SpeakerIdentityEvent.speaker_id == speaker.id)
    if not PrivacyView(db).voiceprints(int(speaker.media_file_id)).enabled:
        query = query.filter(SpeakerIdentityEvent.voiceprint.is_(False))
    events = (
        query.order_by(SpeakerIdentityEvent.created_at.desc(), SpeakerIdentityEvent.id.desc())
        .limit(limit)
        .all()
    )
    if not events:
        return []
    file_row = (
        db.query(MediaFile.user_id, MediaFile.organization_id, MediaFile.uuid)
        .filter(MediaFile.id == speaker.media_file_id)
        .one()
    )
    actors = visible_actors(
        db, viewer, tuple(file_row), (e.actor_user_id for e in events if e.actor_user_id)
    )
    profiles = _profile_refs(db, events)
    out: list[dict[str, Any]] = []
    for e in events:
        user = actors.get(int(e.actor_user_id)) if e.actor_user_id is not None else None
        out.append(
            {
                "uuid": e.uuid,
                "kind": e.kind,
                "at": e.created_at,
                "actor": {"kind": e.actor_kind, "user": user},
                "origin": e.origin,
                "model": e.model,
                "confidence": e.confidence,
                "before": _public_values(e.before_value, profiles),
                "after": _public_values(e.after_value, profiles),
            }
        )
    return out
