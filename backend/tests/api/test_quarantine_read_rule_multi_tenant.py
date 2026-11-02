"""Quarantine read rule at the three route sites, in multi-tenant mode with no grant.

A platform admin that the bypass did NOT reach (multi mode, no support-access grant) is
not exempt from the quarantine read rule. Each route below used to take the exemption
from the bare admin role. The single-mode controls keep the review path honest: the same
admin, same row, single mode, still gets through.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.core.config import settings
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment


@pytest.fixture
def multi_mode(monkeypatch):
    monkeypatch.setattr(settings, "TENANCY_MODE", "multi")


@pytest.fixture
def single_mode(monkeypatch):
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")


@pytest.fixture
def quarantined_world(db_session, admin_user):
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="quarantine_rule.wav",
        title="quarantine_rule",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        is_public=False,
        is_quarantined=True,
        user_id=admin_user.id,
    )
    db_session.add(media_file)
    db_session.commit()
    speaker = Speaker(
        uuid=uuid.uuid4(),
        user_id=admin_user.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        display_name="Alex",
    )
    segment = TranscriptSegment(
        uuid=uuid.uuid4(),
        media_file_id=media_file.id,
        speaker_id=None,
        start_time=0.0,
        end_time=1.0,
        text="segment text",
    )
    collection = Collection(
        uuid=uuid.uuid4(), name=f"quarantine-rule-{uuid.uuid4().hex[:8]}", user_id=admin_user.id
    )
    db_session.add_all([speaker, segment, collection])
    db_session.commit()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db_session.commit()
    return media_file, speaker, segment, collection


def test_collection_detail_hides_the_member_from_an_unreached_admin(
    client, admin_token_headers, quarantined_world, multi_mode
):
    _file, _speaker, _segment, collection = quarantined_world
    response = client.get(f"/api/collections/{collection.uuid}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["media_files"] == []


def test_collection_detail_lists_the_member_for_a_single_mode_admin(
    client, admin_token_headers, quarantined_world, single_mode
):
    media_file, _speaker, _segment, collection = quarantined_world
    response = client.get(f"/api/collections/{collection.uuid}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    assert [f["uuid"] for f in response.json()["media_files"]] == [str(media_file.uuid)]


def test_cluster_media_preview_is_refused_for_an_unreached_admin(
    client, admin_token_headers, quarantined_world, multi_mode
):
    _file, speaker, _segment, _collection = quarantined_world
    response = client.get(
        f"/api/speaker-clusters/speakers/{speaker.uuid}/media-preview",
        headers=admin_token_headers,
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert "media_url" not in response.text


def test_cluster_media_preview_is_served_to_a_single_mode_admin(
    client, admin_token_headers, quarantined_world, single_mode
):
    _file, speaker, _segment, _collection = quarantined_world
    response = client.get(
        f"/api/speaker-clusters/speakers/{speaker.uuid}/media-preview",
        headers=admin_token_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.text


def test_segment_speaker_update_is_refused_for_an_unreached_admin(
    client, admin_token_headers, quarantined_world, multi_mode, monkeypatch
):
    monkeypatch.setattr(
        "app.services.search.reindex_dispatch.dispatch_transcript_reindex", lambda **kw: None
    )
    _file, speaker, segment, _collection = quarantined_world
    response = client.put(
        f"/api/transcripts/segments/{segment.uuid}/speaker",
        json={"speaker_uuid": str(speaker.uuid)},
        headers=admin_token_headers,
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text


def test_segment_speaker_update_reaches_the_handler_for_a_single_mode_admin(
    client, admin_token_headers, quarantined_world, single_mode, monkeypatch
):
    monkeypatch.setattr(
        "app.services.search.reindex_dispatch.dispatch_transcript_reindex", lambda **kw: None
    )
    _file, speaker, segment, _collection = quarantined_world
    response = client.put(
        f"/api/transcripts/segments/{segment.uuid}/speaker",
        json={"speaker_uuid": str(speaker.uuid)},
        headers=admin_token_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.text
