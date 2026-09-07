"""Issue #656 Step 5: DiarSidecarUnavailableError must trigger a Celery retry, on BOTH
transcribe_gpu_task and diarize_gpu_task, and must never mark the file ERROR while retries
remain — the ordering-before-`except Exception` invariant documented at both call sites.

Mirrors ``test_asr_error_taxonomy.py``'s ``TestTranscribeGpuTaskRetryWiring`` shape: ``self.retry``
is patched to raise a sentinel so celery.Task's own retry machinery is never actually invoked
(no broker needed), and the test asserts on the REAL values passed to it (exc/countdown/
max_retries) — not merely that it was called.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import numpy as np
import pytest

from app.core.constants import DIAR_SIDECAR_MAX_RETRIES
from app.transcription.diarizer_native import DiarSidecarUnavailableError
from app.transcription.diarizer_native import NativeSpeakerDiarizer

#: All diar-native tests that stand up a real HTTP server, or drive diarizer_native's
#: module-level state, run on ONE xdist worker.
#:
#: `_free_port()` binds port 0, reads the number, then CLOSES the socket and returns it — so
#: between that close and the caller's `HTTPServer((host, port))` bind, another worker can be
#: handed the same ephemeral port. Seven modules use that helper and none were grouped, which
#: is why a DIFFERENT diar test failed on each full-suite run while every one of them passed in
#: isolation. Same remedy the repo already uses for tests sharing mutable global state
#: (backend/tests/CLAUDE.md's `--dist loadgroup` note); here the shared state is the machine's
#: ephemeral-port pool plus diarizer_native's readiness caches.
pytestmark = pytest.mark.xdist_group("diar_native_state")


class _RetrySentinelError(Exception):
    def __init__(self, *, exc, countdown, max_retries):
        super().__init__("retry")
        self.exc = exc
        self.countdown = countdown
        self.max_retries = max_retries


class _FakeProvider:
    provider_name = "deepgram"


@pytest.fixture
def gpu_task_preprocess_context():
    return {
        "task_id": "task-656-1",
        "file_uuid": "22222222-2222-2222-2222-222222222222",
        "file_id": 1,
        "user_id": 1,
        "storage_path": "files/test.mp3",
        "file_name": "test.mp3",
        "content_type": "audio/mpeg",
        "diarization_source": "provider",
    }


def _run_transcribe_gpu_task_raising(preprocess_context, error: Exception):
    """Drive transcribe_gpu_task's cloud-ASR branch down to a raised `error` — the same
    harness shape as test_asr_error_taxonomy.py, reused here for a different exception type.
    """
    from app.tasks.transcription import core as core_module

    task = core_module.transcribe_gpu_task

    def _fake_retry(*, exc, countdown, max_retries):
        raise _RetrySentinelError(exc=exc, countdown=countdown, max_retries=max_retries)

    with (
        patch("app.services.minio_service.download_temp_audio", lambda *a, **kw: None),
        patch.object(core_module, "session_scope") as mock_scope,
        patch.object(core_module, "get_refreshed_object", return_value=None),
        patch.object(core_module, "send_progress_notification"),
        patch.object(core_module, "_resolve_asr_provider_or_none", return_value=_FakeProvider()),
        patch.object(core_module, "_run_cloud_asr_pipeline", side_effect=error),
        patch.object(core_module, "update_task_status"),
        patch.object(core_module, "_get_user_friendly_error_message", return_value="failed"),
        patch.object(core_module, "_handle_transcription_failure") as mock_handle_failure,
        patch("tempfile.TemporaryDirectory") as mock_tmpdir,
        patch.object(task, "retry", side_effect=_fake_retry) as mock_retry,
        patch.object(type(task.request), "retries", 0, create=True),
    ):
        mock_scope.return_value.__enter__.return_value = MagicMock()
        mock_tmpdir.return_value.__enter__.return_value = "/tmp/fake"  # noqa: S108
        try:
            task.__wrapped__(preprocess_context)
        except _RetrySentinelError as sentinel:
            return mock_retry, sentinel, mock_handle_failure
        except Exception:
            import traceback

            traceback.print_exc()
            return mock_retry, None, mock_handle_failure
    return mock_retry, None, mock_handle_failure


class TestTranscribeGpuTaskDiarRetryWiring:
    def test_sidecar_unavailable_triggers_self_retry_with_the_diar_ladder(
        self, gpu_task_preprocess_context
    ):
        unavailable = DiarSidecarUnavailableError("wedged", reason="timeout")
        mock_retry, sentinel, mock_handle_failure = _run_transcribe_gpu_task_raising(
            gpu_task_preprocess_context, unavailable
        )
        mock_retry.assert_called_once()
        assert sentinel is not None
        assert sentinel.exc is unavailable
        assert sentinel.max_retries == DIAR_SIDECAR_MAX_RETRIES
        assert sentinel.countdown == 30  # DIAR_SIDECAR_RETRY_BASE * 2**0, no Retry-After
        mock_handle_failure.assert_not_called()

    def test_sidecar_unavailable_honours_retry_after_over_the_backoff_formula(
        self, gpu_task_preprocess_context
    ):
        unavailable = DiarSidecarUnavailableError(
            "backpressured", reason="backpressure", retry_after=77.0
        )
        mock_retry, sentinel, _mock_handle_failure = _run_transcribe_gpu_task_raising(
            gpu_task_preprocess_context, unavailable
        )
        assert sentinel is not None
        assert sentinel.countdown == 77.0


# ---------------------------------------------------------------------------
# diarize_gpu_task (Stage 2b, gpu-split)
# ---------------------------------------------------------------------------


class _FakeTranscript:
    raw_segments = [{"start": 0.0, "end": 1.0, "text": "hi"}]
    # Annotated because an empty literal gives mypy nothing to infer from; the real
    # RawTranscriptResult carries a str-keyed config snapshot.
    config_snapshot: dict[str, object] = {}
    local_wav_path = "/tmp/fake.wav"  # noqa: S108


class _RaisingEngine:
    def __init__(self, config):
        pass

    def run_diarize_only(self, transcript, progress_callback=None):
        raise DiarSidecarUnavailableError("wedged", reason="timeout")


def _run_diarize_gpu_task_raising():
    from app.tasks.transcription import diarize_task as diarize_module

    task = diarize_module.diarize_gpu_task

    def _fake_retry(*, exc, countdown, max_retries):
        raise _RetrySentinelError(exc=exc, countdown=countdown, max_retries=max_retries)

    preprocess_context = {
        "task_id": "task-656-diarize",
        "file_uuid": "33333333-3333-3333-3333-333333333333",
        "file_id": 2,
        "user_id": 1,
        "storage_path": "files/test.mp3",
        "file_name": "test.mp3",
        "content_type": "audio/mpeg",
        "diarization_source": "provider",
    }

    with (
        patch.object(diarize_module, "session_scope") as mock_scope,
        patch.object(diarize_module, "send_progress_notification"),
        patch.object(diarize_module, "_get_user_friendly_error_message", return_value="failed"),
        patch.object(diarize_module, "_handle_transcription_failure") as mock_handle_failure,
        patch("app.transcription.Engine", _RaisingEngine),
        patch("app.transcription.EngineConfig") as mock_engine_config,
        patch(
            "app.transcription.engine.job.RawTranscriptResult.deserialize",
            return_value=_FakeTranscript(),
        ),
        patch.object(task, "retry", side_effect=_fake_retry) as mock_retry,
        patch.object(type(task.request), "retries", 0, create=True),
    ):
        mock_scope.return_value.__enter__.return_value = MagicMock()
        mock_engine_config.from_snapshot.return_value = MagicMock(
            transcription_config=MagicMock(model_name="fake-model")
        )
        try:
            task.__wrapped__({}, preprocess_context)
        except _RetrySentinelError as sentinel:
            return mock_retry, sentinel, mock_handle_failure
        except Exception:
            import traceback

            traceback.print_exc()
            return mock_retry, None, mock_handle_failure
    return mock_retry, None, mock_handle_failure


class TestDiarizeGpuTaskRetryWiring:
    def test_sidecar_unavailable_triggers_self_retry_and_does_not_mark_error(self):
        mock_retry, sentinel, mock_handle_failure = _run_diarize_gpu_task_raising()
        mock_retry.assert_called_once()
        assert sentinel is not None
        assert isinstance(sentinel.exc, DiarSidecarUnavailableError)
        assert sentinel.max_retries == DIAR_SIDECAR_MAX_RETRIES
        mock_handle_failure.assert_not_called()


# ---------------------------------------------------------------------------
# Reachability: on the DEFAULT config, can DiarSidecarUnavailableError even get here?
# ---------------------------------------------------------------------------
#
# Adversarial-audit finding: the two test classes above drive `transcribe_gpu_task` and
# `diarize_gpu_task` correctly, injecting `DiarSidecarUnavailableError` straight into the
# task body and asserting the retry ladder fires. But nothing pins whether a REAL sidecar
# failure can ever reach that point. `_run_diarize` (app/transcription/engine/stages.py) calls
# `diarizer.diarize(..., allow_local_fallback=True)` by default on the synchronous path, and
# `NativeSpeakerDiarizer._refuse_or_fallback_impl` only re-raises `DiarSidecarUnavailableError`
# when BOTH `allow_local_fallback` is False (the overlapped-diarization thread only) AND/OR
# `diarizer_require_sidecar()` is True (`ENGINE_DIARIZER_REQUIRE_SIDECAR`, default "false").
# On the default synchronous path with the default env, a sidecar failure is silently absorbed
# into the in-process PyAnnote fallback and never becomes a `DiarSidecarUnavailableError` at
# all — so the retry ladder above is live only under a non-default flag. A change that always
# swallows the error (e.g. deleting the `diarizer_require_sidecar()` check, or the ladder
# becoming permanently dead some other way) would leave every test above still green, since
# they never call the real `diarize()` / `_refuse_or_fallback_impl` path.
class TestSidecarFailureReachesTheLadderOnlyUnderRequireSidecar:
    def _diarizer(self, monkeypatch) -> tuple[NativeSpeakerDiarizer, MagicMock]:
        diarizer = NativeSpeakerDiarizer(config=object())
        fake_fallback = MagicMock()
        fake_fallback.diarize.return_value = ("fallback-result", {}, None)
        monkeypatch.setattr(diarizer, "_fallback_engine", lambda: fake_fallback)
        return diarizer, fake_fallback

    def test_default_config_absorbs_the_failure_into_the_pyannote_fallback(self, monkeypatch):
        """The reachability condition, negative case: `ENGINE_DIARIZER_REQUIRE_SIDECAR` unset
        (this repo's shipped default) plus the default `allow_local_fallback=True` synchronous
        call — the shape every ordinary transcription job takes — must NOT raise
        `DiarSidecarUnavailableError`. If this ever starts raising, the ladder tested above
        would fire on every ordinary sidecar hiccup, not just the intentionally fail-hard
        deployments.
        """
        monkeypatch.delenv("ENGINE_DIARIZER_REQUIRE_SIDECAR", raising=False)
        diarizer, fake_fallback = self._diarizer(monkeypatch)
        unavailable = DiarSidecarUnavailableError("wedged", reason="timeout")
        audio = np.zeros(16_000, dtype=np.float32)

        result = diarizer._refuse_or_fallback_impl(
            unavailable, audio=audio, allow_local_fallback=True
        )

        # Identity, not equality: this is the very object the fallback engine returned,
        # so a future implementation that rebuilt an equal-looking tuple would not pass.
        assert result is fake_fallback.diarize.return_value
        # And the fallback must be handed the SAME waveform the failed sidecar call had —
        # `assert_called_once()` alone would not notice it being passed something else.
        fake_fallback.diarize.assert_called_once_with(audio)
        assert diarizer.last_provider == "pyannote"

    def test_require_sidecar_true_is_the_only_default_synchronous_path_that_reaches_the_ladder(
        self, monkeypatch
    ):
        """The reachability condition, positive case: with `ENGINE_DIARIZER_REQUIRE_SIDECAR=true`
        (the only lever that makes the ladder live on the synchronous path — the overlapped
        thread aside), the same failure must propagate unchanged instead of being absorbed.
        This is the precondition the retry-wiring tests above implicitly assume; pinning it
        here means a change that quietly makes the ladder unreachable (e.g. the
        `diarizer_require_sidecar()` check being deleted or short-circuited) fails a test
        even though those tests keep passing.
        """
        monkeypatch.setenv("ENGINE_DIARIZER_REQUIRE_SIDECAR", "true")
        diarizer, fake_fallback = self._diarizer(monkeypatch)
        unavailable = DiarSidecarUnavailableError("wedged", reason="timeout")

        audio = np.zeros(16_000, dtype=np.float32)

        with pytest.raises(DiarSidecarUnavailableError) as excinfo:
            diarizer._refuse_or_fallback_impl(unavailable, audio=audio, allow_local_fallback=True)

        assert excinfo.value is unavailable
        fake_fallback.diarize.assert_not_called()
