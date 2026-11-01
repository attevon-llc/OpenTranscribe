"""Legacy extracted-audio keys (no leading file id) against a real object store.

Cache key hardening put the file id first in every extracted-audio key. Keys written before
that are never read again, so three paths must still remove them: the one-time sweep
(``cache_management_service.reclaim_legacy_audio_cache``), a file's delete
(``VideoProcessingService.clear_derived_cache``) and a takedown
(``media_revocation_cache.remove_cache_copies``). Each test runs on a throwaway bucket, plain
or versioned, that is emptied (every version) and removed in a ``finally``.
"""

from __future__ import annotations

import io
import os
import uuid

import pytest

from app.core.config import settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SKIP_S3", "True").lower() == "true",
        reason="MinIO not reachable — this test needs a real object store",
    ),
]

BUCKET_PREFIX = "e2e-legacy-audio-"

LEGACY = [
    "derived/meeting_audio_mp3.mp3",
    "derived/meeting_audio_wav.wav",
    "derived/talk_audio_original",
]
KEPT = [
    "derived/12_meeting_audio_mp3.mp3",  # current, id-prefixed
    "derived/meeting_with_speakers.mp4",  # burned-in video, never touched
    "derived/12_meeting_with_speakers_rab12.mp4",
    "derived/2024_q1_audio_mp3.mp3",  # cannot be told from an id-prefixed key: left alone
    "bulk/job.zip",
]


def _root():
    from app.services.minio_service import minio_client

    return minio_client


def _put(bucket: str, key: str, body: bytes = b"audio") -> None:
    _root().put_object(bucket, key, io.BytesIO(body), len(body))


def _names(bucket: str) -> set[str]:
    return {o.object_name for o in _root().list_objects(bucket, recursive=True)}


def _versions(bucket: str) -> list[tuple[str, str | None]]:
    return [
        (o.object_name, o.version_id)
        for o in _root().list_objects(bucket, recursive=True, include_version=True)
    ]


def _empty_and_remove(bucket: str) -> None:
    from minio.deleteobjects import DeleteObject

    targets = [DeleteObject(name, vid) for name, vid in _versions(bucket)]
    for _ in _root().remove_objects(bucket, iter(targets)):
        pass
    _root().remove_bucket(bucket)


def _seed(monkeypatch, name: str, *, versioned: bool) -> None:
    from minio.commonconfig import ENABLED
    from minio.versioningconfig import VersioningConfig

    if versioned:
        _root().set_bucket_versioning(name, VersioningConfig(ENABLED))
    monkeypatch.setattr(settings, "CACHE_BUCKET_NAME", name)
    for key in LEGACY + KEPT:
        _put(name, key)


@pytest.fixture()
def plain_bucket(monkeypatch):
    name = f"{BUCKET_PREFIX}{uuid.uuid4().hex[:12]}"
    _root().make_bucket(name)
    try:
        _seed(monkeypatch, name, versioned=False)
        yield name
    finally:
        _empty_and_remove(name)


@pytest.fixture()
def versioned_bucket(monkeypatch):
    name = f"{BUCKET_PREFIX}{uuid.uuid4().hex[:12]}"
    _root().make_bucket(name)
    try:
        _seed(monkeypatch, name, versioned=True)
        yield name
    finally:
        _empty_and_remove(name)


def test_the_sweep_removes_only_legacy_audio_keys_and_is_safe_to_rerun(plain_bucket):
    from app.services import cache_management_service as cms

    first = cms.reclaim_legacy_audio_cache()

    assert (first.matched, first.deleted, first.failed, first.complete) == (3, 3, 0, True)
    assert _names(plain_bucket) == set(KEPT)

    again = cms.reclaim_legacy_audio_cache()
    assert (again.matched, again.deleted, again.complete) == (0, 0, True)
    assert _names(plain_bucket) == set(KEPT)


def test_a_pass_cut_at_the_limit_is_incomplete_and_the_next_one_finishes(plain_bucket):
    from app.services import cache_management_service as cms

    cut = cms.reclaim_legacy_audio_cache(limit=1)
    assert (cut.deleted, cut.complete) == (1, False)

    rest = cms.reclaim_legacy_audio_cache()
    assert (rest.deleted, rest.complete) == (2, True)
    assert _names(plain_bucket) == set(KEPT)


def test_on_a_versioned_bucket_every_version_of_a_legacy_key_is_gone(versioned_bucket):
    from app.services import cache_management_service as cms

    _put(versioned_bucket, LEGACY[0], b"second version")
    assert sum(1 for n, _ in _versions(versioned_bucket) if n == LEGACY[0]) == 2

    result = cms.reclaim_legacy_audio_cache()

    assert result.complete is True
    remaining = {n for n, _ in _versions(versioned_bucket)}
    assert remaining.isdisjoint(LEGACY)
    assert set(KEPT) <= remaining


def test_deleting_a_file_also_removes_its_legacy_audio_keys(plain_bucket):
    from app.services.minio_service import MinIOService
    from app.services.video_processing_service import VideoProcessingService

    VideoProcessingService(MinIOService()).clear_derived_cache(12, "meeting.mp4")

    names = _names(plain_bucket)
    assert "derived/meeting_audio_mp3.mp3" not in names
    assert "derived/meeting_audio_wav.wav" not in names
    assert "derived/12_meeting_audio_mp3.mp3" not in names
    assert "derived/talk_audio_original" in names  # another file's legacy key


def test_a_takedown_removes_and_reports_the_files_legacy_audio_keys(plain_bucket):
    from app.services import media_revocation_cache as cache

    results = cache.remove_cache_copies(12, filename="meeting.mp4", client=_root())

    # Two id-prefixed keys of file 12 and its two legacy audio keys.
    assert [(r.role, r.write.outcome) for r in results] == [("derived", "removed")] * 4
    names = _names(plain_bucket)
    gone = {
        "derived/12_meeting_audio_mp3.mp3",
        "derived/12_meeting_with_speakers_rab12.mp4",
        "derived/meeting_audio_mp3.mp3",
        "derived/meeting_audio_wav.wav",
    }
    assert gone.isdisjoint(names)
    assert {"derived/talk_audio_original", "derived/meeting_with_speakers.mp4"} <= names
