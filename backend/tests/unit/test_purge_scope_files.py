"""``PurgeScope(kind="files")``: validation, selection, payload, and the retention exemption."""

from __future__ import annotations

import uuid

import pytest

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.services.privacy import purge
from app.services.privacy.purge import PurgeScope


def test_files_needs_ids():
    with pytest.raises(ValueError):
        PurgeScope(kind="files")


def test_files_names_no_tenant():
    with pytest.raises(ValueError):
        PurgeScope(kind="files", file_ids=(1,), organization_id=3)
    with pytest.raises(ValueError):
        PurgeScope(kind="files", file_ids=(1,), user_id=3)


def test_only_files_names_file_ids():
    with pytest.raises(ValueError):
        PurgeScope(kind="user", user_id=1, file_ids=(1,))


def test_ids_are_normalised_and_round_trip():
    scope = PurgeScope(kind="files", file_ids=(7, 3, 7))
    assert scope.file_ids == (3, 7)
    payload = scope.to_payload()
    assert payload["file_ids"] == [3, 7]
    assert PurgeScope.from_payload(payload) == scope
    # Pre-existing kinds keep their payload shape.
    assert "file_ids" not in PurgeScope(kind="user", user_id=1).to_payload()


def test_audit_details_count_files_and_name_none():
    scope = PurgeScope(kind="files", file_ids=(1, 2, 3))
    assert scope.audit_details() == {
        "kind": "files",
        "organization_id": None,
        "user_id": None,
        "file_count": 3,
    }


def _file(db, owner) -> MediaFile:
    row = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="scope.wav",
        storage_path="media/test/scope.wav",
        content_type="audio/wav",
        file_size=1,
        status="completed",
        is_public=False,
        user_id=owner.id,
    )
    db.add(row)
    db.flush()
    return row


def _speaker(db, owner, media_file) -> Speaker:
    row = Speaker(
        uuid=str(uuid.uuid4()), user_id=owner.id, media_file_id=media_file.id, name="SPEAKER_00"
    )
    db.add(row)
    db.flush()
    return row


def test_speaker_filters_select_only_those_files(db_session, normal_user):
    a, b = _file(db_session, normal_user), _file(db_session, normal_user)
    sa, sb = _speaker(db_session, normal_user, a), _speaker(db_session, normal_user, b)
    scope = PurgeScope(kind="files", file_ids=(a.id,))
    ids = {r.id for r in db_session.query(Speaker).filter(*scope.speaker_filters())}
    assert sa.id in ids
    assert sb.id not in ids
    files = {r.id for r in db_session.query(MediaFile).filter(*scope.sql_filters(MediaFile))}
    assert files == {a.id}


def test_sql_filters_match_no_profile(db_session, normal_user):
    profile = SpeakerProfile(uuid=str(uuid.uuid4()), user_id=normal_user.id, name="P")
    db_session.add(profile)
    db_session.flush()
    scope = PurgeScope(kind="files", file_ids=(1,))
    assert db_session.query(SpeakerProfile).filter(*scope.sql_filters(SpeakerProfile)).count() == 0
    assert db_session.query(SpeakerProfile).count() >= 1  # control: the table is not empty


def test_retention_is_the_only_exemption(db_session):
    scope = PurgeScope(kind="files", file_ids=(1,))
    assert (
        purge.targets_still_enabled(
            db_session, scope, [purge.VOICEPRINTS], purge_reason=purge.RETENTION
        )
        == []
    )
    # Without the reason a files scope is never authorised by a privacy switch.
    assert purge.targets_still_enabled(db_session, scope, [purge.VOICEPRINTS]) == [
        purge.VOICEPRINTS
    ]
    with pytest.raises(ValueError):
        purge.targets_still_enabled(db_session, scope, [purge.VOICEPRINTS], purge_reason="x")


def test_audit_event_carries_scope_and_reason(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(purge.audit_logger, "log", lambda **kw: events.append(kw))
    purge.audit_purge(
        scope=PurgeScope(kind="files", file_ids=(4, 5)),
        targets=[purge.VOICEPRINTS],
        phase="pass_1_completed",
        actor_user_id=None,
        actor_username=None,
        task_id="t",
        purge_reason=purge.RETENTION,
    )
    assert len(events) == 1
    details = events[0]["details"]
    assert details["purge_scope"] == {
        "kind": "files",
        "organization_id": None,
        "user_id": None,
        "file_count": 2,
    }
    assert details["purge_reason"] == "retention"
    assert "reason" not in details  # the outcome reason is a different key
    assert events[0]["organization_id"] is None
