"""Every content-access and membership emit site writes ONE correctly scoped event (#1277).

Each test drives a real route as a member of an organization and reads what reached the audit
logger. The recorder replaces ``audit_logger.log`` on the shared singleton, so it sees the
events written by the shared writer (``services/content_audit``) AND by the routes that call the
logger directly (collection and tag shares).

What is pinned, per emit site: the event type, ``organization_id`` and ``tenant_scope`` taken
from the RESOURCE (not the request), ``resource_uuid``, and the one detail that tells the
surfaces apart. The cross-cutting rules (the opt-in switch, view de-duplication, a Redis outage
writing more audit, no names or text in an event) each have their own test, with a control
that proves the same request does write when the rule is not in play.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg

import pytest
from fastapi import status

from app.auth.audit import TENANT_SCOPE_ORG
from app.auth.audit import TENANT_SCOPE_PERSONAL
from app.auth.audit import AuditEventType
from app.auth.audit import audit_logger
from app.core.constants import AUDIT_CONTENT_ACCESS_SETTING_KEY
from app.models.chat import ROLE_ASSISTANT
from app.models.chat import ROLE_USER
from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.models.media import TranscriptSegment
from app.models.topic import TopicSuggestion
from app.services import content_audit
from app.services.auto_label_service import AutoLabelService
from app.services.system_settings_service import set_setting

# The audit switch is a SystemSettings key shared by every test that flips it.
pytestmark = pytest.mark.xdist_group("content_audit_settings")

SECRET_TITLE = "Quarterly layoffs plan"
SECRET_TEXT = "the acquisition closes on friday"

CONTENT_EVENTS = {
    AuditEventType.CONTENT_FILE_VIEW,
    AuditEventType.CONTENT_MEDIA_ACCESS,
    AuditEventType.CONTENT_EXPORT,
}


class _Redis:
    """The slice of redis-py ``SET NX EX`` the view de-duplication uses."""

    def __init__(self):
        self.keys: dict[str, int] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = ex
        return True


class _BrokenRedis:
    def set(self, *args, **kwargs):
        raise ConnectionError("redis is down")


@pytest.fixture()
def events(monkeypatch) -> list[dict]:
    """Every audit event written during the test, as the keyword arguments of ``log``."""
    written: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: written.append(kw))
    return written


@pytest.fixture()
def auditing_on(db_session, monkeypatch):
    monkeypatch.setattr(content_audit, "capability_enabled", lambda key, request=None: True)
    set_setting(db_session, AUDIT_CONTENT_ACCESS_SETTING_KEY, True)


@pytest.fixture()
def redis(monkeypatch) -> _Redis:
    fake = _Redis()
    monkeypatch.setattr("app.core.redis.get_probe_redis", lambda: fake)
    return fake


@pytest.fixture()
def presign(monkeypatch):
    # Presigning issues a bucket-region lookup over the network; the URL is never asserted.
    monkeypatch.setattr(
        "app.services.minio_service.get_presigned_download_url",
        lambda *args, **kwargs: "https://storage.test/presigned/clip.mp3",
    )


def _of(events: list[dict], *types: AuditEventType) -> list[dict]:
    return [e for e in events if e["event_type"] in types]


def _one(events: list[dict], event: AuditEventType) -> dict:
    found = _of(events, event)
    assert len(found) == 1, f"expected exactly one {event.value}, got {[e for e in events]}"
    return found[0]


def _add_segments(db, media_file) -> None:
    for start, end in ((0.0, 1.5), (1.5, 3.0)):
        db.add(
            TranscriptSegment(
                media_file_id=media_file.id, start_time=start, end_time=end, text=SECRET_TEXT
            )
        )
    db.commit()


def _org_file(gov, **fields):
    """Alice's organization file, readable by the other members through an org collection."""
    media_file = gov.file(gov.alice, title=SECRET_TITLE, **fields)
    gov.put(gov.collection(gov.alice), media_file)
    return media_file


# --------------------------------------------------------------------------- #
# One event per action, scoped from the resource
# --------------------------------------------------------------------------- #


def test_file_view_is_recorded_under_the_files_organization(
    client, gov, events, auditing_on, redis
):
    media_file = _org_file(gov)
    with gov.acting_as(gov.bob):
        response = client.get(f"/api/files/{media_file.uuid}")
    assert response.status_code == status.HTTP_200_OK, response.text

    event = _one(events, AuditEventType.CONTENT_FILE_VIEW)
    assert event["user_id"] == gov.bob.id
    assert event["organization_id"] == gov.org.id
    assert event["tenant_scope"] == TENANT_SCOPE_ORG
    assert event["resource_type"] == "media_file"
    assert event["resource_uuid"] == str(media_file.uuid)
    assert event["target_user_id"] == gov.alice.id  # the owner: "who looked at Alice's files"
    assert event["details"]["surface"] == "detail"


def test_a_personal_file_view_is_stamped_personal_not_org(client, gov, events, auditing_on, redis):
    media_file = gov.file(gov.alice, org=None)
    with gov.acting_as(gov.alice, org=None):
        assert client.get(f"/api/files/{media_file.uuid}").status_code == status.HTTP_200_OK

    event = _one(events, AuditEventType.CONTENT_FILE_VIEW)
    assert event["organization_id"] is None
    assert event["tenant_scope"] == TENANT_SCOPE_PERSONAL


def test_a_refused_view_records_nothing(client, gov, events, auditing_on, redis):
    media_file = _org_file(gov)
    with gov.acting_as(gov.outsider, org=None):
        response = client.get(f"/api/files/{media_file.uuid}")
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert _of(events, *CONTENT_EVENTS) == []


def test_stream_url_is_a_media_access_but_a_thumbnail_is_not(
    client, gov, events, auditing_on, redis
):
    media_file = _org_file(gov, thumbnail_path="thumbs/x.webp")
    with gov.acting_as(gov.bob):
        thumb = client.get(
            f"/api/files/{media_file.uuid}/stream-url", params={"media_type": "thumbnail"}
        )
        assert thumb.status_code == status.HTTP_200_OK, thumb.text
        assert _of(events, AuditEventType.CONTENT_MEDIA_ACCESS) == []

        media = client.get(f"/api/files/{media_file.uuid}/stream-url")
        assert media.status_code == status.HTTP_200_OK, media.text

    event = _one(events, AuditEventType.CONTENT_MEDIA_ACCESS)
    assert event["details"]["kind"] == "stream_url"
    assert event["organization_id"] == gov.org.id
    assert event["tenant_scope"] == TENANT_SCOPE_ORG
    assert event["resource_uuid"] == str(media_file.uuid)


def test_prepare_download_and_download_stream_each_record_a_download(
    client, gov, events, auditing_on, redis, presign
):
    media_file = _org_file(gov)
    with gov.acting_as(gov.bob):
        prepared = client.post(
            f"/api/files/{media_file.uuid}/prepare-download", params={"mode": "audio_original"}
        )
        assert prepared.status_code == status.HTTP_200_OK, prepared.text
        with client.stream(
            "GET",
            f"/api/files/{media_file.uuid}/download-stream",
            params={"mode": "audio_original"},
        ) as streamed:
            assert streamed.status_code == status.HTTP_200_OK

    kinds = sorted(e["details"]["kind"] for e in _of(events, AuditEventType.CONTENT_MEDIA_ACCESS))
    assert kinds == ["download_prepare", "download_stream"]
    for event in _of(events, AuditEventType.CONTENT_MEDIA_ACCESS):
        assert event["details"]["mode"] == "audio_original"
        assert event["organization_id"] == gov.org.id
        assert event["resource_uuid"] == str(media_file.uuid)


@pytest.mark.parametrize(
    ("path", "params", "surface"),
    [
        ("/api/files/{uuid}/export", {"format": "txt"}, "transcript"),
        ("/api/files/{uuid}/subtitles", {"subtitle_format": "srt"}, "subtitles"),
    ],
)
def test_a_transcript_or_subtitle_export_is_one_content_export(
    client, gov, events, auditing_on, redis, path, params, surface
):
    media_file = _org_file(gov)
    _add_segments(gov.db, media_file)
    with gov.acting_as(gov.bob):
        response = client.get(path.format(uuid=media_file.uuid), params=params)
    assert response.status_code == status.HTTP_200_OK, response.text

    event = _one(events, AuditEventType.CONTENT_EXPORT)
    assert event["details"]["surface"] == surface
    assert event["details"]["file_count"] == 1
    assert event["organization_id"] == gov.org.id
    assert event["tenant_scope"] == TENANT_SCOPE_ORG
    assert event["resource_uuid"] == str(media_file.uuid)


def test_a_summary_export_is_one_content_export(client, gov, events, auditing_on, redis):
    media_file = _org_file(
        gov, summary_data={"content": "Roadmap review.", "action_items": [], "key_points": []}
    )
    with gov.acting_as(gov.bob):
        response = client.get(f"/api/files/{media_file.uuid}/summary/export")
    assert response.status_code == status.HTTP_200_OK, response.text

    event = _one(events, AuditEventType.CONTENT_EXPORT)
    assert event["details"]["surface"] == "summary"
    assert event["resource_uuid"] == str(media_file.uuid)
    assert event["organization_id"] == gov.org.id


def test_a_bulk_export_records_one_event_per_file(client, gov, events, auditing_on, redis):
    first, second = _org_file(gov), _org_file(gov)
    _add_segments(gov.db, first)
    _add_segments(gov.db, second)
    with gov.acting_as(gov.bob):
        response = client.post(
            "/api/files/bulk-export/prepare",
            json={"file_uuids": [str(first.uuid), str(second.uuid)], "subtitle_format": "srt"},
        )
    assert response.status_code == status.HTTP_200_OK, response.text

    exported = _of(events, AuditEventType.CONTENT_EXPORT)
    assert sorted(e["resource_uuid"] for e in exported) == sorted(
        [str(first.uuid), str(second.uuid)]
    )
    for event in exported:
        assert event["details"]["surface"] == "bulk"
        assert event["details"]["file_count"] == 2


def test_a_chat_export_is_recorded_with_the_message_count_and_no_text(
    client, gov, events, auditing_on, redis
):
    conversation = ChatConversation(
        uuid=uuid_pkg.uuid4(),
        user_id=gov.bob.id,
        organization_id=gov.org.id,
        title=SECRET_TITLE,
        context={},
    )
    gov.db.add(conversation)
    gov.db.commit()
    gov.db.refresh(conversation)
    gov.db.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(),
            conversation_id=conversation.id,
            role=ROLE_USER,
            content=SECRET_TEXT,
        )
    )
    gov.db.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(),
            conversation_id=conversation.id,
            role=ROLE_ASSISTANT,
            content="ok",
        )
    )
    gov.db.commit()
    with gov.acting_as(gov.bob):
        response = client.get(f"/api/chat/conversations/{conversation.uuid}/export")
    assert response.status_code == status.HTTP_200_OK, response.text

    event = _one(events, AuditEventType.CONTENT_EXPORT)
    assert event["resource_type"] == "conversation"
    assert event["resource_uuid"] == str(conversation.uuid)
    assert event["details"] == {"surface": "chat", "format": "markdown", "message_count": 2}


# --------------------------------------------------------------------------- #
# Membership and sharing: governance events, always recorded
# --------------------------------------------------------------------------- #


def test_add_and_remove_media_record_membership_changes(client, gov, events):
    collection = gov.collection(gov.alice)
    media_file = gov.file(gov.bob)
    base = f"/api/collections/{collection.uuid}/media"
    body = {"media_file_ids": [str(media_file.uuid)]}
    with gov.acting_as(gov.alice):
        assert client.post(base, json=body).status_code == status.HTTP_200_OK
        added = _one(events, AuditEventType.COLLECTION_MEMBERSHIP_CHANGE)
        assert client.request("DELETE", base, json=body).status_code == status.HTTP_200_OK

    changes = _of(events, AuditEventType.COLLECTION_MEMBERSHIP_CHANGE)
    assert len(changes) == 2
    removed = changes[1]
    assert (added["details"]["added"], added["details"]["removed"]) == (1, 0)
    assert (removed["details"]["added"], removed["details"]["removed"]) == (0, 1)
    for event in changes:
        assert event["resource_type"] == "collection"
        assert event["resource_uuid"] == str(collection.uuid)
        assert event["organization_id"] == gov.org.id
        assert event["tenant_scope"] == TENANT_SCOPE_ORG
        assert event["details"]["file_uuids"] == [str(media_file.uuid)]
        assert event["details"]["source"] == "manual"


def test_removing_a_file_that_was_never_a_member_records_nothing(client, gov, events):
    collection = gov.collection(gov.alice)
    stranger = gov.file(gov.bob)
    with gov.acting_as(gov.alice):
        response = client.request(
            "DELETE",
            f"/api/collections/{collection.uuid}/media",
            json={"media_file_ids": [str(stranger.uuid)]},
        )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert _of(events, AuditEventType.COLLECTION_MEMBERSHIP_CHANGE) == []


def test_an_auto_label_join_is_a_membership_change_by_the_files_owner(gov, events):
    collection = gov.collection(gov.alice, name="Retail")
    media_file = gov.file(gov.alice)
    AutoLabelService(gov.db)._announce_collection_joins(media_file, [collection])

    event = _one(events, AuditEventType.COLLECTION_MEMBERSHIP_CHANGE)
    assert event["user_id"] == gov.alice.id
    assert event["resource_uuid"] == str(collection.uuid)
    assert event["organization_id"] == gov.org.id
    assert event["details"] == {
        "added": 1,
        "removed": 0,
        "file_uuids": [str(media_file.uuid)],
        "source": "auto_label",
    }


def test_the_topic_suggestion_path_is_what_calls_the_auto_label_announcement(gov, events):
    """The announcement is wired into ``auto_apply_suggestions``, not merely defined."""
    media_file = gov.file(gov.alice)
    suggestion = TopicSuggestion(
        media_file_id=media_file.id,
        user_id=gov.alice.id,
        suggested_tags=[],
        suggested_collections=[{"name": "Retail", "confidence": 0.99}],
    )
    gov.db.add(suggestion)
    gov.db.commit()
    AutoLabelService(gov.db).auto_apply_suggestions(
        media_file, suggestion, gov.alice.id, apply_tags=False
    )

    event = _one(events, AuditEventType.COLLECTION_MEMBERSHIP_CHANGE)
    assert event["details"]["source"] == "auto_label"
    assert event["details"]["file_uuids"] == [str(media_file.uuid)]


def test_a_collection_share_carries_typed_fields_and_no_collection_name(client, gov, events):
    collection = gov.collection(gov.alice, name="Matter 7 review")
    with gov.acting_as(gov.alice):
        response = client.post(
            f"/api/collections/{collection.uuid}/shares",
            json={"target_type": "user", "target_uuid": str(gov.bob.uuid), "permission": "viewer"},
        )
    assert response.status_code == status.HTTP_201_CREATED, response.text

    event = _one(events, AuditEventType.RESOURCE_SHARE)
    assert event["resource_type"] == "collection"
    assert event["resource_uuid"] == str(collection.uuid)
    assert event["tenant_scope"] == TENANT_SCOPE_ORG
    assert event["organization_id"] == gov.org.id
    assert "resource_name" not in event["details"]
    assert "Matter 7" not in json.dumps(event, default=str)


def test_a_personal_collection_share_is_stamped_personal(client, gov, events):
    collection = gov.collection(gov.alice, org=None)
    with gov.acting_as(gov.alice, org=None):
        response = client.post(
            f"/api/collections/{collection.uuid}/shares",
            json={"target_type": "user", "target_uuid": str(gov.bob.uuid), "permission": "viewer"},
        )
    assert response.status_code == status.HTTP_201_CREATED, response.text

    event = _one(events, AuditEventType.RESOURCE_SHARE)
    assert event["tenant_scope"] == TENANT_SCOPE_PERSONAL
    assert event["organization_id"] is None


# --------------------------------------------------------------------------- #
# Cross-cutting rules
# --------------------------------------------------------------------------- #


def test_a_second_view_inside_the_window_writes_none_but_a_download_twice_writes_two(
    client, gov, events, auditing_on, redis, presign
):
    media_file = _org_file(gov)
    with gov.acting_as(gov.bob):
        for _ in range(2):
            assert client.get(f"/api/files/{media_file.uuid}").status_code == status.HTTP_200_OK
        for _ in range(2):
            assert (
                client.post(
                    f"/api/files/{media_file.uuid}/prepare-download",
                    params={"mode": "audio_original"},
                ).status_code
                == status.HTTP_200_OK
            )

    assert len(_of(events, AuditEventType.CONTENT_FILE_VIEW)) == 1
    assert len(_of(events, AuditEventType.CONTENT_MEDIA_ACCESS)) == 2
    assert list(redis.keys.values()) == [content_audit.CONTENT_AUDIT_DEDUP_SECONDS]


def test_another_person_viewing_the_same_file_is_not_collapsed_into_the_first(
    client, gov, events, auditing_on, redis
):
    media_file = _org_file(gov)
    for person in (gov.bob, gov.admin):
        with gov.acting_as(person):
            assert client.get(f"/api/files/{media_file.uuid}").status_code == status.HTTP_200_OK
    assert sorted(e["user_id"] for e in _of(events, AuditEventType.CONTENT_FILE_VIEW)) == sorted(
        [gov.bob.id, gov.admin.id]
    )


def test_with_the_switch_off_no_content_event_is_written_but_governance_still_is(
    client, gov, events, redis
):
    media_file = _org_file(gov)
    _add_segments(gov.db, media_file)
    collection = gov.collection(gov.alice)
    with gov.acting_as(gov.bob):
        assert client.get(f"/api/files/{media_file.uuid}").status_code == status.HTTP_200_OK
        assert client.get(f"/api/files/{media_file.uuid}/stream-url").status_code == 200
        assert (
            client.get(f"/api/files/{media_file.uuid}/export", params={"format": "txt"}).status_code
            == status.HTTP_200_OK
        )
    with gov.acting_as(gov.alice):
        added = client.post(
            f"/api/collections/{collection.uuid}/media",
            json={"media_file_ids": [str(media_file.uuid)]},
        )
        assert added.status_code == status.HTTP_200_OK, added.text

    assert _of(events, *CONTENT_EVENTS) == []
    assert len(_of(events, AuditEventType.COLLECTION_MEMBERSHIP_CHANGE)) == 1


def test_a_setting_on_without_the_capability_still_writes_nothing(
    client, gov, events, db_session, monkeypatch, redis
):
    monkeypatch.setattr(content_audit, "capability_enabled", lambda key, request=None: False)
    set_setting(db_session, AUDIT_CONTENT_ACCESS_SETTING_KEY, True)
    media_file = _org_file(gov)
    with gov.acting_as(gov.bob):
        assert client.get(f"/api/files/{media_file.uuid}").status_code == status.HTTP_200_OK
    assert _of(events, *CONTENT_EVENTS) == []


def test_a_redis_outage_writes_every_view_instead_of_dropping_them(
    client, gov, events, auditing_on, monkeypatch
):
    monkeypatch.setattr("app.core.redis.get_probe_redis", lambda: _BrokenRedis())
    media_file = _org_file(gov)
    with gov.acting_as(gov.bob):
        for _ in range(2):
            assert client.get(f"/api/files/{media_file.uuid}").status_code == status.HTTP_200_OK
    assert len(_of(events, AuditEventType.CONTENT_FILE_VIEW)) == 2


def test_an_audit_failure_does_not_fail_the_request_it_describes(
    client, gov, auditing_on, redis, monkeypatch
):
    def boom(**kwargs):
        raise RuntimeError("audit sink down")

    monkeypatch.setattr(audit_logger, "log", boom)
    media_file = _org_file(gov)
    with gov.acting_as(gov.bob):
        assert client.get(f"/api/files/{media_file.uuid}").status_code == status.HTTP_200_OK


def test_content_events_never_carry_filename_title_or_text(
    client, gov, events, auditing_on, redis, presign
):
    media_file = _org_file(gov, summary_data={"content": SECRET_TEXT, "action_items": []})
    _add_segments(gov.db, media_file)
    collection = gov.collection(gov.alice, name="Confidential matter")
    with gov.acting_as(gov.bob):
        client.get(f"/api/files/{media_file.uuid}")
        client.get(f"/api/files/{media_file.uuid}/stream-url")
        client.post(
            f"/api/files/{media_file.uuid}/prepare-download", params={"mode": "audio_original"}
        )
        client.get(f"/api/files/{media_file.uuid}/export", params={"format": "txt"})
        client.get(f"/api/files/{media_file.uuid}/subtitles")
        client.get(f"/api/files/{media_file.uuid}/summary/export")
    with gov.acting_as(gov.alice):
        client.post(
            f"/api/collections/{collection.uuid}/media",
            json={"media_file_ids": [str(media_file.uuid)]},
        )

    assert len(events) >= 7, "the control: every surface above must have written an event"
    blob = json.dumps(events, default=str)
    for forbidden in (SECRET_TITLE, SECRET_TEXT, media_file.filename, "Confidential matter"):
        assert forbidden not in blob
