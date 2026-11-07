"""Transcript edits on a held file are refused; tag-share events never carry the tag name.

Two concerns in one module:

1. A legal hold freezes the file's content (v0.7.0). Before it, editing a held file's
   transcript was allowed and recorded as ``content.transcript.edit`` (#1307); that left the
   pre-hold text overwritten, the only copy gone. Each edit is now refused with
   ``409 FILE_UNDER_LEGAL_HOLD`` BEFORE anything is written or committed, and the refusal is
   recorded as ``content.hold.refused`` with the edit's action.
2. Tag-share events still carried ``details.resource_name`` (the tag's name) after collection
   events had dropped theirs. A tag name is user text and may be sensitive; an audit log is
   read by people who may not see the tag.

In-process: the hold lookup, the refusal audit and the outbox stage are the seams, so a
regression in the call sites or their order against the commit fails here without a database.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any
from typing import cast
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api import hold_refusal
from app.api.endpoints import transcript_segments
from app.api.endpoints.files import crud
from app.api.endpoints.tags import sharing
from app.auth.audit_events import AuditEventType
from app.schemas.transcript import SegmentSpeakerUpdate
from app.schemas.transcript import TranscriptSegmentUpdate
from app.services import audit_outbox
from app.services import content_audit
from app.services.hold_guard import FREEZE_REFUSAL
from app.services.hold_guard import LEGAL_HOLD_ERROR_CODE
from app.services.hold_guard import HoldState
from app.services.request_context import RequestContext

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


@pytest.fixture
def staged(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """``audit_outbox.stage``, recorded: its keyword arguments are ``AuditLogger.log``'s."""
    stage = MagicMock(name="audit_outbox.stage", return_value="event-uuid")
    monkeypatch.setattr(audit_outbox, "stage", stage)
    return stage


def _staged(stage: MagicMock) -> list[dict[str, Any]]:
    return [dict(call.kwargs) for call in stage.call_args_list]


def _hold(monkeypatch: pytest.MonkeyPatch, file_id: int, state: HoldState | None) -> None:
    result = {file_id: state} if state is not None else {}
    monkeypatch.setattr(hold_refusal, "hold_states", lambda db, ids: result)


@pytest.fixture
def refusals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[int, list[str]]]]:
    """``(action, {file_id: held_by})`` for every ``content.hold.refused`` the guard writes."""
    seen: list[tuple[str, dict[int, list[str]]]] = []

    def _record(db, states, *, actor, action, request=None):
        seen.append((action, {fid: st.held_by for fid, st in states.items()}))

    monkeypatch.setattr(hold_refusal, "audit_hold_refusals", _record)
    return seen


def _events(log: MagicMock) -> list[dict[str, Any]]:
    return [call.kwargs for call in log.log.call_args_list]


# ------------------------------------------- the real edit endpoints refuse a held file


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


def _edit_text(monkeypatch, file: Any, segment: Any, db: MagicMock):
    """Drive crud.update_single_transcript_segment with only the DB faked."""
    monkeypatch.setattr(crud, "get_media_file_by_uuid", lambda *a, **k: file)
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


def test_editing_segment_text_on_a_held_file_is_refused_before_anything_is_written(
    monkeypatch, staged, refusals
):
    file, segment = _file(), _segment()
    _hold(monkeypatch, file.id, HoldState(platform_flag=False, org_hold_uuids=("hold-1",)))
    db = MagicMock(name="db")

    with pytest.raises(HTTPException) as refused:
        _edit_text(monkeypatch, file, segment, db)

    assert refused.value.status_code == 409
    detail = cast("dict[str, Any]", refused.value.detail)
    assert detail["error"] == LEGAL_HOLD_ERROR_CODE
    assert detail["message"] == FREEZE_REFUSAL
    assert detail["held_by"] == ["organization"]
    assert "holds" not in detail, "an editor is never given the organization's hold uuids"
    assert segment.text == "before"
    db.commit.assert_not_called()
    staged.assert_not_called()
    assert refusals == [("segment_edit", {file.id: ["organization"]})]
    assert SECRET_TEXT not in json.dumps(detail)


def test_editing_segment_text_on_a_free_file_goes_through_and_stages_nothing(
    monkeypatch, staged, refusals
):
    file, segment = _file(), _segment()
    _hold(monkeypatch, file.id, None)

    result = _edit_text(monkeypatch, file, segment, MagicMock(name="db"))

    assert result.text == SECRET_TEXT
    staged.assert_not_called()
    assert refusals == []


@pytest.fixture
def speaker_edit_collaborators(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Everything ``update_segment_speaker`` touches except the hold path, faked."""
    file, segment = _file(), _segment()
    monkeypatch.setattr(transcript_segments, "get_by_uuid", lambda *a, **k: segment)
    monkeypatch.setattr("app.services.file_availability.is_unavailable", lambda *a, **k: False)
    monkeypatch.setattr(transcript_segments, "_resource_in_tenant_scope", lambda *a: True)
    monkeypatch.setattr(transcript_segments, "_get_new_speaker_id", lambda *a, **k: 99)
    monkeypatch.setattr(transcript_segments, "_handle_speaker_change", lambda *a, **k: None)
    monkeypatch.setattr(transcript_segments, "raise_if_incomplete", lambda *a: None)
    return file, segment


def test_reassigning_a_speaker_on_a_held_file_is_refused_before_the_segment_changes(
    monkeypatch, speaker_edit_collaborators, staged, refusals
):
    file, segment = speaker_edit_collaborators
    _hold(monkeypatch, file.id, HoldState(platform_flag=True))
    db = MagicMock(name="db")
    db.query.return_value.filter.return_value.first.return_value = file
    ctx = RequestContext(user=cast("Any", _actor()), org_id=7, org_role=None)

    with pytest.raises(HTTPException) as refused:
        transcript_segments.update_segment_speaker(
            str(segment.uuid),
            SegmentSpeakerUpdate(speaker_uuid=None),
            db,
            _actor(),
            ctx,
            request=MagicMock(name="request"),
        )

    assert refused.value.status_code == 409
    assert cast("dict[str, Any]", refused.value.detail)["held_by"] == ["platform"]
    assert segment.speaker_id is None, "the segment keeps the speaker it had when held"
    db.commit.assert_not_called()
    staged.assert_not_called()
    assert refusals == [("segment_speaker", {file.id: ["platform"]})]


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
