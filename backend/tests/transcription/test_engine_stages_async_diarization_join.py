"""Issue #661 phase 1.1 — an early return in the GPU stages must not outlive the
overlapped diarization thread.

``_GpuRawStage.run`` (and ``_GpuStage.run``) start an ``_AsyncDiarization`` daemon thread
against the shared-volume WAV before transcription runs, then join it later via
``_collect_diarization``. But when the transcriber returns no segments, both stages
returned EARLY — before ever calling ``result()`` — leaving the diarization thread running
against a WAV a caller could unlink out from under it. This test drives that exact shape and
asserts ``run()`` does not return until the diarization thread has finished.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import MagicMock
from unittest.mock import patch

from app.transcription.engine.job import PreprocessResult

if TYPE_CHECKING:
    from app.transcription.engine.config import EngineConfig


def _make_preprocess_result() -> PreprocessResult:
    return PreprocessResult(
        task_id="task-abc",
        file_id=1,
        user_id=1,
        local_wav_path="/scratch/opentranscribe/engine/task-abc.wav",
        minio_temp_object="",
        audio_duration_s=1.0,
        audio_sample_rate=16000,
        audio_channels=1,
        audio_size_bytes=32000,
        vad_regions=None,
        config_snapshot={},
        stage1_timings={},
    )


class _StubEngineConfig:
    class _TC:
        enable_diarization = True
        concurrent_requests = 1
        device = "cpu"
        diarizer_backend = "native"

    transcription_config = _TC()
    boundary_acoustic_recheck_enabled = False


def test_gpu_raw_stage_joins_async_diarization_before_returning_on_empty_transcript():
    """RED before the fix: run() returned immediately while the diarize thread was blocked."""
    from app.transcription.engine import stages as stages_mod

    release_event = threading.Event()
    diarize_thread_finished = threading.Event()

    class _BlockingDiarizer:
        last_provider = "native"
        last_model = "community-1"

        def diarize(self, audio, wav_path=None, allow_local_fallback=True):
            release_event.wait(timeout=5)
            diarize_thread_finished.set()
            return (MagicMock(), {}, {})

    manager = MagicMock()
    manager.get_transcriber.return_value = MagicMock(
        transcribe=MagicMock(return_value={"segments": [], "language": "en"})
    )
    manager.get_diarizer.return_value = _BlockingDiarizer()

    with (
        patch.object(stages_mod, "_overlap_diarization_enabled", return_value=True),
        patch("app.transcription.model_manager.ModelManager.get_instance", return_value=manager),
        patch(
            "app.transcription.engine.audio_loader.load_from_shared_volume",
            return_value=MagicMock(__len__=lambda self: 16000),
        ),
        patch("app.utils.hardware_detection.detect_hardware", return_value=MagicMock()),
    ):
        stage = stages_mod._GpuRawStage()
        pre = _make_preprocess_result()
        # A deliberate minimal stub: a real EngineConfig resolves against SystemSettings
        # and the environment, none of which this test is about. `cast` records that the
        # narrowing is intentional — it does not widen anything in production code, and
        # every attribute the stage actually reads is present on the stub above.
        config = cast("EngineConfig", _StubEngineConfig())

        result_holder: list = []

        def _run():
            result_holder.append(stage.run(pre, config))

        runner = threading.Thread(target=_run, daemon=True)
        runner.start()

        # Give run() a moment to reach the early return and (pre-fix) return without joining.
        runner.join(timeout=1.0)
        assert runner.is_alive(), (
            "run() returned before the diarization thread finished — the fix must make "
            "run() block on close()/join() even on the no-segments early return"
        )
        assert not diarize_thread_finished.is_set()

        release_event.set()
        runner.join(timeout=5.0)
        assert not runner.is_alive()
        assert diarize_thread_finished.is_set()
        assert len(result_holder) == 1
