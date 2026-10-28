"""`SpeakerEmbeddingService._load_audio` must fall through its whole loader chain.

The chain is ffmpeg -> torchaudio -> scipy. On torchaudio 2.11 `torchaudio.load` goes
through torchcodec, which in our images raises a `RuntimeError("Could not load
libtorchcodec ...")` (missing libnppicc). That message does not contain "backend", and the
old fallback re-raised anything that did not, so a failed ffmpeg decode never reached the
scipy WAV reader. These tests drive the real function with ffmpeg and torchaudio both
failing and assert the scipy path still returns the waveform.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest
import torch
from scipy.io import wavfile

from app.services.speaker_embedding_service import SpeakerEmbeddingService

SR = 16000


@pytest.fixture
def wav_path(tmp_path: Path) -> Path:
    """A real 16-bit mono WAV so the scipy reader has something to decode."""
    samples = (np.sin(np.linspace(0, 200, SR // 2)) * 16000).astype(np.int16)
    path = tmp_path / "tone.wav"
    wavfile.write(str(path), SR, samples)
    return path


def _ffmpeg_fails(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)


def _torchaudio_load_raises(monkeypatch, exc: BaseException) -> list[str]:
    calls: list[str] = []

    def load(path, *args, **kwargs):
        calls.append(path)
        raise exc

    fake = types.ModuleType("torchaudio")
    fake.load = load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torchaudio", fake)
    return calls


class TestLoaderChainFallsThrough:
    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("Could not load libtorchcodec. libnppicc.so.13: cannot open"),
            OSError("decoder unavailable"),
            ValueError("unsupported format"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_torchaudio_failure_reaches_scipy(self, monkeypatch, wav_path, exc, caplog):
        _ffmpeg_fails(monkeypatch)
        calls = _torchaudio_load_raises(monkeypatch, exc)

        with caplog.at_level("WARNING", logger="app.services.speaker_embedding_service"):
            waveform, sr = SpeakerEmbeddingService._load_audio(str(wav_path))

        assert calls == [str(wav_path)], "torchaudio must have been attempted before scipy"
        assert sr == SR
        assert waveform.shape == (1, SR // 2)
        assert waveform.dtype == torch.float32
        expected = wavfile.read(str(wav_path))[1].astype(np.float32) / np.float32(32767)
        np.testing.assert_allclose(waveform[0].numpy(), expected)

        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("torchaudio" in m for m in warnings), warnings
        assert not any(str(wav_path) in m for m in warnings), "the path must not be logged"

    def test_torchaudio_not_installed_reaches_scipy(self, monkeypatch, wav_path):
        _ffmpeg_fails(monkeypatch)
        monkeypatch.setitem(sys.modules, "torchaudio", None)  # import raises ImportError

        waveform, sr = SpeakerEmbeddingService._load_audio(str(wav_path))

        assert sr == SR
        assert waveform.shape == (1, SR // 2)

    def test_unexpected_torchaudio_error_still_propagates(self, monkeypatch, wav_path):
        """Only the known loader-failure types fall through; a bug is not swallowed."""
        _ffmpeg_fails(monkeypatch)
        _torchaudio_load_raises(monkeypatch, KeyError("bug"))

        with pytest.raises(KeyError):
            SpeakerEmbeddingService._load_audio(str(wav_path))


class TestFfmpegSuccessPathUnchanged:
    def test_ffmpeg_result_is_returned_without_touching_torchaudio(self, monkeypatch, wav_path):
        audio = np.linspace(-0.5, 0.5, 800, dtype=np.float32)

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=audio.tobytes())

        monkeypatch.setattr(subprocess, "run", fake_run)
        calls = _torchaudio_load_raises(monkeypatch, RuntimeError("must not be called"))

        waveform, sr = SpeakerEmbeddingService._load_audio(str(wav_path))

        assert calls == []
        assert sr == SR
        np.testing.assert_array_equal(waveform[0].numpy(), audio)
