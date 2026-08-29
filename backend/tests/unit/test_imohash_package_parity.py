"""Unit tests: the imohash service delegates to the real package consistently.

Verifies path / stream / bytes all agree with the ``imohash`` package itself
(the §2 correctness guarantee). MinIO ranged-read parity is covered separately
in the integration suite (it needs a running MinIO).
"""

from __future__ import annotations

import io

import imohash
import pytest

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


# ---------------------------------------------------------------------------
# Cross-language contract with the browser (issue #342)
# ---------------------------------------------------------------------------

# The SPA computes the same imohash before uploading, so a duplicate can be caught
# without moving any bytes (`frontend/src/lib/services/fileFingerprint.ts`). These
# are the identical vectors asserted in `fileFingerprint.test.ts`; both files must
# be updated together, and either going red means the browser and the server have
# stopped agreeing on what "same file" means.
_BROWSER_VECTORS = {
    b"": "00000000000000000000000000000000",
    b"abc": "03963f3f3fad78673ba2744126ca2d52",
}


def _pattern(n: int) -> bytes:
    """`(i * 31 + 7) % 256` — the pattern the shared vectors are generated from."""
    return bytes((i * 31 + 7) % 256 for i in range(n))


def test_browser_fingerprint_vectors_are_reproducible():
    """Pin the exact digests the browser implementation asserts."""
    for data, expected in _BROWSER_VECTORS.items():
        assert svc.compute_from_bytes(data) == expected

    assert svc.compute_from_bytes(bytes(i % 256 for i in range(4096))) == (
        "8020a803a564957a836898c60fbb77bb"
    )
    # Exactly at, and above, the sampling threshold.
    assert svc.compute_from_bytes(_pattern(128 * 1024)) == "80800833394f6067f0a5e566b8d64210"
    assert svc.compute_from_bytes(_pattern(200 * 1024)) == "80c00c33394f6067f0a5e566b8d64210"
    assert svc.compute_from_bytes(_pattern(8 * 1024 * 1024)) == "80808004394f6067f0a5e566b8d64210"


def test_compute_from_minio_propagates_a_storage_outage(monkeypatch):
    """B2: a transient MinIO error must not be swallowed into the same ``None``
    a genuinely absent object returns — that silently degrades dedup to "found
    nothing new" with no signal it was actually a storage outage."""
    from minio.error import S3Error

    outage = S3Error(
        response=None,
        code="InternalError",
        message="simulated storage outage",
        resource="/x",
        request_id="test",
        host_id="test",
    )

    def _raise(*_a, **_k):
        raise outage

    monkeypatch.setattr("app.services.minio_service.object_exists_and_size", _raise)

    with pytest.raises(S3Error):
        svc.compute_from_minio("some/object.wav", size=None)


def test_compute_from_minio_returns_none_for_a_genuinely_new_file(monkeypatch):
    """Control: a real absent/empty object still returns None, not an exception —
    B2's fix must not turn every miss into a raised error."""
    monkeypatch.setattr("app.services.minio_service.object_exists_and_size", lambda *a, **k: None)
    assert svc.compute_from_minio("some/object.wav", size=None) is None


def test_multi_gigabyte_sizes_are_encoded_losslessly(tmp_path):
    """Sizes past 2^32 must survive the varint prefix.

    This is the range the whole change exists for: the browser's varint has to
    avoid JavaScript's int32-coercing bitwise operators, and these two digests are
    what `fileFingerprint.test.ts` checks its output against. Written as sparse
    files so the assertion costs no disk.
    """
    for size, expected in (
        (6442450944, "8080808018f5f56d948936e07fad6ae3"),
        (5000000000, "80e497d012f5f56d948936e07fad6ae3"),
    ):
        sparse = tmp_path / f"sparse_{size}.bin"
        with open(sparse, "wb") as fh:
            fh.truncate(size)
        assert svc.compute_from_path(str(sparse)) == expected
