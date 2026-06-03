"""Unit tests: the imohash service delegates to the real package consistently.

Verifies path / stream / bytes all agree with the ``imohash`` package itself
(the §2 correctness guarantee). MinIO ranged-read parity is covered separately
in the integration suite (it needs a running MinIO).
"""

from __future__ import annotations

import io

import imohash

from app.services import imohash_service as svc


def _sample_bytes(n: int) -> bytes:
    return bytes((i * 7 + 3) % 256 for i in range(n))


def test_path_stream_bytes_match_package(tmp_path):
    # > 4*SAMPLE_SIZE so the sampling branch (not full-read) is exercised.
    data = _sample_bytes(svc.SAMPLE_SIZE * 8)
    f = tmp_path / "sample.bin"
    f.write_bytes(data)

    from_path = svc.compute_from_path(str(f))
    from_bytes = svc.compute_from_bytes(data)
    from_stream = svc.compute_from_stream(io.BytesIO(data))
    pkg = imohash.hashfile(
        str(f),
        sample_threshhold=svc.SAMPLE_THRESHOLD,
        sample_size=svc.SAMPLE_SIZE,
        hexdigest=True,
    )

    assert from_path == pkg
    assert from_bytes == pkg
    assert from_stream == pkg
    assert len(pkg) == 32  # 16-byte digest, hex


def test_small_file_full_hash(tmp_path):
    # Below the threshold the package hashes the whole file; we must still agree.
    data = _sample_bytes(1024)
    f = tmp_path / "small.bin"
    f.write_bytes(data)
    assert svc.compute_from_path(str(f)) == imohash.hashfile(
        str(f),
        sample_threshhold=svc.SAMPLE_THRESHOLD,
        sample_size=svc.SAMPLE_SIZE,
        hexdigest=True,
    )


def test_different_content_differs(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(_sample_bytes(svc.SAMPLE_SIZE * 8))
    b.write_bytes(_sample_bytes(svc.SAMPLE_SIZE * 8 + 1))  # different size + content
    assert svc.compute_from_path(str(a)) != svc.compute_from_path(str(b))


def test_missing_path_returns_none():
    assert svc.compute_from_path("/nonexistent/file.bin") is None
