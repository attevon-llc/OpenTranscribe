"""Issue #661 E2 phase 2.3 — the engine handoff must be an os.link, not a cross-filesystem copy,
whenever the scratch-volume WAV is available; and it must still fall back to a copy of the
container-local temp WAV when scratch is unavailable.
"""

from __future__ import annotations

import os

from app.tasks.transcription.preprocess import stage_engine_shared_volume_wav
from app.utils import scratch_volume


def test_link_produces_one_inode_when_scratch_available(tmp_path, monkeypatch):
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    engine_dir = tmp_path / "engine"
    monkeypatch.setattr(scratch_volume, "SCRATCH_DIR", scratch_root)
    monkeypatch.setattr(scratch_volume, "_scratch_is_shared", lambda: True)
    monkeypatch.setenv("ENGINE_SHARED_VOLUME_PATH", str(engine_dir))

    file_uuid = "22222222-2222-2222-2222-222222222222"
    temp_audio_path = str(tmp_path / "container_local.wav")
    with open(temp_audio_path, "wb") as f:
        f.write(b"RIFF....WAVEfmt ")

    scratch_dest = scratch_volume.write_audio(file_uuid, temp_audio_path)
    assert scratch_dest is not None

    result = stage_engine_shared_volume_wav(file_uuid, "task-123", temp_audio_path)

    assert result and os.path.exists(result)
    assert os.stat(result).st_ino == os.stat(scratch_dest).st_ino, (
        "engine WAV must be a hard link to the scratch WAV (same inode), not a copy"
    )


def test_falls_back_to_copy_when_scratch_unavailable(tmp_path, monkeypatch):
    engine_dir = tmp_path / "engine"
    monkeypatch.setattr(scratch_volume, "_scratch_is_shared", lambda: False)
    monkeypatch.setenv("ENGINE_SHARED_VOLUME_PATH", str(engine_dir))

    file_uuid = "33333333-3333-3333-3333-333333333333"
    temp_audio_path = str(tmp_path / "container_local2.wav")
    with open(temp_audio_path, "wb") as f:
        f.write(b"RIFF....WAVEfmt ")

    result = stage_engine_shared_volume_wav(file_uuid, "task-456", temp_audio_path)

    assert result and os.path.exists(result)
    assert os.stat(result).st_ino != os.stat(temp_audio_path).st_ino, (
        "with scratch unavailable this must be a real copy, not a link"
    )
    with open(result, "rb") as f:
        assert f.read() == b"RIFF....WAVEfmt "
