"""Fingerprinting an in-place MinIO object is best-effort; the ranged reader rejects bad seeks."""

from __future__ import annotations

import logging

import pytest
import urllib3.exceptions

from app.models.media import MediaFile
from app.services import imohash_service
from app.services import storage_recovery_service


@pytest.mark.parametrize("error", [OSError("reset"), urllib3.exceptions.HTTPError("pool")])
def test_fingerprint_failure_leaves_the_row_unfingerprinted(monkeypatch, caplog, error):
    def _boom(*_a, **_k):
        raise error

    monkeypatch.setattr(imohash_service, "compute_from_minio", _boom)
    media_file = MediaFile(storage_path="a/b.mp3", file_size=10, imohash=None)

    with caplog.at_level(logging.DEBUG, logger=storage_recovery_service.logger.name):
        storage_recovery_service.fingerprint_object(media_file)  # must not raise

    assert media_file.imohash is None
    assert "imohash compute failed for a/b.mp3" in caplog.text


def test_range_reader_rejects_an_unknown_whence():
    reader = imohash_service._MinioRangeReader("obj", 100)

    with pytest.raises(ValueError, match="Invalid whence: 9"):
        reader.seek(0, 9)
