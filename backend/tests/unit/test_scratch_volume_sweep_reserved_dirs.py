"""Issue #661 phase 1.2 — sweep_expired must not rmtree the reserved ``engine``/``diar``
namespaces just because their OWN mtime bumps on every file create.

Before the fix, ``sweep_expired`` treats every top-level directory the same way: if its own
mtime is older than the TTL, the whole directory is removed. Once ``engine/`` and ``diar/``
become permanent top-level namespaces (phase 2), a directory whose mtime happens to be stale
(no file created in it recently) would have its WHOLE namespace deleted, taking any in-flight
WAV with it. Reserved namespaces must instead be swept file-by-file, by each file's own mtime,
and the directory itself must never be removed.
"""

from __future__ import annotations

import os
import time

from app.utils import scratch_volume


def test_sweep_spares_reserved_namespace_dirs_but_sweeps_stale_files_inside(tmp_path, monkeypatch):
    monkeypatch.setattr(scratch_volume, "SCRATCH_DIR", tmp_path)
    monkeypatch.setattr(scratch_volume, "_scratch_is_shared", lambda: True)

    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    old_file = engine_dir / "old.wav"
    new_file = engine_dir / "new.wav"
    old_file.write_bytes(b"x")
    new_file.write_bytes(b"x")

    old_time = time.time() - 2 * scratch_volume.DEFAULT_TTL_SECONDS
    os.utime(old_file, (old_time, old_time))
    # Bump the reserved dir's own mtime well past the TTL too, simulating "no file created
    # here recently" — this is exactly the case that would previously delete the whole dir.
    os.utime(engine_dir, (old_time, old_time))

    removed, errors = scratch_volume.sweep_expired()

    assert errors == 0
    assert engine_dir.is_dir(), "reserved namespace directory must never be rmtree'd"
    assert not old_file.exists(), "stale file inside a reserved namespace must be swept"
    assert new_file.exists(), "fresh file inside a reserved namespace must survive"
    assert removed >= 1


def test_sweep_still_removes_a_stale_unreserved_uuid_dir(tmp_path, monkeypatch):
    """Guard-the-guard: ordinary per-file scratch dirs keep their existing (whole-dir) behaviour."""
    monkeypatch.setattr(scratch_volume, "SCRATCH_DIR", tmp_path)
    monkeypatch.setattr(scratch_volume, "_scratch_is_shared", lambda: True)

    uuid_dir = tmp_path / "11111111-1111-1111-1111-111111111111"
    uuid_dir.mkdir()
    (uuid_dir / "audio.wav").write_bytes(b"x")
    old_time = time.time() - 2 * scratch_volume.DEFAULT_TTL_SECONDS
    os.utime(uuid_dir, (old_time, old_time))

    removed, errors = scratch_volume.sweep_expired()

    assert errors == 0
    assert removed == 1
    assert not uuid_dir.exists()
