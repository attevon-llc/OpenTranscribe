"""``temp_audio_exists`` must distinguish "no temp WAV" from "storage unreachable".

It used to return False for *any* exception, so a MinIO outage read as "no preprocessed
audio" and the rediarize / speaker-embedding tasks silently took the expensive path
(re-download the original, re-run ffmpeg) with no hint in the log that storage was down.
"""

from __future__ import annotations

import logging

import pytest
import urllib3
from minio.error import S3Error
from urllib3.exceptions import MaxRetryError
from urllib3.response import HTTPResponse

from app.services import temp_audio_probe
from app.tasks import rediarize_task
from app.utils import scratch_volume

FILE_UUID = "55555555-5555-5555-5555-555555555555"


def _s3_error(code: str, status: int) -> S3Error:
    return S3Error(
        response=HTTPResponse(status=status),
        code=code,
        message=f"simulated {code}",
        resource="/x",
        request_id="req",
        host_id="host",
    )


def _max_retry() -> MaxRetryError:
    return MaxRetryError(urllib3.HTTPConnectionPool("minio", 9000), "/x", None)


@pytest.fixture
def no_scratch(tmp_path, monkeypatch):
    """Scratch volume empty, so only the MinIO leg decides the answer."""
    monkeypatch.setattr(scratch_volume, "SCRATCH_DIR", tmp_path / "scratch")


def _stat_raises(monkeypatch, exc: Exception) -> None:
    def _stat(*_a, **_k):
        raise exc

    monkeypatch.setattr(temp_audio_probe.minio_client, "stat_object", _stat)


@pytest.mark.parametrize("code", ["NoSuchKey", "NoSuchBucket", "ResourceNotFound"])
def test_genuinely_absent_object_reads_as_false(no_scratch, monkeypatch, code):
    _stat_raises(monkeypatch, _s3_error(code, 404))

    assert temp_audio_probe.temp_audio_exists(FILE_UUID) is False


def test_present_object_reads_as_true(no_scratch, monkeypatch):
    monkeypatch.setattr(temp_audio_probe.minio_client, "stat_object", lambda *_a, **_k: object())

    assert temp_audio_probe.temp_audio_exists(FILE_UUID) is True


@pytest.mark.parametrize(
    "exc",
    [
        _max_retry(),
        TimeoutError("read timed out"),
        _s3_error("InternalError", 500),
        _s3_error("AccessDenied", 403),
    ],
    ids=["max-retry", "timeout", "s3-500", "access-denied"],
)
def test_outage_is_not_reported_as_absent(no_scratch, monkeypatch, exc):
    _stat_raises(monkeypatch, exc)

    with pytest.raises(temp_audio_probe.TempAudioUnavailableError) as raised:
        temp_audio_probe.temp_audio_exists(FILE_UUID)

    assert raised.value.__cause__ is exc


def test_rediarize_logs_that_storage_was_unreachable_and_still_falls_back(
    no_scratch, monkeypatch, caplog
):
    """The cheap path is skipped because storage is down, and the log must say so."""
    _stat_raises(monkeypatch, _max_retry())
    fetched_temp: list[str] = []
    monkeypatch.setattr(
        rediarize_task, "download_temp_audio", lambda *_a, **_k: fetched_temp.append("x")
    )

    class _FallbackReachedError(Exception):
        pass

    def _download_original(_path):
        raise _FallbackReachedError

    monkeypatch.setattr(rediarize_task, "download_file", _download_original)

    with caplog.at_level(logging.WARNING, logger=rediarize_task.logger.name):
        with pytest.raises(_FallbackReachedError):
            rediarize_task._prepare_audio("media/1/a.wav", "audio/wav", "a.wav", FILE_UUID)

    assert fetched_temp == []
    assert any("unreachable" in r.getMessage() for r in caplog.records)
