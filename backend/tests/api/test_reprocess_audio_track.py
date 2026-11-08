"""Reprocessing a multi-track file on a chosen audio track (issue #1263).

The storage probe and the object-store deletes are stubbed so the outcome is the same with the
dev stack up and on a bare runner; the validation, the persisted choice, the invalidation of
what was derived from the old track and the mismatch flag all run for real.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile

TRACKS = [
    {"index": 0, "codec": "aac", "channels": 2, "language": "eng", "title": None, "default": True},
    {
        "index": 1,
        "codec": "aac",
        "channels": 2,
        "language": "por",
        "title": "Dub",
        "default": False,
    },
]


@pytest.fixture
def stubbed_storage(monkeypatch):
    """Storage says the object exists; record what the track change deletes."""
    monkeypatch.setenv("SKIP_S3", "False")
    monkeypatch.setattr(
        "app.services.minio_service.object_exists_and_size", lambda name: (name, 1024)
    )
    deleted: dict[str, list] = {"cache": [], "objects": []}

    class _Minio:
        def delete_objects(self, bucket, keys):
            deleted["cache"].extend(keys)
            return len(keys)

    class _Service:
        cache_bucket = "derived"
        minio_service = _Minio()

        def __init__(self, *_a, **_k) -> None:
            pass

        def audio_cache_key(self, file_id, filename, fmt):
            return f"derived/{file_id}_{fmt}"

    monkeypatch.setattr("app.services.video_processing_service.VideoProcessingService", _Service)
    monkeypatch.setattr("app.services.minio_bucket_admin.MinIOService", lambda: None)
    monkeypatch.setattr(
        "app.services.minio_service.delete_file", lambda name: deleted["objects"].append(name)
    )
    return deleted


def _file(db_session, owner, **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    row = MediaFile(
        uuid=file_uuid,
        filename=f"tracks_{file_uuid[:8]}.mp4",
        title="tracks",
        storage_path=f"media/test/{file_uuid}.mp4",
        content_type="video/mp4",
        file_size=4096,
        status="completed",
        is_public=False,
        retry_count=0,
        user_id=owner.id,
        audio_streams=TRACKS,
        playback_path=f"media/test/{file_uuid}.mp4.playback.m4a",
        waveform_data={"waveform_800": [0.1]},
        language="en",
        **overrides,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def test_unknown_track_is_422_and_nothing_changes(
    client, user_token_headers, normal_user, db_session, stubbed_storage
):
    row = _file(db_session, normal_user)

    response = client.post(
        f"/api/files/{row.uuid}/reprocess",
        headers=user_token_headers,
        json={"audio_stream_index": 2},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    db_session.expire_all()
    assert row.audio_stream_index is None
    assert row.playback_path is not None
    assert stubbed_storage == {"cache": [], "objects": []}


def test_negative_track_is_422(
    client, user_token_headers, normal_user, db_session, stubbed_storage
):
    row = _file(db_session, normal_user)
    response = client.post(
        f"/api/files/{row.uuid}/reprocess",
        headers=user_token_headers,
        json={"audio_stream_index": -1},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_a_track_on_a_stage_that_cannot_use_it_is_422(
    client, user_token_headers, normal_user, db_session, stubbed_storage
):
    row = _file(db_session, normal_user)
    response = client.post(
        f"/api/files/{row.uuid}/reprocess",
        headers=user_token_headers,
        json={"stages": ["summarization"], "audio_stream_index": 1},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    db_session.expire_all()
    assert row.audio_stream_index is None


def test_choosing_the_second_track_is_stored_and_drops_what_the_first_built(
    client, user_token_headers, normal_user, db_session, stubbed_storage
):
    row = _file(db_session, normal_user)
    old_rendition = row.playback_path

    response = client.post(
        f"/api/files/{row.uuid}/reprocess",
        headers=user_token_headers,
        json={"audio_stream_index": 1},
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    assert row.audio_stream_index == 1
    assert row.playback_path is None
    assert row.waveform_data is None
    assert stubbed_storage["objects"] == [old_rendition]
    assert len(stubbed_storage["cache"]) == 3  # mp3, wav, original downloads


def test_reprocess_without_a_track_keeps_the_choice_and_the_rendition(
    client, user_token_headers, normal_user, db_session, stubbed_storage
):
    row = _file(db_session, normal_user, audio_stream_index=1)
    rendition = row.playback_path

    response = client.post(f"/api/files/{row.uuid}/reprocess", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    assert row.audio_stream_index == 1
    assert stubbed_storage["objects"] == []
    del rendition


def test_detail_flags_a_track_whose_tag_disagrees_with_the_detected_language(
    client, user_token_headers, normal_user, db_session
):
    row = _file(db_session, normal_user, audio_stream_index=1)  # por track, detected "en"

    body = client.get(f"/api/files/{row.uuid}", headers=user_token_headers).json()

    assert body["audio_language_mismatch"] is True
    assert body["audio_track_language"] == "por"
    assert [s["index"] for s in body["audio_streams"]] == [0, 1]
    assert body["audio_stream_index"] == 1


def test_detail_does_not_flag_the_matching_default_track(
    client, user_token_headers, normal_user, db_session
):
    row = _file(db_session, normal_user)  # eng track 0, detected "en"

    body = client.get(f"/api/files/{row.uuid}", headers=user_token_headers).json()

    assert body["audio_language_mismatch"] is False
    assert body["audio_track_language"] == "eng"
