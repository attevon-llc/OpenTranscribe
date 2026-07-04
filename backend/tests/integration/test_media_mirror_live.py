"""Live-stack integration test for the media mirror (issue #242).

Runs a **bounded** incremental mirror against the dev stack's real MinIO media
bucket into a pytest tmp folder, asserting the copied/skipped/never-deleted
semantics end-to-end.

Safety posture (the source bucket holds irreplaceable live media):
- **Read-only toward the source** — the engine only lists and GETs; nothing here
  writes to, deletes from, or modifies the source bucket.
- **Bounded** — ``max_objects`` caps the run at a handful of objects and the source
  stream is additionally filtered to small objects (≤ 5 MB) so a run never drags
  multi-GB originals onto the test host.
- **Destination is a pytest tmp_path** — auto-cleaned; nothing else is touched.

Gated the same way as the other MinIO-backed tests: conftest TCP-probes
localhost:5178 and sets SKIP_S3 accordingly.

Run: cd backend && PYTHONPATH=. pytest -m integration tests/integration/test_media_mirror_live.py -v
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SKIP_S3", "True").lower() == "true",
        reason="MinIO (localhost:5178) not reachable — live mirror test needs the dev stack",
    ),
]

MAX_OBJECTS = 5
MAX_OBJECT_BYTES = 5 * 1024 * 1024  # only mirror small objects in the test


def _small_source(limit: int = MAX_OBJECT_BYTES) -> Iterator:
    """Source stream filtered to small objects (bounding bytes, not just count)."""
    from app.core.config import settings
    from app.services import media_mirror_engine as eng

    for obj in eng.iter_source_objects(settings.MEDIA_BUCKET_NAME):
        if obj.size <= limit:
            yield obj


class _RecordingDestination:
    """FolderDestination wrapper that records which keys were copied."""

    def __init__(self, inner):
        self.inner = inner
        self.copied_keys: list[str] = []

    def lookup(self, key):
        return self.inner.lookup(key)

    def copy(self, obj):
        self.inner.copy(obj)
        self.copied_keys.append(obj.key)


def test_bounded_live_mirror_roundtrip(tmp_path):
    from app.core.config import settings
    from app.services import media_mirror_engine as eng
    from app.services.minio_service import minio_client

    dest_root = tmp_path / "mirror-dest"
    dest_root.mkdir()

    # A pre-existing destination file the mirror must NEVER touch or delete.
    sentinel = dest_root / "user_999" / "file_999" / "not-in-source.bin"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"pre-existing destination data")

    dest = _RecordingDestination(eng.FolderDestination(dest_root))

    # --- First bounded pass: copies up to MAX_OBJECTS small source objects. ---
    result = eng.execute_mirror(_small_source(), dest, throttle_ms=0, max_objects=MAX_OBJECTS)
    assert result["objects_scanned"] <= MAX_OBJECTS
    assert result["objects_failed"] == 0
    assert (
        result["objects_copied"] + result["objects_skipped"] + result["objects_excluded"]
        == (result["objects_scanned"])
    )

    if result["objects_scanned"] == 0:
        pytest.skip("media bucket has no small objects to mirror — nothing to verify")

    # Every copied object landed with the exact source size (per stat_object).
    for key in dest.copied_keys:
        local = dest_root / key
        assert local.is_file(), f"copied object missing at destination: {key}"
        stat = minio_client.stat_object(settings.MEDIA_BUCKET_NAME, key)
        assert local.stat().st_size == stat.size

    # --- Second pass over the same window: fully incremental (nothing re-copied). ---
    dest2 = _RecordingDestination(eng.FolderDestination(dest_root))
    result2 = eng.execute_mirror(_small_source(), dest2, throttle_ms=0, max_objects=MAX_OBJECTS)
    assert result2["objects_failed"] == 0
    assert result2["objects_copied"] == 0, "second pass must skip already-mirrored objects"
    assert result2["objects_skipped"] == result["objects_copied"] + result["objects_skipped"]
    assert dest2.copied_keys == []

    # --- Never-delete: the sentinel and every first-pass copy are still intact. ---
    assert sentinel.read_bytes() == b"pre-existing destination data"
    for key in dest.copied_keys:
        assert (dest_root / key).is_file()

    # No excluded (regenerable) prefix ever landed in the destination.
    from app.services.media_mirror_engine import EXCLUDED_PREFIXES

    for path in dest_root.rglob("*"):
        rel = path.relative_to(dest_root).as_posix()
        assert not rel.startswith(EXCLUDED_PREFIXES)
