"""Branches of the transcript-read redaction gate that nothing else pinned.

Found by mutating ``api/endpoints/files/redaction_gate.py``, ``transcript_view.py`` and
``detail_response.py`` (then one module, ``files/crud.py``) one branch at a time and running
every test that reaches them. Each case below was a mutation the whole suite let through:

* the file detail read (``GET /api/files/{uuid}``) served the transcript while detection
  was still pending, served cached spans unmasked, and ignored ``redact=true``: the
  existing withholding/masking tests drive the ``/segments`` and caption surfaces only;
* a redaction policy that could not be resolved was answered with "redaction off" (an
  unmasked read with no audit event) instead of the 503 the gate documents;
* the ``transcript.view_unredacted`` event could be dropped from the reveal path;
* under a support-access grant, the owner's policy could be read in the requester's
  tenant instead of the file's;
* the scan claim could report a broker publish without its task id, or queue the scan
  for the wrong user, and a failed claim commit or a failed withdrawal had no test;
* ``identity.by`` could be hidden from the file's owner on the detail read, and shown to
  any member of the file's organization instead of only its administrators;
* the legal-hold uuids could be handed to every viewer of a held file, not only to an
  administrator of its organization.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.endpoints.files.detail_response import _redact_and_decorate_speakers
from app.api.endpoints.files.detail_response import get_media_file_detail
from app.api.endpoints.files.redaction_gate import RedactionScanDispatch
from app.api.endpoints.files.redaction_gate import _resolve_redaction_for_request
from app.api.endpoints.files.redaction_gate import claim_and_dispatch_redaction_scan
from app.auth import audit as audit_module
from app.auth.audit_events import AuditEventType
from app.core import redaction_defaults
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.organization import Organization
from app.models.speaker import Speaker
from app.models.user import User
from app.services.platform_bypass import PlatformBypass
from app.services.platform_bypass import SupportGrantView
from app.services.redaction.config import EffectiveRedactionConfig
from tests.user_owned_rows import make_user

FORCED_WORD = "acquisition"
SENSITIVE_TEXT = f"we should discuss the {FORCED_WORD} before the board meets"


def _set_prefs(db_session, user, **prefs: str) -> None:
    from app import models

    for key, value in prefs.items():
        db_session.add(models.UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.flush()


@pytest.fixture
def redacting_owner(db_session, normal_user):
    """An owner whose policy masks exactly one category, ``custom``."""
    _set_prefs(db_session, normal_user, redaction_enabled="true", redaction_categories='["custom"]')
    return normal_user


def _scanned_file(
    db_session,
    owner: User,
    *,
    redaction_status: str | None = redaction_defaults.REDACTION_STATUS_DONE,
    organization_id: int | None = None,
) -> MediaFile:
    """A completed file with one segment carrying a cached ``custom`` span on FORCED_WORD."""
    start = SENSITIVE_TEXT.find(FORCED_WORD)
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        organization_id=organization_id,
        filename=f"gate-branches-{uuid_pkg.uuid4().hex[:8]}.wav",
        storage_path=f"gate-branches-test/{uuid_pkg.uuid4().hex}",
        file_size=1,
        content_type="audio/wav",
        language="en",
        status="completed",
        redaction_status=redaction_status,
    )
    db_session.add(media)
    db_session.commit()
    db_session.add(
        TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media.id,
            start_time=0.0,
            end_time=3.0,
            text=SENSITIVE_TEXT,
            redactions=[
                {
                    "char_start": start,
                    "char_end": start + len(FORCED_WORD),
                    "category": "custom",
                    "entity_type": "CUSTOM",
                    "detector": "wordlist",
                    "confidence": 1.0,
                }
            ],
        )
    )
    db_session.commit()
    db_session.refresh(media)
    return media


def _org(db_session) -> Organization:
    org = Organization(name="gate-branches", slug=f"gate-branches-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    return org


@pytest.fixture
def audit_events(monkeypatch) -> list[dict[str, Any]]:
    """Every audit event written while the test runs (the real event, not the helper call)."""
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(audit_module.audit_logger, "log", lambda **kw: seen.append(kw))
    return seen


class _Task:
    def __init__(self, task_id: str | None) -> None:
        if task_id is not None:
            self.id = task_id


@pytest.fixture
def broker(monkeypatch):
    """Stand in for ``redaction_detect_task.delay``; an unreachable broker raises."""
    from app.tasks import redaction_task

    class _Broker:
        def __init__(self) -> None:
            self.published: list[dict] = []
            self.reachable = True
            self.task_id: str | None = "scan-task-1"

        def delay(self, **kwargs):
            if not self.reachable:
                raise OSError("[Errno 111] Connection refused: redis://redis:6379//")
            self.published.append(kwargs)
            return _Task(self.task_id)

    stub = _Broker()
    monkeypatch.setattr(redaction_task.redaction_detect_task, "delay", stub.delay)
    return stub


def _fail_commit_number(monkeypatch, db_session, n: int) -> list[str]:
    """Make the n-th ``commit`` on ``db_session`` (1-based) raise; the others are real.

    ``rollback`` is recorded and reduced to what it does to loaded instances (expire them):
    a real one would also discard this test's own committed rows, because the test session
    is a savepoint inside one outer transaction.
    """
    real_commit = db_session.commit
    calls = {"n": 0}
    rollbacks: list[str] = []

    def _commit() -> None:
        calls["n"] += 1
        if calls["n"] == n:
            raise RuntimeError("could not serialize access due to concurrent update")
        real_commit()

    def _rollback() -> None:
        rollbacks.append(f"after commit {calls['n']}")
        db_session.expire_all()

    monkeypatch.setattr(db_session, "commit", _commit)
    monkeypatch.setattr(db_session, "rollback", _rollback)
    return rollbacks


# ---------------------------------------------------------------- the file detail read


def test_the_detail_read_withholds_the_transcript_while_detection_is_pending(
    db_session, redacting_owner, broker
):
    """No unredacted display window on ``GET /api/files/{uuid}`` either."""
    media = _scanned_file(
        db_session, redacting_owner, redaction_status=redaction_defaults.REDACTION_STATUS_PENDING
    )

    detail = get_media_file_detail(db_session, str(media.uuid), redacting_owner)

    assert detail.redaction_pending is True
    assert detail.transcript_segments == []
    assert detail.grouped_segments == []
    assert (detail.total_segments, detail.total_speaker_segments) == (0, 0), (
        "a withheld transcript must not leak its shape through the counts"
    )
    assert broker.published == [], "pending already claims a queued scan"

    # The control: the same read of the same file once detection has finished.
    media.redaction_status = redaction_defaults.REDACTION_STATUS_DONE
    db_session.commit()
    done = get_media_file_detail(db_session, str(media.uuid), redacting_owner)
    assert done.redaction_pending is False
    assert len(done.transcript_segments) == 1
    assert (done.total_segments, done.total_speaker_segments) == (1, 1)


def test_the_detail_read_masks_a_cached_span_and_only_the_owner_reveals_it_audited(
    db_session, redacting_owner, audit_events
):
    media = _scanned_file(db_session, redacting_owner)

    masked = get_media_file_detail(db_session, str(media.uuid), redacting_owner, redact=True)

    assert masked.redaction_enabled is True
    assert FORCED_WORD not in masked.transcript_segments[0].text
    assert audit_events == [], "nothing was revealed, so no reveal event"

    revealed = get_media_file_detail(db_session, str(media.uuid), redacting_owner, redact=False)

    assert FORCED_WORD in revealed.transcript_segments[0].text
    assert [e["event_type"] for e in audit_events] == [AuditEventType.TRANSCRIPT_VIEW_UNREDACTED]
    assert audit_events[0]["resource_uuid"] == str(media.uuid)
    assert audit_events[0]["details"]["revealed_categories"] == ["custom"]
    assert audit_events[0]["details"]["surface"] == "transcript"


def test_a_policy_that_cannot_be_resolved_refuses_the_read_with_503(
    db_session, redacting_owner, audit_events, monkeypatch
):
    """Fail closed: "no policy" must never read as "redaction off"."""
    media = _scanned_file(db_session, redacting_owner)

    def _unresolvable(*_args: object, **_kwargs: object) -> EffectiveRedactionConfig:
        raise RuntimeError("system_settings is unreachable")

    monkeypatch.setattr("app.services.redaction.config.resolve_effective_config", _unresolvable)

    with pytest.raises(HTTPException) as gate:
        _resolve_redaction_for_request(
            db_session, media, redacting_owner, bypass=PlatformBypass.none(), redact=False
        )
    assert gate.value.status_code == 503

    # The detail read passes the 503 through rather than turning it into a 500 or a read.
    with pytest.raises(HTTPException) as read:
        get_media_file_detail(db_session, str(media.uuid), redacting_owner, redact=False)
    assert read.value.status_code == 503
    assert read.value.detail == "Redaction policy is temporarily unavailable; transcript withheld."
    assert audit_events == []


def test_under_a_grant_the_owners_policy_is_read_in_the_files_own_tenant(
    db_session, normal_user, admin_user, monkeypatch
):
    org = _org(db_session)
    media = _scanned_file(db_session, normal_user, organization_id=org.id)
    grant = SupportGrantView(
        id=1,
        uuid="00000000-0000-0000-0000-000000000001",
        organization_id=org.id,
        subject_user_id=None,
        access_level="read",
        grant_mode="approved",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    asked: list[tuple[int, int | None]] = []

    def _policy(_db: object, user_id: int, organization_id: int | None = None):
        asked.append((user_id, organization_id))
        return EffectiveRedactionConfig(enabled=True, enabled_categories={"custom"})

    monkeypatch.setattr("app.services.redaction.config.resolve_effective_config", _policy)

    # The requester's own scope (personal here) is deliberately not the file's tenant.
    _cfg, reveal = _resolve_redaction_for_request(
        db_session,
        media,
        admin_user,
        bypass=PlatformBypass(user=admin_user, grant=grant),
        redact=False,
        organization_id=None,
    )

    assert asked == [(normal_user.id, org.id)]
    assert reveal == set(), "support staff never reveal under a grant"


def test_the_detail_read_shows_the_owner_who_confirmed_a_speakers_identity(
    db_session, redacting_owner
):
    media = _scanned_file(db_session, redacting_owner)
    db_session.add(
        Speaker(
            user_id=redacting_owner.id,
            media_file_id=media.id,
            name="SPEAKER_00",
            display_name="Ada",
            identity_source="human",
            identity_source_user_id=redacting_owner.id,
            identity_source_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    detail = get_media_file_detail(db_session, str(media.uuid), redacting_owner)

    identity = detail.speakers[0].identity
    assert identity is not None and identity.by is not None
    assert str(identity.by.uuid) == str(redacting_owner.uuid)


def test_only_an_org_admin_is_named_who_confirmed_an_identity_in_an_org_file(
    db_session, normal_user
):
    org = _org(db_session)
    media = _scanned_file(db_session, normal_user, organization_id=org.id)
    speaker = Speaker(
        user_id=normal_user.id,
        media_file_id=media.id,
        name="SPEAKER_00",
        display_name="Ada",
        identity_source="human",
        identity_source_user_id=normal_user.id,
        identity_source_at=datetime.now(UTC),
    )
    db_session.add(speaker)
    db_session.commit()
    member = make_user(db_session, "gate-branches-member")

    def _by_as_seen_by(*, administers_the_org: bool):
        db_session.expire_all()
        rows = db_session.query(Speaker).filter(Speaker.media_file_id == media.id).all()
        _redact_and_decorate_speakers(
            db_session,
            rows,
            [],
            member,
            bypass=PlatformBypass.none(),
            organization_id=org.id,
            reveal_hold_uuids=administers_the_org,
        )
        return rows[0].identity["by"]

    assert _by_as_seen_by(administers_the_org=False) is None
    by = _by_as_seen_by(administers_the_org=True)
    assert by is not None and str(by["uuid"]) == str(normal_user.uuid)


def test_the_detail_read_names_a_hold_but_gives_its_uuids_only_to_an_org_admin(
    db_session, normal_user
):
    from app.services.content_hold_service import place_file_hold

    org = _org(db_session)
    media = _scanned_file(db_session, normal_user, organization_id=org.id)
    change = place_file_hold(
        db_session, org_id=org.id, actor=normal_user, files=[media], reason="litigation"
    )
    db_session.commit()

    member_view = get_media_file_detail(
        db_session, str(media.uuid), normal_user, organization_id=org.id
    )
    admin_view = get_media_file_detail(
        db_session, str(media.uuid), normal_user, organization_id=org.id, reveal_hold_uuids=True
    )

    assert member_view.hold.held is True
    assert member_view.hold.hold_uuids == []
    assert admin_view.hold.hold_uuids == [str(change.hold.uuid)]


# ------------------------------------------------------------ the scan claim protocol


def test_a_published_scan_reports_its_task_and_runs_for_the_files_owner(
    db_session, redacting_owner, broker
):
    media = _scanned_file(db_session, redacting_owner, redaction_status=None)

    queued = claim_and_dispatch_redaction_scan(db_session, media, surface="test")

    assert queued == RedactionScanDispatch(queued=True, task_id="scan-task-1")
    assert broker.published == [{"file_id": media.id, "user_id": redacting_owner.id}]

    broker.task_id = None
    assert claim_and_dispatch_redaction_scan(
        db_session, media, surface="test"
    ) == RedactionScanDispatch(queued=True, task_id=None)


def test_a_claim_that_cannot_be_committed_queues_nothing(
    db_session, redacting_owner, broker, monkeypatch
):
    media = _scanned_file(db_session, redacting_owner, redaction_status=None)
    rollbacks = _fail_commit_number(monkeypatch, db_session, 1)

    queued = claim_and_dispatch_redaction_scan(db_session, media, surface="test")

    assert queued == RedactionScanDispatch(queued=False, task_id=None)
    assert broker.published == [], "a scan was published for a claim that never landed"
    assert rollbacks == ["after commit 1"]
    db_session.refresh(media)
    assert media.redaction_status is None


def test_a_withdrawal_that_fails_is_logged_as_stranded_and_never_raises(
    db_session, redacting_owner, broker, monkeypatch, caplog
):
    """The withdrawal runs on an error path: its own failure must not replace the outcome."""
    media = _scanned_file(db_session, redacting_owner, redaction_status=None)
    broker.reachable = False
    rollbacks = _fail_commit_number(monkeypatch, db_session, 2)  # 1 = claim, 2 = withdrawal

    queued = claim_and_dispatch_redaction_scan(db_session, media, surface="test")

    assert queued == RedactionScanDispatch(queued=False, task_id=None)
    assert rollbacks == ["after commit 2"]
    assert "is stranded at 'pending' with no scan queued" in caplog.text
