"""The content-audit writer's rules, one at a time (#1277).

Gating (capability AND setting, governance events exempt), tenant stamping from the RESOURCE,
the owner as ``target_user_id``, de-duplication of view-class events, and the rule that a
Redis outage writes MORE audit, never less.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.auth.audit import AuditEventType
from app.core.constants import AUDIT_CONTENT_ACCESS_SETTING_KEY
from app.models.user import User
from app.services import content_audit
from app.services.content_audit import KIND_DOWNLOAD_PREPARE
from app.services.content_audit import KIND_STREAM_URL
from app.services.content_audit import AuditedResource
from app.services.content_audit import content_audit_enabled
from app.services.content_audit import record_content_event
from app.services.platform_bypass import PlatformBypass
from app.services.platform_bypass import SupportGrantView
from app.services.system_settings_service import set_setting

ACTOR = User(id=11, email="actor@example.com")
OTHER_ACTOR = User(id=99, email="other@example.com")


def _resource(org_id: int | None = 7, owner_id: int | None = 12) -> AuditedResource:
    row = SimpleNamespace(uuid="res-1", organization_id=org_id, user_id=owner_id)
    return AuditedResource.file(row)


class _Redis:
    """The slice of redis-py ``SET NX EX`` the de-dup uses."""

    def __init__(self):
        self.keys: dict[str, int] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = ex
        return True


def _rec(
    db,
    event: AuditEventType,
    *,
    actor: User = ACTOR,
    resource: AuditedResource | None = None,
    details: dict | None = None,
    dedup: bool = True,
    bypass: PlatformBypass | None = None,
) -> bool:
    return record_content_event(
        db,
        None,
        event=event,
        actor=actor,
        resource=resource or _resource(),
        details=details,
        dedup=dedup,
        bypass=bypass,
    )


@pytest.fixture()
def written(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(content_audit.audit_logger, "log", lambda **kw: events.append(kw))
    return events


@pytest.fixture()
def auditing_on(db_session, monkeypatch):
    monkeypatch.setattr(content_audit, "capability_enabled", lambda key, request=None: True)
    set_setting(db_session, AUDIT_CONTENT_ACCESS_SETTING_KEY, True)


@pytest.fixture()
def redis(monkeypatch):
    fake = _Redis()
    monkeypatch.setattr("app.core.redis.get_probe_redis", lambda: fake)
    return fake


def test_the_switch_defaults_to_off(db_session, monkeypatch):
    monkeypatch.setattr(content_audit, "capability_enabled", lambda key, request=None: True)
    assert content_audit_enabled(db_session) is False


@pytest.mark.parametrize(
    ("capability", "setting", "expected"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_both_gates_must_be_open(db_session, monkeypatch, capability, setting, expected):
    monkeypatch.setattr(content_audit, "capability_enabled", lambda key, request=None: capability)
    set_setting(db_session, AUDIT_CONTENT_ACCESS_SETTING_KEY, setting)
    assert content_audit_enabled(db_session) is expected


@pytest.mark.parametrize(
    "event",
    [
        AuditEventType.CONTENT_FILE_VIEW,
        AuditEventType.CONTENT_MEDIA_ACCESS,
        AuditEventType.CONTENT_EXPORT,
    ],
)
def test_the_high_volume_events_are_dropped_while_auditing_is_off(db_session, written, event):
    assert _rec(db_session, event, dedup=False) is False
    assert written == []


def test_governance_events_are_recorded_with_auditing_off(db_session, written):
    for event in (AuditEventType.COLLECTION_VISIBILITY_CHANGE, AuditEventType.RESOURCE_SHARE):
        assert _rec(db_session, event)
    assert len(written) == 2


def test_an_org_resource_is_stamped_with_its_org_and_owner(db_session, written, auditing_on, redis):
    _rec(
        db_session,
        AuditEventType.CONTENT_FILE_VIEW,
        resource=_resource(org_id=7, owner_id=12),
        details={"surface": "detail"},
    )

    (event,) = written
    assert event["organization_id"] == 7
    assert event["tenant_scope"] == "org"
    assert (event["resource_type"], event["resource_uuid"]) == ("media_file", "res-1")
    assert event["user_id"] == 11
    assert event["target_user_id"] == 12, "the owner, because they are not the actor"
    assert event["details"] == {"surface": "detail"}


def test_a_personal_resource_is_never_attributed_to_an_org(db_session, written, auditing_on, redis):
    _rec(
        db_session,
        AuditEventType.CONTENT_FILE_VIEW,
        resource=_resource(org_id=None, owner_id=ACTOR.id),
    )

    (event,) = written
    assert event["organization_id"] is None
    assert event["tenant_scope"] == "personal"
    assert event["target_user_id"] is None, "acting on your own file names no other subject"


def test_a_repeat_view_inside_the_window_is_written_once(db_session, written, auditing_on, redis):
    first = _rec(db_session, AuditEventType.CONTENT_FILE_VIEW)
    second = _rec(db_session, AuditEventType.CONTENT_FILE_VIEW)

    assert (first, second) == (True, False)
    assert len(written) == 1
    ((_key, ttl),) = redis.keys.items()
    assert ttl == 900


def test_dedup_is_per_actor_resource_and_surface(db_session, written, auditing_on, redis):
    _rec(db_session, AuditEventType.CONTENT_FILE_VIEW)
    _rec(db_session, AuditEventType.CONTENT_FILE_VIEW, actor=OTHER_ACTOR)
    _rec(
        db_session,
        AuditEventType.CONTENT_FILE_VIEW,
        resource=AuditedResource.file(SimpleNamespace(uuid="other", organization_id=7, user_id=1)),
    )
    assert len(written) == 3


def test_a_stream_url_is_collapsed_but_a_download_never_is(db_session, written, auditing_on, redis):
    def access(kind):
        return _rec(db_session, AuditEventType.CONTENT_MEDIA_ACCESS, details={"kind": kind})

    assert [access(KIND_STREAM_URL), access(KIND_STREAM_URL)] == [True, False]
    assert [access(KIND_DOWNLOAD_PREPARE), access(KIND_DOWNLOAD_PREPARE)] == [True, True]


def test_an_export_is_never_collapsed(db_session, written, auditing_on, redis):
    for _ in range(2):
        _rec(
            db_session,
            AuditEventType.CONTENT_EXPORT,
            details={"format": "srt", "surface": "subtitles", "file_count": 1},
        )
    assert len(written) == 2


def test_redis_down_still_writes_the_event(db_session, written, auditing_on, monkeypatch):
    def _down():
        raise ConnectionError("redis is down")

    monkeypatch.setattr("app.core.redis.get_probe_redis", _down)

    assert _rec(db_session, AuditEventType.CONTENT_FILE_VIEW) is True
    assert _rec(db_session, AuditEventType.CONTENT_FILE_VIEW) is True
    assert len(written) == 2, "fail toward more audit, never less"


def test_a_support_grant_is_named_in_the_event(db_session, written, auditing_on, redis):
    grant = SupportGrantView(
        id=1,
        uuid="grant-1",
        organization_id=7,
        subject_user_id=None,
        access_level="read",
        grant_mode="approved",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    _rec(db_session, AuditEventType.CONTENT_FILE_VIEW, bypass=PlatformBypass(grant=grant))
    assert written[0]["details"]["grant_uuid"] == "grant-1"


def test_the_writer_never_raises(db_session, auditing_on, redis, monkeypatch):
    def _boom(**_):
        raise RuntimeError("opensearch exploded")

    monkeypatch.setattr(content_audit.audit_logger, "log", _boom)
    assert _rec(db_session, AuditEventType.CONTENT_FILE_VIEW) is False
