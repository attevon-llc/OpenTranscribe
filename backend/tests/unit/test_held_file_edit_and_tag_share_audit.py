"""Transcript edits on a held file are audited; tag-share events never carry the tag name (#1307).

Two gaps the content-governance work left:

1. A hold blocks destruction, not correction, so editing a held file's transcript stays
   allowed. "Allowed" must not mean "invisible": each edit made while the file is held now
   writes ``content.transcript.edit`` through the one content-audit write path.
2. Tag-share events still carried ``details.resource_name`` (the tag's name) after collection
   events had dropped theirs. A tag name is user text and may be sensitive; an audit log is
   read by people who may not see the tag.

In-process: the audit logger and the hold lookup are the seams, so a regression in either
the call sites or the event body fails here without a database.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any
from typing import cast
from unittest.mock import MagicMock

import pytest

from app.api.deps_context import RequestContext
from app.api.endpoints import transcript_segments
from app.api.endpoints.files import crud
from app.api.endpoints.tags import sharing
from app.auth.audit import AuditEventType
from app.schemas.media import TranscriptSegmentUpdate
from app.schemas.transcript import SegmentSpeakerUpdate
from app.services import content_audit
from app.services import content_hold_service
from app.services.content_hold_service import record_edit_on_held_file
from app.services.hold_guard import HoldState

pytestmark = pytest.mark.unit

SECRET_TEXT = "The merger closes on Friday"
SECRET_TITLE = "Confidential board call.mp4"
SECRET_TAG = "Acme layoffs 2026"


def _file(**overrides: Any) -> Any:
    base = {
        "id": 41,
        "uuid": uuid.uuid4(),
        "organization_id": 7,
        "user_id": 3,
        "title": SECRET_TITLE,
        "filename": SECRET_TITLE,
    }
    return SimpleNamespace(**{**base, **overrides})


def _actor() -> Any:
    return SimpleNamespace(id=3, email="owner@example.com")


@pytest.fixture
def audit_log(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    log = MagicMock(name="audit_logger")
    monkeypatch.setattr(content_audit, "audit_logger", log)
    return log


def _hold(monkeypatch: pytest.MonkeyPatch, file_id: int, state: HoldState | None) -> None:
    result = {file_id: state} if state is not None else {}
    monkeypatch.setattr(content_hold_service, "hold_states", lambda db, ids: result)


def _events(log: MagicMock) -> list[dict[str, Any]]:
    return [call.kwargs for call in log.log.call_args_list]


# ------------------------------------------------------------------ the audit helper


def test_an_edit_on_an_org_held_file_writes_a_transcript_edit_event(monkeypatch, audit_log):
    file = _file()
    _hold(monkeypatch, file.id, HoldState(platform=False, org_hold_uuids=("hold-1",)))

    written = record_edit_on_held_file(
        MagicMock(),
        None,
        _actor(),
        file,
        kind="segment_edit",
        segment_uuid="seg-1",
        fields=["text", "start_time"],
    )

    assert written is True
    (event,) = _events(audit_log)
    assert event["event_type"] is AuditEventType.CONTENT_TRANSCRIPT_EDIT
    assert event["resource_type"] == "media_file"
    assert event["resource_uuid"] == str(file.uuid)
    assert event["organization_id"] == 7
    assert event["tenant_scope"] == "org"
    assert event["details"] == {
        "kind": "segment_edit",
        "segment_uuid": "seg-1",
        "fields": ["start_time", "text"],
        "held_by": ["organization"],
    }


def test_a_platform_hold_is_recorded_as_such(monkeypatch, audit_log):
    file = _file(organization_id=None)
    _hold(monkeypatch, file.id, HoldState(platform=True))

    record_edit_on_held_file(
        MagicMock(), None, _actor(), file, kind="segment_speaker", segment_uuid="seg-2"
    )

    (event,) = _events(audit_log)
    assert event["details"]["held_by"] == ["platform"]
    assert event["tenant_scope"] == "personal"


def test_an_edit_on_a_free_file_writes_nothing(monkeypatch, audit_log):
    file = _file()
    _hold(monkeypatch, file.id, None)

    written = record_edit_on_held_file(
        MagicMock(), None, _actor(), file, kind="segment_edit", segment_uuid="seg-1"
    )

    assert written is False
    audit_log.log.assert_not_called()


def test_the_event_never_carries_text_titles_or_values(monkeypatch, audit_log):
    file = _file()
    _hold(monkeypatch, file.id, HoldState(platform=False, org_hold_uuids=("h",)))

    record_edit_on_held_file(
        MagicMock(),
        None,
        _actor(),
        file,
        kind="segment_edit",
        segment_uuid="seg-1",
        fields=["text"],
    )

    body = json.dumps(_events(audit_log), default=str)
    assert SECRET_TEXT not in body
    assert SECRET_TITLE not in body
    assert "Confidential" not in body


def test_an_audit_failure_never_fails_the_edit(monkeypatch, audit_log):
    file = _file()
    _hold(monkeypatch, file.id, HoldState(platform=True))
    audit_log.log.side_effect = RuntimeError("opensearch down")

    assert (
        record_edit_on_held_file(
            MagicMock(), None, _actor(), file, kind="segment_edit", segment_uuid="s"
        )
        is False
    )


def test_a_failing_hold_lookup_never_fails_the_edit(monkeypatch, audit_log):
    def _boom(db, ids):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(content_hold_service, "hold_states", _boom)
    assert (
        record_edit_on_held_file(
            MagicMock(), None, _actor(), _file(), kind="segment_edit", segment_uuid="s"
        )
        is False
    )


# ------------------------------------------- the real edit endpoints reach the helper


def _segment() -> Any:
    return SimpleNamespace(
        uuid=uuid.uuid4(),
        start_time=1.0,
        end_time=2.0,
        text="before",
        speaker=None,
        speaker_id=None,
        media_file_id=41,
    )


def _edit_text(monkeypatch, file: Any, segment: Any):
    """Drive crud.update_single_transcript_segment with only the DB faked."""
    monkeypatch.setattr(crud, "get_media_file_by_uuid", lambda *a, **k: file)
    db = MagicMock(name="db")
    db.query.return_value.options.return_value.filter.return_value.first.return_value = segment
    monkeypatch.setattr(
        "app.services.redaction.service.RedactionService.redetect_edited_segment",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.services.search.reindex_dispatch.dispatch_transcript_reindex", lambda **k: None
    )
    monkeypatch.setattr(
        "app.services.analytics_service.AnalyticsService.refresh_analytics", lambda *a: True
    )
    return crud.update_single_transcript_segment(
        db,
        str(file.uuid),
        str(segment.uuid),
        TranscriptSegmentUpdate(text=SECRET_TEXT),
        _actor(),
        request=None,
    )


def test_editing_segment_text_on_a_held_file_writes_an_audit_event(monkeypatch, audit_log):
    file, segment = _file(), _segment()
    _hold(monkeypatch, file.id, HoldState(platform=False, org_hold_uuids=("hold-1",)))

    result = _edit_text(monkeypatch, file, segment)

    assert result.text == SECRET_TEXT, "the edit itself must still go through on a held file"
    (event,) = _events(audit_log)
    assert event["event_type"] is AuditEventType.CONTENT_TRANSCRIPT_EDIT
    assert event["details"]["kind"] == "segment_edit"
    assert event["details"]["fields"] == ["text"]
    assert event["details"]["segment_uuid"] == str(segment.uuid)
    assert SECRET_TEXT not in json.dumps(event, default=str)


def test_editing_segment_text_on_a_free_file_writes_no_audit_event(monkeypatch, audit_log):
    file, segment = _file(), _segment()
    _hold(monkeypatch, file.id, None)

    result = _edit_text(monkeypatch, file, segment)

    assert result.text == SECRET_TEXT
    audit_log.log.assert_not_called()


@pytest.fixture
def speaker_edit_collaborators(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Everything ``update_segment_speaker`` touches except the audit path, faked."""
    file, segment = _file(), _segment()
    monkeypatch.setattr(transcript_segments, "get_by_uuid", lambda *a, **k: segment)
    monkeypatch.setattr("app.services.takedown_service.is_hidden_for", lambda *a, **k: False)
    monkeypatch.setattr(transcript_segments, "_resource_in_tenant_scope", lambda *a: True)
    monkeypatch.setattr(transcript_segments, "_get_new_speaker_id", lambda *a, **k: None)
    monkeypatch.setattr(transcript_segments, "_handle_speaker_change", lambda *a, **k: None)
    monkeypatch.setattr(transcript_segments, "raise_if_incomplete", lambda *a: None)
    return file, segment


def test_reassigning_a_speaker_on_a_held_file_writes_an_audit_event(
    monkeypatch, audit_log, speaker_edit_collaborators
):
    file, segment = speaker_edit_collaborators
    _hold(monkeypatch, file.id, HoldState(platform=True))
    db = MagicMock(name="db")
    db.query.return_value.filter.return_value.first.return_value = file
    ctx = RequestContext(user=cast("Any", _actor()), org_id=7, org_role=None)

    transcript_segments.update_segment_speaker(
        str(segment.uuid),
        SegmentSpeakerUpdate(speaker_uuid=None),
        db,
        _actor(),
        ctx,
        request=MagicMock(name="request"),
    )

    (event,) = _events(audit_log)
    assert event["event_type"] is AuditEventType.CONTENT_TRANSCRIPT_EDIT
    assert event["details"]["kind"] == "segment_speaker"
    assert event["details"]["held_by"] == ["platform"]


# ------------------------------------------------------------- tag-share audit events


def _tag() -> Any:
    return SimpleNamespace(uuid=uuid.uuid4(), name=SECRET_TAG, organization_id=7)


@pytest.fixture
def tag_audit(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    log = MagicMock(name="audit_logger")
    monkeypatch.setattr(sharing, "audit_logger", log)
    monkeypatch.setattr(
        sharing, "get_request_context", lambda request: {"source_ip": "10.0.0.1", "user_agent": "t"}
    )
    monkeypatch.setattr(sharing, "request_org_id", lambda request: 7)
    return log


@pytest.mark.parametrize(
    "event_type", [AuditEventType.RESOURCE_SHARE, AuditEventType.RESOURCE_UNSHARE]
)
def test_no_tag_share_event_contains_the_tag_name(tag_audit, event_type):
    tag = _tag()

    sharing._audit_tag_share(
        MagicMock(), _actor(), tag, event_type, target_user_id=9, target_group_id=None
    )

    (event,) = _events(tag_audit)
    assert event["event_type"] is event_type
    assert event["resource_type"] == "tag"
    assert event["resource_uuid"] == str(tag.uuid), "the tag is identified by uuid"
    assert event["target_user_id"] == 9
    assert SECRET_TAG not in json.dumps(event, default=str)
    assert "resource_name" not in event["details"]


def test_the_tag_share_endpoints_cannot_bypass_the_helper():
    """Both endpoints must audit through the helper; a second ``audit_logger.log`` is a leak."""
    import inspect

    source = inspect.getsource(sharing)
    assert source.count("_audit_tag_share(") == 3  # the definition and the two endpoints
    assert source.count("audit_logger.log(") == 1
    assert "resource_name" not in source.replace("never put the tag's NAME", "")


def test_a_failing_audit_lookup_leaves_the_session_usable_and_keeps_staged_work(
    monkeypatch, audit_log, db_session, normal_user
):
    """The helper runs between an edit's commit and the writes that follow it.

    A DB error inside it aborts the transaction; unless only the helper's own work is rolled
    back, the next statement of the request fails and a committed edit answers 500.
    """
    from sqlalchemy import select
    from sqlalchemy import text

    from app.models.user import User

    def broken(db, ids):
        db.execute(text("SELECT * FROM a_table_that_does_not_exist"))

    monkeypatch.setattr(content_hold_service, "hold_states", broken)
    normal_user.full_name = "staged before the audit"

    written = record_edit_on_held_file(
        db_session, None, normal_user, _file(), kind="segment_edit", segment_uuid="s"
    )

    assert written is False
    assert db_session.execute(text("SELECT 1")).scalar() == 1
    staged = db_session.execute(select(User.full_name).where(User.id == normal_user.id)).scalar()
    assert staged == "staged before the audit"
