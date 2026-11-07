"""A speaker merge on a held file is refused and the refusal audited (legal-hold freeze).

Merging moves every transcript segment of the source speaker onto the target, which is a bulk
edit of the file's transcript. Before v0.7.0 a hold blocked destruction only and the merge went
ahead with a ``content.transcript.edit`` record; a hold now freezes the file's content, so the
merge is refused with ``409 FILE_UNDER_LEGAL_HOLD``, nothing moves, and ``content.hold.refused``
(action ``speaker_merge``) is written instead. Driven through the real route against
savepoint-isolated rows; the hold lookup is the seam.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg

from sqlalchemy import text

from app.api import hold_refusal
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.services.hold_guard import LEGAL_HOLD_ERROR_CODE
from app.services.hold_guard import HoldState
from tests.fixtures.speaker_cleanup_seams import make_pair
from tests.fixtures.speaker_cleanup_seams import make_speaker

SECRET_TEXT = "the merger closes on friday"


def _edit_events(db_session, file_uuid: str) -> list[dict]:
    """``content.transcript.edit`` rows for ``file_uuid``: historic, no longer emitted."""
    return [
        row.payload
        for row in db_session.execute(
            text(
                "SELECT payload FROM audit_outbox WHERE event_type = :t "
                "AND payload->>'resource_uuid' = :f"
            ),
            {"t": AuditEventType.CONTENT_TRANSCRIPT_EDIT.value, "f": file_uuid},
        )
    ]


def _merge(client, headers, db_session, normal_user, seams, monkeypatch, *, held: bool):
    file_a, _fb, source, _sb, _cluster = make_pair(db_session, normal_user, seams)
    target = make_speaker(db_session, normal_user, file_a, name="SPEAKER_01")
    segment = TranscriptSegment(
        uuid=uuid_pkg.uuid4(),
        media_file_id=file_a.id,
        speaker_id=source.id,
        start_time=0.0,
        end_time=1.0,
        text=SECRET_TEXT,
    )
    db_session.add(segment)
    db_session.commit()
    state = {file_a.id: HoldState(platform_flag=True)} if held else {}
    monkeypatch.setattr(
        hold_refusal, "hold_states", lambda db, ids: {i: state[i] for i in ids if i in state}
    )
    events: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: events.append(kw))
    ids = (str(file_a.uuid), str(source.uuid), str(target.uuid))
    headers = {k: v for k, v in headers.items() if not k.startswith("_")}
    response = client.post(f"/api/speakers/{ids[1]}/merge/{ids[2]}", headers=headers)
    return response, ids, events, segment.id, source.id


def test_merging_speakers_on_a_held_file_is_refused_and_audited(
    client, user_token_headers, db_session, normal_user, speaker_os_seams, monkeypatch
):
    response, (file_uuid, source_uuid, _target), events, segment_id, source_id = _merge(
        client,
        user_token_headers,
        db_session,
        normal_user,
        speaker_os_seams,
        monkeypatch,
        held=True,
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == LEGAL_HOLD_ERROR_CODE
    assert detail["held_by"] == ["platform"]
    db_session.expire_all()
    assert db_session.get(TranscriptSegment, segment_id).speaker_id == source_id
    assert db_session.query(Speaker).filter(Speaker.uuid == source_uuid).count() == 1
    refused = [e for e in events if e["event_type"] is AuditEventType.CONTENT_HOLD_REFUSED]
    (event,) = refused
    assert event["resource_type"] == "media_file"
    assert event["resource_uuid"] == file_uuid
    assert event["details"] == {"action": "speaker_merge", "held_by": ["platform"]}
    assert _edit_events(db_session, file_uuid) == []
    assert SECRET_TEXT not in json.dumps(events, default=str)


def test_merging_speakers_on_a_free_file_writes_no_event(
    client, user_token_headers, db_session, normal_user, speaker_os_seams, monkeypatch
):
    """Control: the same merge on a file nobody holds goes through and is not a hold event."""
    response, (file_uuid, _source, _target), events, _seg, _src = _merge(
        client,
        user_token_headers,
        db_session,
        normal_user,
        speaker_os_seams,
        monkeypatch,
        held=False,
    )

    assert response.status_code == 200, response.text
    assert _edit_events(db_session, file_uuid) == []
    assert not [e for e in events if e["event_type"] is AuditEventType.CONTENT_HOLD_REFUSED]
