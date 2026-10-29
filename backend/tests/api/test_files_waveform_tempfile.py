"""The waveform endpoints must leave no temp file behind, on success or failure (K5).

The endpoint downloads the media object to a temp file, hands the path to ffmpeg-backed
extraction, and must remove it afterwards. Storage and extraction are replaced with
stand-ins that record what the endpoint did; ``tempfile.tempdir`` points at a private
directory so "no leak" is a plain emptiness check on it.
"""

from __future__ import annotations

import io
import os
import tempfile
import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile

_MODULE = "app.api.endpoints.files.waveform"
_PAYLOAD = b"RIFF-not-really-audio" * 1000
_RESULT = {
    "waveform": [0, 128, 255, 64],
    "duration": 5.0,
    "sample_rate": 16000,
    "extracted_samples": 4,
    "expected_duration": 5.0,
}


class _FakeGenerator:
    """Records the temp path it was handed and whether the bytes had been written."""

    seen: list[tuple[str, bytes]] = []
    fail = False

    def _extract_single_waveform(self, path: str, samples: int):
        with open(path, "rb") as fh:
            type(self).seen.append((path, fh.read()))
        if type(self).fail:
            raise RuntimeError("ffmpeg exploded")
        return dict(_RESULT)


@pytest.fixture
def temp_dir(tmp_path, monkeypatch):
    private = tmp_path / "tmp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    return private


@pytest.fixture
def storage(monkeypatch):
    _FakeGenerator.seen = []
    _FakeGenerator.fail = False
    monkeypatch.setattr(f"{_MODULE}.WaveformGenerator", _FakeGenerator)
    monkeypatch.setattr(
        f"{_MODULE}.download_file", lambda path: (io.BytesIO(_PAYLOAD), "audio/wav", len(_PAYLOAD))
    )
    return _FakeGenerator


def _make_file(db_session, owner) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="wave_tmp.wav",
        title="wave_tmp",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=len(_PAYLOAD),
        status="completed",
        duration=5.0,
        is_public=False,
        user_id=owner.id,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


@pytest.mark.parametrize("suffix", ["waveform", "waveform/peaks"])
def test_temp_file_holds_the_download_and_is_removed_after_success(
    client, user_token_headers, normal_user, db_session, temp_dir, storage, suffix
):
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/{suffix}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert len(storage.seen) == 1
    path, content = storage.seen[0]
    assert os.path.dirname(path) == str(temp_dir)
    assert content == _PAYLOAD
    assert os.listdir(temp_dir) == []


@pytest.mark.parametrize("suffix", ["waveform", "waveform/peaks"])
def test_temp_file_is_removed_when_extraction_fails(
    client, user_token_headers, normal_user, db_session, temp_dir, storage, suffix
):
    storage.fail = True
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/{suffix}", headers=user_token_headers)
    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert len(storage.seen) == 1
    assert os.listdir(temp_dir) == []


@pytest.mark.parametrize("suffix", ["waveform", "waveform/peaks"])
def test_temp_file_is_removed_when_the_download_fails(
    client, user_token_headers, normal_user, db_session, temp_dir, storage, monkeypatch, suffix
):
    class _BrokenStream:
        def read(self, size: int = -1) -> bytes:
            raise OSError("connection reset")

    monkeypatch.setattr(f"{_MODULE}.download_file", lambda path: (_BrokenStream(), "audio/wav", 0))
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/{suffix}", headers=user_token_headers)
    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert storage.seen == []
    assert os.listdir(temp_dir) == []
