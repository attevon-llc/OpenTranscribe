"""Unit tests for the media mirror sync engine (issue #242).

Covers the pure planning helpers (compare logic, prefix exclusion), the copy loop
(counters, per-object error containment, throttle, ``max_objects`` bound), and the
**never-delete invariant** — behaviorally (pre-existing destination content survives
every run) and structurally (the engine module contains no delete primitives).
All against fake in-memory listings/destinations; no MinIO required.
"""

from __future__ import annotations

import inspect
from unittest import mock

import pytest

from app.services import media_mirror_engine as eng
from app.services.media_mirror_engine import SourceObject


class FakeDestination:
    """In-memory destination: key → (size, etag). Copy failures injectable.

    Deliberately implements ONLY the ``MirrorDestination`` protocol (lookup + copy) —
    there is no delete to call even by accident.
    """

    def __init__(self, existing: dict[str, tuple[int, str | None]] | None = None):
        self.objects: dict[str, tuple[int, str | None]] = dict(existing or {})
        self.copied_keys: list[str] = []
        self.fail_keys: set[str] = set()

    def lookup(self, key: str) -> tuple[int, str | None] | None:
        return self.objects.get(key)

    def copy(self, obj: SourceObject) -> None:
        if obj.key in self.fail_keys:
            raise OSError(f"injected copy failure for {obj.key}")
        self.objects[obj.key] = (obj.size, obj.etag)
        self.copied_keys.append(obj.key)


# =============================================================================
# Compare logic (should_copy)
# =============================================================================
def test_copies_missing_object():
    assert eng.should_copy(SourceObject("a", 10, "e1"), None) is True


def test_copies_on_size_mismatch():
    assert eng.should_copy(SourceObject("a", 10, "e1"), (9, "e1")) is True


def test_skips_identical_size_and_etag():
    assert eng.should_copy(SourceObject("a", 10, '"abc123"'), (10, "abc123")) is False


def test_copies_on_simple_etag_mismatch():
    assert eng.should_copy(SourceObject("a", 10, "abc"), (10, "def")) is True


def test_multipart_etags_fall_back_to_size_only():
    # Multipart ETags depend on chunking — same bytes produce different values, so
    # they must never trigger a re-copy when sizes match.
    assert eng.should_copy(SourceObject("a", 10, "abc-4"), (10, "zzz-7")) is False
    assert eng.should_copy(SourceObject("a", 10, "abc-4"), (10, "plain")) is False


def test_missing_etag_falls_back_to_size_only():
    assert eng.should_copy(SourceObject("a", 10, None), (10, None)) is False
    assert eng.should_copy(SourceObject("a", 10, "abc"), (10, None)) is False


# =============================================================================
# Prefix exclusion
# =============================================================================
@pytest.mark.parametrize(
    "key",
    [
        "temp/preprocess/uuid/audio.wav",  # preprocessed-audio staging
        "derived/whatever.mp4",  # defensive: derived cache prefix
        "bulk/job-1234.zip",  # defensive: bulk export zips
    ],
)
def test_regenerable_prefixes_excluded(key):
    assert eng.is_excluded(key) is True


@pytest.mark.parametrize(
    "key",
    [
        "user_1/file_42/original.mp4",  # irreplaceable original
        "user_1/file_42/thumbnail.webp",  # thumbnail — mirrored
        "avatars/3/profile-uuid.png",  # user-uploaded avatar
        "user_1/file_42/temp/nested.mp4",  # 'temp' only excluded as a top prefix
    ],
)
def test_media_keys_included(key):
    assert eng.is_excluded(key) is False


def test_excluded_objects_never_reach_destination():
    dest = FakeDestination()
    source = [
        SourceObject("temp/preprocess/u/audio.wav", 5),
        SourceObject("user_1/file_1/a.mp4", 100),
    ]
    result = eng.execute_mirror(source, dest)
    assert result["objects_excluded"] == 1
    assert result["objects_copied"] == 1
    assert dest.copied_keys == ["user_1/file_1/a.mp4"]


# =============================================================================
# Copy loop: counters, incrementality
# =============================================================================
def test_execute_mirror_counts_and_bytes():
    dest = FakeDestination(existing={"user_1/f/kept.mp4": (50, None)})
    source = [
        SourceObject("user_1/f/kept.mp4", 50),  # up to date → skip
        SourceObject("user_1/f/new.mp4", 100),  # missing → copy
        SourceObject("user_1/f/changed.mp4", 70),  # will be copied (missing)
        SourceObject("temp/x", 1),  # excluded
    ]
    result = eng.execute_mirror(source, dest)
    assert result["objects_scanned"] == 4
    assert result["objects_skipped"] == 1
    assert result["objects_copied"] == 2
    assert result["objects_excluded"] == 1
    assert result["objects_failed"] == 0
    assert result["bytes_copied"] == 170


def test_second_pass_is_incremental():
    dest = FakeDestination()
    source = [SourceObject(f"user_1/f/{i}.mp4", 10 * i) for i in range(1, 6)]
    first = eng.execute_mirror(source, dest)
    assert first["objects_copied"] == 5
    second = eng.execute_mirror(source, dest)
    assert second["objects_copied"] == 0
    assert second["objects_skipped"] == 5


# =============================================================================
# Never-delete invariant
# =============================================================================
def test_never_deletes_destination_extras():
    # Objects that exist ONLY at the destination (deleted from source, ransomware
    # recovery copies, etc.) must survive every run untouched.
    extras = {
        "user_9/file_9/deleted-from-source.mp4": (123, None),
        "unrelated/manual-copy.bin": (7, None),
    }
    dest = FakeDestination(existing=dict(extras))
    eng.execute_mirror([SourceObject("user_1/f/a.mp4", 10)], dest)
    for key, meta in extras.items():
        assert dest.objects[key] == meta


def test_engine_module_has_no_delete_primitives():
    # Structural half of the invariant: the engine source must not contain any
    # object/file deletion call. If one is ever added, this fails loudly.
    src = inspect.getsource(eng)
    for forbidden in (
        "remove_object",
        "delete_object",
        "delete_objects",
        ".unlink",
        "os.remove",
        "rmtree",
        "rmdir",
    ):
        assert forbidden not in src, f"never-delete invariant violated: {forbidden!r} in engine"


def test_destination_protocol_has_no_delete():
    assert not any("delete" in name or "remove" in name for name in dir(FakeDestination()))
    protocol_members = [m for m in dir(eng.MirrorDestination) if not m.startswith("_")]
    assert set(protocol_members) == {"lookup", "copy"}


# =============================================================================
# Error containment, max_objects, throttle
# =============================================================================
def test_one_failed_object_does_not_abort_the_run():
    dest = FakeDestination()
    dest.fail_keys = {"user_1/f/bad.mp4"}
    source = [
        SourceObject("user_1/f/a.mp4", 1),
        SourceObject("user_1/f/bad.mp4", 2),
        SourceObject("user_1/f/z.mp4", 3),
    ]
    result = eng.execute_mirror(source, dest)
    assert result["objects_failed"] == 1
    assert result["objects_copied"] == 2
    assert len(result["errors"]) == 1
    assert "bad.mp4" in result["errors"][0]


def test_error_sample_is_bounded_but_count_exact():
    dest = FakeDestination()
    source = [SourceObject(f"user_1/f/{i}.mp4", i) for i in range(25)]
    dest.fail_keys = {o.key for o in source}
    result = eng.execute_mirror(source, dest)
    assert result["objects_failed"] == 25
    assert len(result["errors"]) == eng.MAX_RECORDED_ERRORS


def test_max_objects_bounds_the_run():
    dest = FakeDestination()
    source = (SourceObject(f"user_1/f/{i}.mp4", 1) for i in range(1000))
    result = eng.execute_mirror(source, dest, max_objects=5)
    assert result["objects_scanned"] == 5
    assert result["objects_copied"] == 5


def test_max_objects_none_means_all():
    dest = FakeDestination()
    result = eng.execute_mirror(
        [SourceObject(f"user_1/f/{i}.mp4", 1) for i in range(7)], dest, max_objects=None
    )
    assert result["objects_scanned"] == 7


def test_throttle_sleeps_between_objects():
    dest = FakeDestination()
    source = [SourceObject(f"user_1/f/{i}.mp4", 1) for i in range(3)]
    with mock.patch("app.services.media_mirror_engine.time.sleep") as sleep:
        eng.execute_mirror(source, dest, throttle_ms=250)
    assert sleep.call_count == 3
    sleep.assert_called_with(0.25)


def test_no_throttle_means_no_sleep():
    dest = FakeDestination()
    with mock.patch("app.services.media_mirror_engine.time.sleep") as sleep:
        eng.execute_mirror([SourceObject("user_1/f/a.mp4", 1)], dest, throttle_ms=0)
    sleep.assert_not_called()


# =============================================================================
# Folder destination path safety
# =============================================================================
def test_folder_destination_rejects_traversal_keys(tmp_path):
    dest = eng.FolderDestination(tmp_path)
    with pytest.raises(ValueError, match="unsafe object key"):
        dest.lookup("../../etc/passwd")
    with pytest.raises(ValueError, match="unsafe object key"):
        dest.lookup("/etc/passwd")


def test_traversal_key_is_contained_as_object_failure(tmp_path):
    # A hostile/corrupt key must be counted as a failed object, not crash the run.
    dest = eng.FolderDestination(tmp_path)
    result = eng.execute_mirror([SourceObject("../escape.bin", 1)], dest)
    assert result["objects_failed"] == 1
    assert result["objects_copied"] == 0
    assert not (tmp_path.parent / "escape.bin").exists()


def test_folder_destination_lookup(tmp_path):
    (tmp_path / "user_1" / "file_1").mkdir(parents=True)
    (tmp_path / "user_1" / "file_1" / "a.mp4").write_bytes(b"12345")
    dest = eng.FolderDestination(tmp_path)
    assert dest.lookup("user_1/file_1/a.mp4") == (5, None)
    assert dest.lookup("user_1/file_1/missing.mp4") is None
