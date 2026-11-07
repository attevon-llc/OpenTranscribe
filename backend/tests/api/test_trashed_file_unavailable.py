"""A trashed file (``deleted_at`` set) is gone from every normal read surface (#1101).

The trash is a state on the row (v463). Nothing moves a file into it yet (the trash service
comes later); these tests set the columns directly, exactly as that service will, and prove the
read side is already closed: every surface that routes through ``app.services.file_availability``
drops a trashed file, for its owner AND for a single-tenant platform admin, who keeps review
reach over a quarantined file but has none over the trash (only the trash routes reach it).

Every assertion has its control: the same call on the same file before it is trashed.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from fastapi import status

from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember
from app.models.speaker import SpeakerProfile
from app.services import file_availability
from app.services.platform_access import TenancyMode
from app.services.platform_bypass import PlatformBypass

_TITLE = "Brindlequay retrospective"


def _file(db_session, owner, **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    row = MediaFile(
        uuid=file_uuid,
        filename=f"{_TITLE}.wav",
        title=_TITLE,
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status=overrides.pop("status", "completed"),
        is_public=False,
        user_id=owner.id,
        **overrides,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _trash(db_session, media_file: MediaFile, actor) -> None:
    now = datetime.now(UTC)
    media_file.deleted_at = now
    media_file.purge_after = now + timedelta(days=30)
    media_file.trash_origin = "user"
    media_file.deleted_by_user_id = actor.id
    db_session.commit()


def _speaker(db_session, owner, media_file, **kw) -> Speaker:
    row = Speaker(
        uuid=uuid.uuid4(),
        user_id=owner.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        display_name="Ottermere",
        **kw,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


# --- per-resource choke points ----------------------------------------------------------------


def test_file_detail_is_404_for_its_owner_once_trashed(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)
    url = f"/api/files/{media_file.uuid}"
    assert client.get(url, headers=user_token_headers).status_code == status.HTTP_200_OK

    _trash(db_session, media_file, normal_user)

    response = client.get(url, headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert _TITLE not in response.text


def test_file_detail_is_404_for_a_platform_admin_too(
    client, admin_token_headers, normal_user, db_session
):
    """Quarantine has a review exemption for the platform role; the trash has none."""
    media_file = _file(db_session, normal_user)
    url = f"/api/files/{media_file.uuid}"
    assert client.get(url, headers=admin_token_headers).status_code == status.HTTP_200_OK

    _trash(db_session, media_file, normal_user)

    assert client.get(url, headers=admin_token_headers).status_code == status.HTTP_404_NOT_FOUND


def test_speaker_detail_is_404_once_its_file_is_trashed(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)
    speaker = _speaker(db_session, normal_user, media_file)
    url = f"/api/speakers/{speaker.uuid}"
    assert client.get(url, headers=user_token_headers).status_code == status.HTTP_200_OK

    _trash(db_session, media_file, normal_user)

    assert client.get(url, headers=user_token_headers).status_code == status.HTTP_404_NOT_FOUND


# --- lists, facets, counts --------------------------------------------------------------------


def _gallery_uuids(client, headers) -> set[str]:
    response = client.get("/api/files", headers=headers, params={"page_size": 100})
    assert response.status_code == status.HTTP_200_OK, response.text
    return {item["uuid"] for item in response.json()["items"]}


def test_gallery_drops_a_trashed_file_for_owner_and_admin(
    client, user_token_headers, admin_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)
    assert str(media_file.uuid) in _gallery_uuids(client, user_token_headers)
    assert str(media_file.uuid) in _gallery_uuids(client, admin_token_headers)

    _trash(db_session, media_file, normal_user)

    assert str(media_file.uuid) not in _gallery_uuids(client, user_token_headers)
    assert str(media_file.uuid) not in _gallery_uuids(client, admin_token_headers)


def test_owners_facet_drops_the_owner_of_only_a_trashed_file(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)

    def owners() -> set[str]:
        response = client.get("/api/files/owners", headers=user_token_headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        return {str(row["uuid"]) for row in response.json()}

    assert str(normal_user.uuid) in owners()
    _trash(db_session, media_file, normal_user)
    assert str(normal_user.uuid) not in owners()


def test_collections_on_files_refuses_a_trashed_file(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)
    collection = Collection(
        uuid=uuid.uuid4(), name=f"trash-{uuid.uuid4().hex[:8]}", user_id=normal_user.id
    )
    db_session.add(collection)
    db_session.commit()
    db_session.add(
        CollectionMember(
            uuid=uuid.uuid4(), collection_id=collection.id, media_file_id=media_file.id
        )
    )
    db_session.commit()
    params = {"file_uuids": [str(media_file.uuid)]}

    before = client.get("/api/collections/for-files", params=params, headers=user_token_headers)
    assert before.status_code == status.HTTP_200_OK, before.text
    _trash(db_session, media_file, normal_user)
    after = client.get("/api/collections/for-files", params=params, headers=user_token_headers)
    assert after.status_code == status.HTTP_404_NOT_FOUND, after.text


def test_stuck_files_drop_a_trashed_file(client, user_token_headers, normal_user, db_session):
    media_file = _file(db_session, normal_user, status="orphaned")

    def stuck() -> set[str]:
        response = client.get("/api/files/management/stuck", headers=user_token_headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        return {row["uuid"] for row in response.json()["stuck_files"]}

    assert str(media_file.uuid) in stuck()
    _trash(db_session, media_file, normal_user)
    assert str(media_file.uuid) not in stuck()


def test_profile_counts_and_cluster_members_drop_a_trashed_file(
    client, user_token_headers, normal_user, db_session
):
    profile = SpeakerProfile(uuid=uuid.uuid4(), user_id=normal_user.id, name="P trash")
    db_session.add(profile)
    db_session.commit()
    kept = _file(db_session, normal_user)
    trashed = _file(db_session, normal_user)
    _speaker(db_session, normal_user, kept, profile_id=profile.id)
    speaker = _speaker(db_session, normal_user, trashed, profile_id=profile.id)
    cluster = SpeakerCluster(uuid=uuid.uuid4(), user_id=normal_user.id, member_count=1)
    db_session.add(cluster)
    db_session.flush()
    db_session.add(
        SpeakerClusterMember(
            uuid=uuid.uuid4(), cluster_id=cluster.id, speaker_id=speaker.id, confidence=0.9
        )
    )
    db_session.commit()

    def counts() -> tuple[int, int]:
        response = client.get("/api/speaker-profiles/profiles", headers=user_token_headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        entry = next(p for p in response.json() if str(p["uuid"]) == str(profile.uuid))
        return entry["media_count"], entry["instance_count"]

    def members() -> list[dict[str, object]]:
        response = client.get(f"/api/speaker-clusters/{cluster.uuid}", headers=user_token_headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        found: list[dict[str, object]] = response.json()["members"]
        return found

    assert counts() == (2, 2)
    assert len(members()) == 1
    _trash(db_session, trashed, normal_user)
    assert counts() == (1, 1)
    assert members() == []


# --- the rule's own functions -----------------------------------------------------------------


def test_the_sql_form_drops_a_trashed_row_even_with_the_quarantine_exemption(
    db_session, normal_user
):
    media_file = _file(db_session, normal_user)
    base = db_session.query(MediaFile.id).filter(MediaFile.id == media_file.id)
    assert file_availability.exclude_unavailable(base, include_quarantined=True).count() == 1

    _trash(db_session, media_file, normal_user)

    assert file_availability.exclude_unavailable(base).count() == 0
    assert file_availability.exclude_unavailable(base, include_quarantined=True).count() == 0
    assert file_availability.unavailable_among(db_session, [media_file.uuid]) == {
        str(media_file.uuid)
    }
    assert file_availability.unavailable_among(
        db_session, [media_file.uuid], include_quarantined=True
    ) == {str(media_file.uuid)}


def test_the_row_form_refuses_a_trashed_file_to_a_reached_platform_role(
    db_session, normal_user, admin_user, monkeypatch
):
    monkeypatch.setattr("app.services.support_access_service.record_use", lambda *a, **k: None)
    media_file = _file(db_session, normal_user)
    admin = PlatformBypass(user=admin_user, mode=TenancyMode.SINGLE)
    media_file.is_quarantined = True
    assert file_availability.is_unavailable_for(admin, media_file) is False, "control: review reach"
    media_file.is_quarantined = False
    assert file_availability.is_unavailable(media_file, platform_reached=True) is False

    _trash(db_session, media_file, normal_user)

    assert file_availability.is_unavailable(media_file, platform_reached=True) is True
    assert file_availability.is_unavailable_for(admin, media_file) is True


@contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def bridge_session(monkeypatch, db_session):
    """The notification predicates open their OWN session: bridge it to the savepoint."""
    monkeypatch.setattr("app.db.session_utils.session_scope", lambda: _yield_session(db_session))
    return db_session


def test_a_trashed_files_notifications_reach_no_one(bridge_session, normal_user, admin_user):
    media_file = _file(bridge_session, normal_user)
    other = _file(bridge_session, normal_user)
    for recipient in (normal_user, admin_user):
        assert file_availability.is_notification_suppressed(media_file.id, recipient.id) is False

    _trash(bridge_session, media_file, normal_user)

    for recipient in (normal_user, admin_user):
        assert file_availability.is_notification_suppressed(media_file.id, recipient.id) is True
        assert (
            file_availability.is_notification_suppressed_for_uuid(media_file.uuid, recipient.id)
            is True
        )
        assert file_availability.filter_suppressed_file_uuids(
            [media_file.uuid, other.uuid], recipient.id
        ) == [str(other.uuid)]
