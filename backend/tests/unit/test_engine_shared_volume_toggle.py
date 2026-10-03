"""Turning the engine shared-volume WAV handoff off for multi-node deployments (#1151).

``stage_engine_shared_volume_wav`` copies the preprocessed WAV into
``ENGINE_SHARED_VOLUME_PATH`` so the GPU task can read it without a MinIO download. That only
works when the preprocess worker and the GPU worker share that directory. On separate nodes
they never can: the write either fails (read-only root filesystem, a WARNING per file) or
succeeds into a directory nothing on that node ever cleans up, while the GPU task logs that
the recorded path was not found.

``ENGINE_SHARED_VOLUME_ENABLED=false`` turns the handoff off; when it is unset it follows
``PIPELINE_SCRATCH_SHARED``, which already declares that workers do not share a filesystem.
Off means: no write, ``""`` returned (the GPU task downloads from MinIO), one INFO line per
process instead of a WARNING per file, and nothing above DEBUG on the GPU side.
"""

from __future__ import annotations

import logging

import pytest

from app.core import constants as engine_constants
from app.core.constants import engine_shared_volume_enabled
from app.tasks.transcription import core as gpu_core
from app.tasks.transcription import preprocess
from app.tasks.transcription.preprocess import stage_engine_shared_volume_wav


@pytest.fixture
def engine_dir(tmp_path, monkeypatch):
    target = tmp_path / "engine"
    monkeypatch.setenv("ENGINE_SHARED_VOLUME_PATH", str(target))
    monkeypatch.setattr(
        engine_constants, "ENGINE_SHARED_VOLUME_DEFAULT", str(tmp_path / "no-such-default")
    )
    monkeypatch.delenv("ENGINE_SHARED_VOLUME_ENABLED", raising=False)
    monkeypatch.delenv("PIPELINE_SCRATCH_SHARED", raising=False)
    monkeypatch.setattr(preprocess, "_handoff_disabled_logged", False)
    return target


@pytest.fixture
def temp_wav(tmp_path):
    path = tmp_path / "local.wav"
    path.write_bytes(b"RIFF....WAVEfmt ")
    return str(path)


class TestSwitch:
    @pytest.mark.parametrize(
        ("engine_flag", "scratch_shared", "expected"),
        [
            (None, None, True),  # default: unchanged behaviour
            ("false", None, False),
            ("FALSE", None, False),
            (None, "false", False),  # follows the scratch kill switch
            ("true", "false", True),  # explicit value wins
            ("false", "true", False),
            (None, "true", True),
        ],
    )
    def test_resolution(self, monkeypatch, engine_flag, scratch_shared, expected):
        for name, value in (
            ("ENGINE_SHARED_VOLUME_ENABLED", engine_flag),
            ("PIPELINE_SCRATCH_SHARED", scratch_shared),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        assert engine_shared_volume_enabled() is expected


class TestStagingWhenDisabled:
    @pytest.mark.parametrize(
        "env", [{"ENGINE_SHARED_VOLUME_ENABLED": "false"}, {"PIPELINE_SCRATCH_SHARED": "false"}]
    )
    def test_skips_the_write_and_returns_empty(self, engine_dir, temp_wav, monkeypatch, env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)

        assert stage_engine_shared_volume_wav("f-1", "task-1", temp_wav) == ""
        assert not engine_dir.exists()  # not even the directory is created

    def test_logs_once_at_info_and_never_warns(self, engine_dir, temp_wav, monkeypatch, caplog):
        monkeypatch.setenv("ENGINE_SHARED_VOLUME_ENABLED", "false")
        caplog.set_level(logging.DEBUG, logger=preprocess.logger.name)

        for i in range(3):
            stage_engine_shared_volume_wav(f"f-{i}", f"task-{i}", temp_wav)

        records = [r for r in caplog.records if r.name == preprocess.logger.name]
        assert [r.levelno for r in records if r.levelno >= logging.INFO] == [logging.INFO]
        assert "ENGINE_SHARED_VOLUME_ENABLED" in records[0].getMessage()


class TestStagingWhenEnabled:
    def test_default_still_stages(self, engine_dir, temp_wav):
        result = stage_engine_shared_volume_wav("f-1", "task-1", temp_wav)

        assert result == str(engine_dir / "task-1.wav")
        assert (engine_dir / "task-1.wav").read_bytes() == b"RIFF....WAVEfmt "

    def test_explicit_enable_overrides_scratch_switch(self, engine_dir, temp_wav, monkeypatch):
        monkeypatch.setenv("PIPELINE_SCRATCH_SHARED", "false")
        monkeypatch.setenv("ENGINE_SHARED_VOLUME_ENABLED", "true")

        assert stage_engine_shared_volume_wav("f-1", "task-1", temp_wav) == str(
            engine_dir / "task-1.wav"
        )


class TestGpuSideFallbackLog:
    def test_quiet_when_disabled(self, monkeypatch, caplog):
        monkeypatch.setenv("ENGINE_SHARED_VOLUME_ENABLED", "false")
        caplog.set_level(logging.DEBUG, logger=gpu_core.logger.name)

        gpu_core._log_shared_wav_fallback_reason("", 7)
        gpu_core._log_shared_wav_fallback_reason("/nowhere/task.wav", 7)

        loud = [r for r in caplog.records if r.levelno >= logging.INFO]
        assert loud == []

    def test_not_found_still_warns_when_enabled(self, monkeypatch, caplog):
        monkeypatch.delenv("ENGINE_SHARED_VOLUME_ENABLED", raising=False)
        monkeypatch.delenv("PIPELINE_SCRATCH_SHARED", raising=False)
        caplog.set_level(logging.DEBUG, logger=gpu_core.logger.name)

        gpu_core._log_shared_wav_fallback_reason("/nowhere/task.wav", 7)

        assert [r.levelno for r in caplog.records] == [logging.WARNING]
