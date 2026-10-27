"""A speaker merge on a held file is audited like every other transcript edit (#1307).

Merging moves every transcript segment of the source speaker onto the target, which is a
bulk edit of the file's transcript. A hold blocks destruction, not correction, so the merge
goes ahead; what the hold changes is that the edit is on the record. Driven through the real
route against savepoint-isolated rows; the audit logger and the hold lookup are the seams.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg
from unittest.mock import MagicMock

import pytest

from app.auth.audit import AuditEventType
from app.models.media import TranscriptSegment
from app.services import content_audit
from app.services import content_hold_service
from app.services.hold_guard import HoldState
from tests.fixtures.speaker_cleanup_seams import make_pair
from tests.fixtures.speaker_cleanup_seams import make_speaker

SECRET_TEXT = "the merger closes on friday"


@pytest.fixture
def audit_log(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    log = MagicMock(name="audit_logger")
    monkeypatch.setattr(content_audit, "audit_logger", log)
    return log


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
    state = {file_a.id: HoldState(platform=True)} if held else {}
    monkeypatch.setattr(content_hold_service, "hold_states", lambda db, ids: state)
    ids = (str(file_a.uuid), str(source.uuid), str(target.uuid))
    headers = {k: v for k, v in headers.items() if not k.startswith("_")}
    response = client.post(f"/api/speakers/{ids[1]}/merge/{ids[2]}", headers=headers)
    return response, ids


def test_merging_speakers_on_a_held_file_writes_a_transcript_edit_event(
    client, user_token_headers, db_session, normal_user, speaker_os_seams, audit_log, monkeypatch
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
    events = [c.kwargs for c in audit_log.log.call_args_list]
    (event,) = [e for e in events if e["event_type"] is AuditEventType.CONTENT_TRANSCRIPT_EDIT]
    assert event["resource_type"] == "media_file"
    assert event["resource_uuid"] == file_uuid
    assert event["details"]["kind"] == "speaker_merge"
    assert sorted(event["details"]["speaker_uuids"]) == sorted([source_uuid, target_uuid])
    assert event["details"]["held_by"] == ["platform"]
    assert SECRET_TEXT not in json.dumps(events, default=str)


def test_merging_speakers_on_a_free_file_writes_no_event(
    client, user_token_headers, db_session, normal_user, speaker_os_seams, audit_log, monkeypatch
):
    """Control: the same merge on a file nobody holds is not a governance event."""
    response, _ids = _merge(
        client,
        user_token_headers,
        db_session,
        normal_user,
        speaker_os_seams,
        monkeypatch,
        held=False,
    )

    assert response.status_code == 200, response.text
    assert [
        c
        for c in audit_log.log.call_args_list
        if c.kwargs["event_type"] is AuditEventType.CONTENT_TRANSCRIPT_EDIT
    ] == []
