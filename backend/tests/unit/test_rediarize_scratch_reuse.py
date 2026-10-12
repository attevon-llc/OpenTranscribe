"""Issue #661 E2 phase 2.6 — rediarize_task must reuse the scratch-volume WAV directly
(no copy, no cleanup of the shared namespace) when it's available, and pass wav_path through
to the diarizer only in that case.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from app.tasks import rediarize_task
from app.utils import scratch_volume


def test_prepare_audio_returns_scratch_path_with_no_cleanup_dir(tmp_path, monkeypatch):
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    monkeypatch.setattr(scratch_volume, "SCRATCH_DIR", scratch_root)
    monkeypatch.setattr(scratch_volume, "_scratch_is_shared", lambda: True)

    file_uuid = "44444444-4444-4444-4444-444444444444"
    src = tmp_path / "src.wav"
    src.write_bytes(b"RIFF....WAVEfmt ")
    scratch_dest = scratch_volume.write_audio(file_uuid, str(src))
    assert scratch_dest is not None

    audio_path, temp_dir = rediarize_task._prepare_audio(
        storage_path="s3://irrelevant",
        content_type="audio/wav",
        filename="irrelevant.wav",
        file_uuid=file_uuid,
    )

    assert audio_path == str(scratch_dest)
    assert temp_dir is None, "must not return a dir the caller would rmtree the scratch ns with"


def _empty_frame():
    import numpy as np

    from app.transcription.diarize_result import DiarizeResult

    return DiarizeResult(
        start=np.array([], dtype=np.float64),
        end=np.array([], dtype=np.float64),
        speaker=np.array([], dtype=object),
    )


def test_local_engine_passes_wav_path_to_native_and_omits_for_pyannote(tmp_path):
    """The post stage's local engine (``LocalDiarizationProvider``) hands the scratch WAV to
    the native sidecar only, never to the in-process PyAnnote fallback."""
    from app.services.diarization.local_provider import LocalDiarizationProvider
    from app.services.diarization.types import DiarizeConfig
    from app.transcription.diarizer_native import NativeSpeakerDiarizer

    audio_path = str(tmp_path / "a.wav")
    with open(audio_path, "wb") as f:
        f.write(b"RIFF....WAVEfmt ")

    fake_audio = MagicMock()
    native_diarizer = MagicMock(spec=NativeSpeakerDiarizer)
    native_diarizer.diarize.return_value = (_empty_frame(), {}, {})
    native_diarizer.last_provider = "native"
    native_diarizer.last_model = "native-model"
    config = DiarizeConfig(min_speakers=1, max_speakers=20, sidecar_wav_path=audio_path)

    with (
        patch("app.transcription.audio.load_audio", return_value=fake_audio),
        patch(
            "app.transcription.model_manager.ModelManager.get_instance",
            return_value=MagicMock(get_diarizer=MagicMock(return_value=native_diarizer)),
        ),
    ):
        result = LocalDiarizationProvider().diarize(audio_path, config)

    native_diarizer.diarize.assert_called_once()
    _, kwargs = native_diarizer.diarize.call_args
    assert kwargs.get("wav_path") == audio_path
    # The provider reports the engine that actually ran, not a generic "local".
    assert result.provider_name == "native"

    non_native_diarizer = MagicMock()  # not spec'd as NativeSpeakerDiarizer
    non_native_diarizer.diarize.return_value = (_empty_frame(), {}, {})
    non_native_diarizer.last_provider = "pyannote"
    non_native_diarizer.last_model = "pyannote/speaker-diarization-community-1"
    with (
        patch("app.transcription.audio.load_audio", return_value=fake_audio),
        patch(
            "app.transcription.model_manager.ModelManager.get_instance",
            return_value=MagicMock(get_diarizer=MagicMock(return_value=non_native_diarizer)),
        ),
    ):
        result = LocalDiarizationProvider().diarize(audio_path, config)

    non_native_diarizer.diarize.assert_called_once_with(fake_audio)
    assert result.provider_name == "pyannote"
