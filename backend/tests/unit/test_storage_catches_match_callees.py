"""Handlers around storage calls must catch what the call raises now.

``get_file_url`` / ``delete_file`` / ``download_file`` raise ``StorageError`` (their own
``__cause__`` holds the client error); ``download_temp_audio`` and ``presigned_get_object`` are
raw client calls and raise minio / urllib3 / OS errors. Each test injects the realistic
exception and asserts the documented fallback instead of an escape.
"""

from __future__ import annotations

import io
from typing import Any

import urllib3.exceptions

from app.core.exceptions import StorageError
from app.core.exceptions import StorageUnavailableError


def test_thumbnail_migration_survives_a_refused_delete_of_the_old_jpeg(monkeypatch):
    from app.tasks import thumbnail_migration as tm

    monkeypatch.setattr(tm, "get_file_url", lambda *a, **k: "http://storage/video")
    monkeypatch.setattr(tm, "generate_thumbnail_from_url", lambda url: b"webp-bytes")
    monkeypatch.setattr(tm, "upload_file", lambda **kwargs: "new.webp")

    def _refuse(_name: str) -> None:
        raise StorageError("Storage error while deleting a file: AccessDenied")

    monkeypatch.setattr(tm, "delete_file", _refuse)

    outcome, new_path = tm._migrate_single_thumbnail(
        {"id": 1, "thumbnail_path": "thumbs/a.jpg", "storage_path": "videos/a.mp4"}
    )

    assert (outcome, new_path) == ("migrated", "thumbs/a.webp")


def test_speaker_embedding_staging_falls_back_when_the_temp_wav_fetch_dies_in_urllib3(
    monkeypatch, tmp_path
):
    from app.services import minio_service
    from app.services import temp_audio_probe
    from app.tasks import speaker_embedding_task as task

    monkeypatch.setattr(temp_audio_probe, "temp_audio_available", lambda *a, **k: True)

    def _dead(*args: Any, **kwargs: Any) -> None:
        raise urllib3.exceptions.ProtocolError("Connection aborted")

    monkeypatch.setattr(minio_service, "download_temp_audio", _dead)
    monkeypatch.setattr(
        minio_service, "download_file", lambda _name: (io.BytesIO(b"RIFF"), 4, "audio/wav")
    )
    from app.tasks.transcription import audio_processor

    monkeypatch.setattr(
        audio_processor, "prepare_audio_for_transcription", lambda path, *a, **k: path
    )

    staged = task._stage_audio_for_embeddings(
        "file-uuid",
        {"storage_path": "p", "content_type": "audio/wav", "filename": "a.wav"},
        str(tmp_path),
    )

    assert staged.endswith(".wav")
    assert (tmp_path / "input.wav").read_bytes() == b"RIFF"


def test_waveform_download_helpers_return_none_on_storage_failures(monkeypatch):
    from app.tasks import waveform

    def _unavailable(*args: Any, **kwargs: Any) -> None:
        raise StorageUnavailableError("Storage unavailable while downloading a file")

    monkeypatch.setattr(waveform, "download_file", _unavailable)
    assert waveform._download_to_temp_file("p/a.wav") is None

    def _dead(*args: Any, **kwargs: Any) -> None:
        raise urllib3.exceptions.MaxRetryError(urllib3.HTTPConnectionPool("localhost"), "/x")

    monkeypatch.setattr(waveform, "download_temp_audio", _dead)
    assert waveform._download_temp_audio_safe("u") is None
