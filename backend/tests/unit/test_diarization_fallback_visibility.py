"""The native-sidecar fallback is visible, counted, and optionally strict (issue #1184).

The in-process PyAnnote fork is a working failover for the diar-native sidecar, which is why a
deployment could run on the slower engine for days with every setting reading "native". These
tests pin the four things that now make that visible: a per-run WARNING with the reason, the
``diarization_runs_total{backend,reason}`` counter, the strict mode
(``engine.diarizer_require_sidecar``) refusing the fallback at build time as well as mid-job,
and the admin status saying so.

In-process on purpose: the sidecar boundary is patched at the same seams the other
diar-native suites use (``post_json``, ``sidecar_ready``, ``sidecar_healthy``); wire-level
behaviour has its own suites.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
from prometheus_client import REGISTRY

import app.transcription.diarizer_native as diarizer_native
from app.transcription.diarization_runs import record_diarization_run
from app.transcription.diarization_runs import run_outcome
from app.transcription.diarizer_native import DiarSidecarUnavailableError
from app.transcription.diarizer_native import NativeSpeakerDiarizer
from app.transcription.engine.stages import _run_diarize
from app.transcription.model_manager import ModelManager

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("diar_native_state")]


def _cfg() -> Any:
    """A config stand-in typed Any: the code under test only reads a few attributes."""
    return _Config()


def _count(backend: str, reason: str) -> float:
    value = REGISTRY.get_sample_value(
        "diarization_runs_total", {"backend": backend, "reason": reason}
    )
    return value or 0.0


class _Diarizer:
    """The attributes ``record_diarization_run`` reads, nothing else."""

    def __init__(self, provider: str, reason: str | None = None) -> None:
        self.last_provider = provider
        self.last_fallback_reason = reason


class _Config:
    num_speakers = None
    enable_overlap_detection = False
    enable_native_embeddings = False
    diarizer_backend = "native"


# ------------------------------------------------------------------ the run record


def test_a_native_run_is_counted_as_served_without_a_warning(caplog: pytest.LogCaptureFixture):
    before = _count("native", "served")
    with caplog.at_level(logging.WARNING):
        record_diarization_run(_Diarizer("native"))
    assert _count("native", "served") == before + 1
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_fallback_run_is_counted_with_its_reason_and_warns_every_time(
    caplog: pytest.LogCaptureFixture,
):
    before = _count("pyannote", "unreachable")
    with caplog.at_level(logging.WARNING):
        record_diarization_run(_Diarizer("pyannote", "unreachable"))
        record_diarization_run(_Diarizer("pyannote", "unreachable"))
    assert _count("pyannote", "unreachable") == before + 2
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2, "a WARNING on EVERY fallback run, not once"
    assert all("unreachable" in w and "FALLBACK" in w for w in warnings)


def test_an_explicit_pyannote_pin_is_counted_as_pinned_and_does_not_warn(
    caplog: pytest.LogCaptureFixture,
):
    before = _count("pyannote", "pinned")
    with caplog.at_level(logging.WARNING):
        record_diarization_run(_Diarizer("pyannote"))
    assert _count("pyannote", "pinned") == before + 1
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert run_outcome(_Diarizer("pyannote")) == ("pyannote", "pinned")


def test_recording_never_raises_into_a_transcription(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr("app.core.metrics.diarization_runs_total", None)
    with caplog.at_level(logging.DEBUG, logger="app.transcription.diarization_runs"):
        record_diarization_run(_Diarizer("native"))  # must not raise
    assert any("could not record" in r.getMessage() for r in caplog.records)


def test_run_diarize_records_the_engine_that_actually_served():
    """The shared call site for the sequential path, the overlapped thread and rediarize."""
    fake = MagicMock()
    fake.diarize.return_value = ("df", {}, {})
    fake.last_provider = "pyannote"
    fake.last_fallback_reason = "timeout"
    before = _count("pyannote", "timeout")

    result = _run_diarize(fake, np.zeros(16, dtype=np.float32), None)

    assert result == ("df", {}, {})
    assert _count("pyannote", "timeout") == before + 1


# -------------------------------------------- the native client records why it fell back


def _loaded_native(monkeypatch: pytest.MonkeyPatch) -> NativeSpeakerDiarizer:
    class _Fallback:
        def diarize(self, audio, *, return_centroids=None):
            return ("fallback-df", {}, {})

    diarizer = NativeSpeakerDiarizer(_Config(), base_url="http://fake-sidecar")
    diarizer.is_loaded = True
    monkeypatch.setattr(diarizer, "_fallback_engine", lambda: _Fallback())
    monkeypatch.setattr(diarizer_native, "diarizer_require_sidecar", lambda: False)
    return diarizer


def test_a_classified_sidecar_failure_records_its_reason(monkeypatch: pytest.MonkeyPatch):
    diarizer = _loaded_native(monkeypatch)
    audio = np.zeros(16, dtype=np.float32)

    diarizer._refuse_or_fallback_impl(
        DiarSidecarUnavailableError("slow", reason="timeout"), audio, True
    )

    assert diarizer.last_provider == "pyannote"
    assert diarizer.last_fallback_reason == "timeout"
    assert run_outcome(diarizer) == ("pyannote", "timeout")


def test_an_unclassified_failure_is_recorded_as_error(monkeypatch: pytest.MonkeyPatch):
    diarizer = _loaded_native(monkeypatch)
    diarizer._refuse_or_fallback_impl(OSError("disk full"), np.zeros(16, np.float32), True)
    assert diarizer.last_fallback_reason == "error"


def test_a_later_native_success_clears_the_reason(monkeypatch: pytest.MonkeyPatch):
    diarizer = _loaded_native(monkeypatch)
    diarizer._refuse_or_fallback_impl(OSError("x"), np.zeros(16, np.float32), True)
    assert diarizer.last_fallback_reason == "error"

    reply = {"exclusive_segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}]}
    monkeypatch.setattr(
        diarizer, "_post_own_copy", lambda audio, timeout, request_gender=True: (reply, None)
    )
    diarizer.diarize(np.zeros(16_000, dtype=np.float32))

    assert diarizer.last_provider == "native"
    assert diarizer.last_fallback_reason is None
    assert run_outcome(diarizer) == ("native", "served")


# ----------------------------------------------------------- strict mode at build time


class _CountingPyAnnote:
    built: list[Any] = []

    def __init__(self, config):
        self.config = config
        self.last_fallback_reason = None
        _CountingPyAnnote.built.append(self)

    def load_model(self) -> None:
        pass


@pytest.fixture
def native_cannot_load(monkeypatch: pytest.MonkeyPatch):
    _CountingPyAnnote.built = []
    monkeypatch.setattr("app.transcription.model_manager.SpeakerDiarizer", _CountingPyAnnote)

    def _load(self):
        raise RuntimeError("diar-native sidecar unavailable")

    monkeypatch.setattr(NativeSpeakerDiarizer, "load_model", _load)
    return monkeypatch


def _manager() -> ModelManager:
    return ModelManager.__new__(ModelManager)


def test_default_mode_falls_back_and_remembers_why(native_cannot_load, caplog):
    native_cannot_load.setattr(diarizer_native, "diarizer_require_sidecar", lambda: False)
    native_cannot_load.setattr(diarizer_native, "sidecar_healthy", lambda *a, **k: False)

    with caplog.at_level(logging.WARNING):
        built = _manager()._build_diarizer(_cfg())

    assert isinstance(built, _CountingPyAnnote)
    assert built.last_fallback_reason == "unreachable"
    assert any("unreachable" in r.getMessage() for r in caplog.records)


def test_a_sidecar_that_answers_but_cannot_serve_is_reported_as_not_ready(native_cannot_load):
    native_cannot_load.setattr(diarizer_native, "diarizer_require_sidecar", lambda: False)
    native_cannot_load.setattr(diarizer_native, "sidecar_healthy", lambda *a, **k: True)

    built = _manager()._build_diarizer(_cfg())

    assert built.last_fallback_reason == "sidecar_not_ready"


def test_strict_mode_refuses_to_build_the_fallback(native_cannot_load):
    native_cannot_load.setattr(diarizer_native, "diarizer_require_sidecar", lambda: True)
    native_cannot_load.setattr(diarizer_native, "sidecar_healthy", lambda *a, **k: False)

    with pytest.raises(DiarSidecarUnavailableError, match="refused") as excinfo:
        _manager()._build_diarizer(_cfg())

    assert excinfo.value.reason == "unreachable"
    assert _CountingPyAnnote.built == [], "strict mode must never construct PyAnnote"


def test_strict_mode_does_not_touch_a_pinned_pyannote(monkeypatch: pytest.MonkeyPatch):
    _CountingPyAnnote.built = []
    monkeypatch.setattr("app.transcription.model_manager.SpeakerDiarizer", _CountingPyAnnote)
    monkeypatch.setattr(diarizer_native, "diarizer_require_sidecar", lambda: True)
    config = _cfg()
    config.diarizer_backend = "pyannote"

    built = _manager()._build_diarizer(config)

    assert isinstance(built, _CountingPyAnnote)
    assert built.last_fallback_reason is None


# -------------------------------------------- a cached fallback engine under strict mode


class _CachedPyAnnote:
    pass


def _cached_engine() -> Any:
    """A non-native engine, typed Any: ``_diarizer_current`` only reads its class name."""
    return _CachedPyAnnote()


@pytest.mark.parametrize(
    ("strict", "ready", "keep_cached_pyannote"),
    [
        (False, False, True),  # sidecar still down: keep the engine that works
        (False, True, False),  # sidecar back: rebuild onto native (unchanged behaviour)
        (True, False, False),  # strict: a cached fallback must not keep serving
        (True, True, False),
    ],
)
def test_a_cached_fallback_engine_is_dropped_in_strict_mode(
    monkeypatch: pytest.MonkeyPatch, strict: bool, ready: bool, keep_cached_pyannote: bool
):
    monkeypatch.setattr(diarizer_native, "sidecar_ready", lambda *a, **k: ready)
    monkeypatch.setattr(diarizer_native, "sidecar_healthy", lambda *a, **k: ready)
    monkeypatch.setattr(diarizer_native, "diarizer_require_sidecar", lambda: strict)

    current = ModelManager._diarizer_current(_cached_engine(), _cfg())

    assert current is keep_cached_pyannote


# ------------------------------------------------------------- worker start, admin status


def test_worker_start_logs_one_error_when_the_sidecar_is_down(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(diarizer_native, "sidecar_ready", lambda *a, **k: False)
    monkeypatch.setattr(diarizer_native, "diarizer_require_sidecar", lambda: False)
    manager = _manager()
    monkeypatch.setattr(manager, "get_transcriber", lambda config: None)
    get_diarizer = MagicMock(name="get_diarizer")
    monkeypatch.setattr(manager, "get_diarizer", get_diarizer)

    with caplog.at_level(logging.WARNING):
        manager.ensure_models_loaded(_cfg())

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "not reachable" in errors[0] and "fallback" in errors[0]
    get_diarizer.assert_not_called()


def test_worker_start_error_says_tasks_will_fail_in_strict_mode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(diarizer_native, "sidecar_ready", lambda *a, **k: False)
    monkeypatch.setattr(diarizer_native, "diarizer_require_sidecar", lambda: True)
    manager = _manager()
    monkeypatch.setattr(manager, "get_transcriber", lambda config: None)

    with caplog.at_level(logging.ERROR):
        manager.ensure_models_loaded(_cfg())

    assert any("will FAIL" in r.getMessage() for r in caplog.records)


def _status(monkeypatch: pytest.MonkeyPatch, *, strict: bool, ready: bool) -> dict[str, Any]:
    from app.transcription.config import TranscriptionConfig

    monkeypatch.setattr(
        TranscriptionConfig, "_resolve_diarizer_backend", staticmethod(lambda: "native")
    )
    monkeypatch.setattr(diarizer_native, "sidecar_ready", lambda *a, **k: ready)
    monkeypatch.setattr(diarizer_native, "diarizer_require_sidecar", lambda: strict)
    return diarizer_native.describe_diarizer_status()


def test_status_reports_the_fallback_when_not_strict(monkeypatch: pytest.MonkeyPatch):
    status = _status(monkeypatch, strict=False, ready=False)
    assert (status["effective"], status["using_fallback"]) == ("pyannote", True)
    assert (status["require_sidecar"], status["refusing"]) == (False, False)


def test_status_reports_that_strict_mode_is_refusing_work(monkeypatch: pytest.MonkeyPatch):
    status = _status(monkeypatch, strict=True, ready=False)
    assert status["effective"] == "unavailable"
    assert status["using_fallback"] is False
    assert (status["require_sidecar"], status["refusing"]) == (True, True)
    assert "refused" in status["effective_description"]


def test_status_is_clean_when_strict_and_the_sidecar_serves(monkeypatch: pytest.MonkeyPatch):
    status = _status(monkeypatch, strict=True, ready=True)
    assert (status["effective"], status["using_fallback"]) == ("native", False)
    assert (status["require_sidecar"], status["refusing"]) == (True, False)


# ------------------------------------- the acoustic re-check stays non-fatal, even to strict mode


def test_a_dropped_sidecar_cannot_fail_a_job_whose_acoustic_recheck_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch,
):
    """Diarization finished; in strict mode the re-check's own get_diarizer() then raises."""
    from types import SimpleNamespace

    from app.transcription.engine import stages as stages_mod

    manager = MagicMock(name="manager")
    manager.get_diarizer.side_effect = DiarSidecarUnavailableError("gone", reason="unreachable")
    diarize_df = SimpleNamespace(speaker_gender=None)
    monkeypatch.setattr(
        stages_mod,
        "_collect_diarization",
        lambda *a, **k: (diarize_df, {}, {"SPEAKER_00": [0.1, 0.2]}, "native", "community-1"),
    )
    monkeypatch.setattr(
        "app.transcription.speaker_assigner.assign_speakers",
        lambda df, transcript: {"segments": [], "language": "en"},
    )
    config = SimpleNamespace(boundary_acoustic_recheck_enabled=True)

    result, df = stages_mod._GpuStage()._run_diarization(
        np.zeros(16, dtype=np.float32),
        {"segments": []},
        MagicMock(name="profiler"),
        MagicMock(name="hw"),
        SimpleNamespace(enable_dedup=False),
        manager,
        None,
        config=config,
    )

    assert manager.get_diarizer.called, "the re-check must have asked for its diarizer"
    assert result["diarization_provider"] == "native"
    assert df is diarize_df


# ----------------------------- a cached fallback engine must not keep reporting a fallback


def test_pinning_pyannote_clears_the_fallback_reason_a_cached_engine_carries(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.transcription.diarizer import SpeakerDiarizer

    engine = SpeakerDiarizer(_cfg())
    engine.last_fallback_reason = "unreachable"  # built while the sidecar was down
    config = _cfg()
    config.diarizer_backend = "pyannote"  # the operator has since pinned PyAnnote

    assert ModelManager._diarizer_current(engine, config) is True
    assert run_outcome(engine) == ("pyannote", "pinned")


def test_a_cached_fallback_engine_keeps_its_reason_while_native_is_still_selected(
    monkeypatch: pytest.MonkeyPatch,
):
    """Control: with native selected and the sidecar still down, the reason is still true."""
    from app.transcription.diarizer import SpeakerDiarizer

    monkeypatch.setattr(diarizer_native, "sidecar_ready", lambda *a, **k: False)
    monkeypatch.setattr(diarizer_native, "sidecar_healthy", lambda *a, **k: False)
    monkeypatch.setattr(diarizer_native, "diarizer_require_sidecar", lambda: False)
    engine = SpeakerDiarizer(_cfg())
    engine.last_fallback_reason = "unreachable"

    assert ModelManager._diarizer_current(engine, _cfg()) is True
    assert run_outcome(engine) == ("pyannote", "unreachable")
