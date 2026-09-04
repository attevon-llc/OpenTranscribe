"""Issue #661 E2 phase 2.3 — the engine handoff must be an os.link, not a cross-filesystem copy,
whenever the scratch-volume WAV is available; and it must still fall back to a copy of the
container-local temp WAV when scratch is unavailable.

⚠️ Adversarial-audit finding: these tests used to pass on the host only by accident.
``resolve_engine_shared_volume_path`` (app/core/constants.py) falls back to the coded default
``ENGINE_SHARED_VOLUME_DEFAULT = "/scratch/opentranscribe/engine"`` whenever that directory
exists — and it exists INSIDE the container (the image pre-creates it) but not on a bare host.
So a host pytest run got lucky: the default didn't exist, the resolver honoured the test's
``ENGINE_SHARED_VOLUME_PATH`` override, and the WAV landed in ``tmp_path`` as intended. Inside
the container, the resolver instead returns the real default and the WAV is written into the
LIVE shared ``pipeline_scratch`` volume — reproduced by the auditor as
``assert 51118083 == 22850921`` (this test's assertions comparing against a stale/wrong file
because the destination silently moved). Both tests below now also monkeypatch
``ENGINE_SHARED_VOLUME_DEFAULT`` to a tmp-controlled path that is guaranteed not to exist, so
the resolver's behaviour — and the destination this test writes to — no longer depends on
whether the host happens to have that directory. This must never write into the live shared
volume.
"""

from __future__ import annotations

import os

from app.core import constants as engine_constants
from app.tasks.transcription.preprocess import stage_engine_shared_volume_wav
from app.utils import scratch_volume


def test_link_produces_one_inode_when_scratch_available(tmp_path, monkeypatch):
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    engine_dir = tmp_path / "engine"
    monkeypatch.setattr(scratch_volume, "SCRATCH_DIR", scratch_root)
    monkeypatch.setattr(scratch_volume, "_scratch_is_shared", lambda: True)
    monkeypatch.setenv("ENGINE_SHARED_VOLUME_PATH", str(engine_dir))
    # Never let the resolver fall back to the REAL coded default (which exists inside the
    # container and points at the live shared volume) — point it at a tmp path guaranteed
    # absent, so this test's destination is deterministic regardless of the host layout.
    monkeypatch.setattr(
        engine_constants, "ENGINE_SHARED_VOLUME_DEFAULT", str(tmp_path / "no-such-default")
    )

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
    monkeypatch.setattr(
        engine_constants, "ENGINE_SHARED_VOLUME_DEFAULT", str(tmp_path / "no-such-default")
    )

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
