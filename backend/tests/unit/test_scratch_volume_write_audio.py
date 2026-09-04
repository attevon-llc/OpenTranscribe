"""Pin the write_audio() source-survives invariant (issue #661 E1).

``scratch_volume.write_audio`` used to try ``os.replace(src, dest)`` first — a MOVE that
deletes the source's directory entry as its mechanism. At the time E1 was fixed it only ever
"worked" because the two paths passed in production (a container-local ``/tmp`` temp dir and
the ``pipeline_scratch`` named volume) sat on DIFFERENT filesystems, so the rename always hit
``EXDEV`` and fell through to a copy. Putting them on one filesystem — or running the unit
test suite on a single-filesystem CI runner — made the rename succeed and would have silently
emptied ``local_wav_path`` for every caller that reads ``src_path`` again afterward
(``minio_service.upload_temp_audio``'s caller, ``tasks/transcription/preprocess.py``).

⚠️ **Issue #661 E2 made the same-filesystem case PRODUCTION, on purpose.** The engine handoff
(``preprocess.stage_engine_shared_volume_wav``) now deliberately links the ``engine/``
namespace's WAV from the very same ``pipeline_scratch`` volume ``write_audio`` staged the
``<file_uuid>/`` namespace's WAV onto — same filesystem, ``os.link`` always succeeds, zero
bytes copied. So the below is no longer a hypothetical worst case being forced for test
coverage; it is the ordinary path every job now takes. The fixture still forces it directly
(both source and destination under one ``tmp_path``) so the test does not depend on the host's
mount topology, and it still asserts the source survives a successful write — the same
property the old ``os.replace``-first shape violated, now checked in what is the common case
rather than an edge case.
"""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture
def scratch_module(tmp_path, monkeypatch):
    """Reload ``scratch_volume`` pointed at an isolated, always-available scratch dir."""
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    monkeypatch.setenv("PIPELINE_SCRATCH_DIR", str(scratch_dir))
    monkeypatch.delenv("PIPELINE_SCRATCH_SHARED", raising=False)

    from app.utils import scratch_volume as module

    importlib.reload(module)
    yield module
    # Leave the module in its default (env-driven at import time) state for other tests.
    monkeypatch.delenv("PIPELINE_SCRATCH_DIR", raising=False)
    importlib.reload(module)


@pytest.mark.unit
def test_write_audio_leaves_the_source_file_intact(scratch_module, tmp_path):
    """The regression test: src_path must exist after a successful write_audio()."""
    src = tmp_path / "audio.wav"
    src.write_bytes(b"RIFF-fake-wav-bytes")

    dest = scratch_module.write_audio("file-123", str(src))

    assert dest is not None, "write_audio should have succeeded against an available scratch dir"
    assert src.exists(), (
        "write_audio() removed the source file — callers that read src_path again "
        "afterward (preprocess.py's shared-volume WAV copy) would silently see it vanish"
    )
    assert dest.exists()
    assert dest.read_bytes() == b"RIFF-fake-wav-bytes"


@pytest.mark.unit
def test_write_audio_source_survives_even_on_the_same_filesystem(scratch_module, tmp_path):
    """Same invariant, forced onto ONE filesystem regardless of host mount topology.

    Both the source and the scratch dir live under the same ``tmp_path`` here, so an
    ``os.replace``-based implementation would succeed (no EXDEV) and delete the source —
    this is the exact scenario issue #661 E1 describes as "putting them on one filesystem".
    """
    same_fs_scratch = tmp_path / "same_fs_scratch"
    same_fs_scratch.mkdir()
    scratch_module.SCRATCH_DIR = same_fs_scratch  # same tmp_path filesystem as src below

    src = tmp_path / "same_fs_audio.wav"
    src.write_bytes(b"same-filesystem-payload")
    assert os.stat(str(src)).st_dev == os.stat(str(same_fs_scratch)).st_dev, (
        "test setup invariant: src and scratch dir must share a device for this to be a "
        "real same-filesystem check"
    )

    dest = scratch_module.write_audio("file-456", str(src))

    assert dest is not None
    assert src.exists(), (
        "on a single filesystem, os.replace(src, dest) would succeed and delete the "
        "source — write_audio must not rely on cross-filesystem EXDEV to protect it"
    )
    assert dest.read_bytes() == b"same-filesystem-payload"
