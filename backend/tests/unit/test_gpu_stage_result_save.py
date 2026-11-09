"""``core._validate_and_save_stage_result``: the tail both GPU-task paths share."""

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

from app.tasks.transcription import core
from app.tasks.transcription.context import TranscriptionContext

_CTX = TranscriptionContext(
    task_id="t-1",
    file_id=9,
    file_uuid="u-1",
    user_id=1,
    file_path="p/a.mp4",
    file_name="a.mp4",
    content_type="video/mp4",
)


def _run(result, *, invalid=False, shared=""):
    calls = MagicMock()
    with (
        patch.object(core, "_validate_transcription_result", return_value=invalid),
        patch.object(core, "_process_and_save_critical", return_value={"saved": 1}) as save,
        patch.object(core.benchmark_timing, "mark", calls.mark),
        patch.object(core.benchmark_timing, "set_context", calls.set_context),
        patch("app.transcription.engine.audio_loader.cleanup_shared_volume_wav", calls.cleanup),
    ):
        out = core._validate_and_save_stage_result(
            _CTX, result, {"p": 1}, True, shared_wav_path=shared
        )
    return out, save, calls


def test_invalid_result_returns_the_error_payload_without_saving_or_cleaning_up():
    out, save, calls = _run({"asr_provider": "x"}, invalid=True, shared="/shared/a.wav")

    assert out == {"status": "error", "file_uuid": "u-1", "file_id": 9, "task_id": "t-1"}
    save.assert_not_called()
    calls.cleanup.assert_not_called()
    calls.mark.assert_not_called()


def test_valid_result_is_saved_flagged_timed_and_the_shared_wav_dropped():
    result: dict[str, Any] = {"asr_provider": "gladia", "asr_model": "m"}
    out, save, calls = _run(result, shared="/shared/a.wav")

    assert out == {"saved": 1}
    assert result["diarization_disabled"] is True
    save.assert_called_once_with(_CTX, result, {"p": 1})
    calls.cleanup.assert_called_once_with("/shared/a.wav")
    calls.mark.assert_called_once_with("t-1", "gpu_end")
    calls.set_context.assert_called_once_with("t-1", {"asr_provider": "gladia", "asr_model": "m"})


def test_without_a_shared_wav_nothing_is_cleaned_and_provider_defaults_to_local():
    out, _save, calls = _run({})

    assert out == {"saved": 1}
    calls.cleanup.assert_not_called()
    calls.set_context.assert_called_once_with("t-1", {"asr_provider": "local", "asr_model": None})


def test_non_dict_result_skips_the_flag_and_the_timing_context():
    out, _save, calls = _run(object())

    assert out == {"saved": 1}
    calls.set_context.assert_not_called()
    calls.mark.assert_called_once_with("t-1", "gpu_end")
