"""Speaker identity history (issue #1336): the bulk writer and the purge's delete.

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
