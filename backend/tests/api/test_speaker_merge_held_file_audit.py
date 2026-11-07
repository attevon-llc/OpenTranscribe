"""A speaker merge on a held file is audited like every other transcript edit (#1307).

Merging moves every transcript segment of the source speaker onto the target, which is a
bulk edit of the file's transcript. A hold blocks destruction, not correction, so the merge
goes ahead; what the hold changes is that the edit is on the record, staged in the merge's own
transaction (the transactional outbox, issue #1329). Driven through the real route against
savepoint-isolated rows; the hold lookup is the seam, and the event is read back from the
``audit_outbox`` row the merge committed.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg

from sqlalchemy import text

from app.auth.audit_events import AuditEventType
from app.models.media import TranscriptSegment
from app.services import content_hold_service
from app.services.hold_guard import HoldState
from tests.fixtures.speaker_cleanup_seams import make_pair
from tests.fixtures.speaker_cleanup_seams import make_speaker

SECRET_TEXT = "the merger closes on friday"


def _edit_events(db_session, file_uuid: str) -> list[dict]:
    """The ``content.transcript.edit`` events staged for ``file_uuid``, as their payloads."""
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
    db_session.add(
        TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=file_a.id,
            speaker_id=source.id,
            start_time=0.0,
            end_time=1.0,
            text=SECRET_TEXT,
        )
    )
    db_session.commit()
    state = {file_a.id: HoldState(platform_flag=True)} if held else {}
    monkeypatch.setattr(content_hold_service, "hold_states", lambda db, ids: state)
    ids = (str(file_a.uuid), str(source.uuid), str(target.uuid))
    headers = {k: v for k, v in headers.items() if not k.startswith("_")}
    response = client.post(f"/api/speakers/{ids[1]}/merge/{ids[2]}", headers=headers)
    return response, ids


def test_merging_speakers_on_a_held_file_commits_a_transcript_edit_event(
    client, user_token_headers, db_session, normal_user, speaker_os_seams, monkeypatch
):
    response, (file_uuid, source_uuid, target_uuid) = _merge(
        client,
        user_token_headers,
        db_session,
        normal_user,
        speaker_os_seams,
        monkeypatch,
        held=True,
    )

    assert response.status_code == 200, response.text
    events = _edit_events(db_session, file_uuid)
    (event,) = events
    assert event["resource_type"] == "media_file"
    assert event["resource_uuid"] == file_uuid
    assert event["details"]["kind"] == "speaker_merge"
    assert sorted(event["details"]["speaker_uuids"]) == sorted([source_uuid, target_uuid])
    assert event["details"]["held_by"] == ["platform"]
    assert SECRET_TEXT not in json.dumps(events, default=str)


def test_merging_speakers_on_a_free_file_writes_no_event(
    client, user_token_headers, db_session, normal_user, speaker_os_seams, monkeypatch
):
    """Control: the same merge on a file nobody holds is not a governance event."""
    response, (file_uuid, _source, _target) = _merge(
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
