"""Tier 1 of the privacy policy at the diarizer: what a job may ask of it (issue #1280).

The sidecar is asked for gender, and per-speaker centroids come back from it, unless this job's
privacy answers say no. Both are PER CALL arguments. The ModelManager shares one diarizer, and
the config it was built with, across every job on a worker, so a value left on that config
belongs to whichever job built it: a test below pins that a stale permissive config cannot
widen a restrictive call.

The pieces: ``job_privacy_overrides`` (the policy read), the three local pipeline builders that
apply it, the cross-stage snapshot that carries it to the split diarize worker, ``_run_diarize``
and the two engines that honour it.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.services.privacy.gates import job_privacy_overrides
from app.tasks.transcription import pipelines
from app.transcription import config as tconfig
from app.transcription import diarizer_native
from app.transcription.config import TranscriptionConfig
from app.transcription.diarizer_native import NativeSpeakerDiarizer
from app.transcription.engine.config import EngineConfig
from app.transcription.engine.stages import _run_diarize
from tests.unit._plan_contexts import plan_context

# ------------------------------------------------------------------ the policy read


def test_an_allowed_job_overrides_nothing(privacy_world, normal_user):
    media_file = privacy_world.new_file(normal_user)

    assert job_privacy_overrides(media_file.id) == {}


@pytest.mark.parametrize(
    ("feature", "expected"),
    [
        ("voiceprints", {"enable_native_embeddings": False}),
        ("gender", {"request_gender": False}),
    ],
)
@pytest.mark.parametrize("layer", ["user", "organization", "instance"])
def test_each_forbidden_feature_restricts_only_itself(
    privacy_world, normal_user, feature, expected, layer
):
    org_id = privacy_world.new_org().id if layer == "organization" else None
    media_file = privacy_world.new_file(normal_user, org_id)
    if layer == "user":
        privacy_world.set_user(normal_user, feature, "false")
    elif layer == "organization":
        privacy_world.set_org(org_id, **{feature: False})
    else:
        privacy_world.set_instance(feature, "false")

    assert job_privacy_overrides(media_file.id) == expected


def test_a_file_that_cannot_be_read_is_restricted_on_both(privacy_world):
    assert job_privacy_overrides(2_000_000_000) == {
        "enable_native_embeddings": False,
        "request_gender": False,
    }


# ------------------------------------------------------------------ the pipeline builders

USER_SETTINGS = {
    "min_speakers": 1,
    "max_speakers": 5,
    "vad_threshold": 0.4,
    "vad_min_silence_ms": 900,
    "vad_min_speech_ms": 200,
    "vad_speech_pad_ms": 300,
    "hallucination_silence_threshold": 2.5,
    "repetition_penalty": 1.1,
}


class _StopError(Exception):
    """Raised by the fakes once the config under test has been captured."""


def _build(**overrides) -> TranscriptionConfig:
    tc = TranscriptionConfig(device="cpu")
    for key, value in overrides.items():
        if hasattr(tc, key):
            setattr(tc, key, value)
    return tc


@pytest.fixture
def builders(monkeypatch):
    @contextlib.contextmanager
    def _scope():
        yield MagicMock()

    monkeypatch.setattr(pipelines, "session_scope", _scope)
    monkeypatch.setattr(
        pipelines, "_get_user_transcription_settings", lambda db, uid: USER_SETTINGS
    )
    monkeypatch.setattr(pipelines, "_resolve_language_settings", lambda c, s, t: ("en", False))
    monkeypatch.setattr(pipelines, "update_task_status", lambda *a, **k: None)
    monkeypatch.setattr(pipelines, "send_progress_notification", lambda *a, **k: None)
    monkeypatch.setattr(pipelines, "load_vocabulary_terms", lambda db, uid, fid: [])
    monkeypatch.setattr(
        tconfig.TranscriptionConfig,
        "from_environment",
        classmethod(lambda cls, **overrides: _build(**overrides)),
    )

    captured: list = []

    class _Pipeline:
        def __init__(self, config):
            captured.append(config)
            raise _StopError

    monkeypatch.setattr("app.transcription.TranscriptionPipeline", _Pipeline)

    class _Engine:
        def __init__(self, config):
            pass

        def run_gpu_stage(self, pre, progress_callback=None):
            captured.append(pre.config_snapshot)
            raise _StopError

        def run_transcribe_only(self, pre, progress_callback=None):
            captured.append(pre.config_snapshot)
            raise _StopError

    monkeypatch.setattr("app.transcription.Engine", _Engine)
    monkeypatch.setattr(
        EngineConfig, "from_db_with_env_fallback", classmethod(lambda cls, db: EngineConfig())
    )

    def _engine_from_env(cls, **overrides):
        engine = EngineConfig()
        engine._transcription_config = _build(**overrides)
        return engine

    monkeypatch.setattr(EngineConfig, "from_environment", classmethod(_engine_from_env))
    return captured


def _ctx(media_file, user):
    return MagicMock(task_id="t", file_id=media_file.id, user_id=user.id)


def _flags(captured) -> tuple[bool, bool]:
    first = captured[0]
    if isinstance(first, dict):
        return first["enable_native_embeddings"], first["request_gender"]
    return first.enable_native_embeddings, first.request_gender


def _run(builder: str, ctx, builders):
    context = {"pipeline_plan": plan_context()}
    if builder == "monolithic":
        call = lambda: pipelines._run_transcription_pipeline(  # noqa: E731
            ctx, "/scratch/a.wav", None, None, None, enable_diarization=True
        )
    elif builder == "engine":
        call = lambda: pipelines._run_engine_pipeline(ctx, "/scratch/a.wav", context)  # noqa: E731
    else:
        call = lambda: pipelines._run_transcribe_only_stage(ctx, "/scratch/a.wav", context)  # noqa: E731
    with pytest.raises(_StopError):
        call()
    return builders


BUILDERS = ["monolithic", "engine", "transcribe_only"]


@pytest.mark.parametrize("builder", BUILDERS)
def test_an_allowed_job_keeps_the_deployments_defaults(
    privacy_world, builders, normal_user, builder
):
    media_file = privacy_world.new_file(normal_user)

    _run(builder, _ctx(media_file, normal_user), builders)

    assert _flags(builders) == (True, True)


@pytest.mark.parametrize("builder", BUILDERS)
@pytest.mark.parametrize(
    ("feature", "expected"), [("voiceprints", (False, True)), ("gender", (True, False))]
)
def test_a_forbidden_feature_reaches_the_jobs_config(
    privacy_world, builders, normal_user, builder, feature, expected
):
    media_file = privacy_world.new_file(normal_user)
    privacy_world.set_user(normal_user, feature, "false")

    _run(builder, _ctx(media_file, normal_user), builders)

    assert _flags(builders) == expected


# ------------------------------------------------------------------ the cross-stage snapshot


def test_the_snapshot_carries_gender_to_the_split_diarize_worker():
    engine = EngineConfig()
    engine._transcription_config = _build(request_gender=False, enable_native_embeddings=False)

    rebuilt = EngineConfig.from_snapshot(engine.to_snapshot()).transcription_config

    assert (rebuilt.request_gender, rebuilt.enable_native_embeddings) == (False, False)


def test_a_snapshot_without_the_keys_keeps_todays_defaults():
    rebuilt = EngineConfig.from_snapshot({}).transcription_config

    assert (rebuilt.request_gender, rebuilt.enable_native_embeddings) == (True, True)


# ------------------------------------------------------------------ the native diarizer


class _SidecarConfig:
    """A STALE, permissive config: what a worker's shared diarizer was built with."""

    num_speakers = None
    enable_overlap_detection = False
    enable_native_embeddings = True


def _sidecar_reply(url, payload, timeout):
    return {
        "exclusive_segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
        "segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
        "centroids": [[3.0, 4.0]],
        "speaker_gender": {"SPEAKER_00": {"gender": "female", "confidence": 0.9}},
    }


class _RecordingDiarizer(NativeSpeakerDiarizer):
    """The real client, plus the request bodies it posted."""

    sent: list[dict]


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    sent: list[dict] = []

    def _post(url, payload, timeout):
        sent.append(payload)
        return _sidecar_reply(url, payload, timeout)

    monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(tmp_path / "scratch"))
    monkeypatch.setattr(diarizer_native, "_GENDER_ENABLED", True)
    monkeypatch.setattr(diarizer_native, "post_json", _post)
    diarizer = _RecordingDiarizer(_SidecarConfig(), base_url="http://fake-sidecar")
    diarizer.is_loaded = True
    diarizer.sent = sent
    return diarizer


AUDIO = np.zeros(16_000, dtype=np.float32)


def test_the_sidecar_is_asked_for_gender_and_returns_centroids_by_default(sidecar):
    _, _, centroids = sidecar.diarize(AUDIO)

    assert sidecar.sent[0]["gender"] is True
    assert list(centroids) == ["SPEAKER_00"]


def test_a_job_that_may_not_have_gender_inferred_never_asks_the_sidecar(sidecar):
    sidecar.diarize(AUDIO, request_gender=False)

    assert sidecar.sent[0]["gender"] is False


def test_the_deployments_own_gender_switch_still_wins_over_an_allowed_job(sidecar, monkeypatch):
    monkeypatch.setattr(diarizer_native, "_GENDER_ENABLED", False)

    sidecar.diarize(AUDIO, request_gender=True)

    assert sidecar.sent[0]["gender"] is False


def test_a_per_call_refusal_beats_a_stale_permissive_config(sidecar):
    _, _, centroids = sidecar.diarize(AUDIO, return_centroids=False)

    assert centroids == {}


def test_an_unspecified_call_leaves_the_configs_own_answer_alone(sidecar):
    sidecar.config.enable_native_embeddings = False

    _, _, centroids = sidecar.diarize(AUDIO, return_centroids=True)

    # A permissive call cannot widen a config that already says no.
    assert centroids == {}


def test_the_pyannote_fallback_is_told_the_same_thing(tmp_path, monkeypatch):
    """A sidecar loss degrades to the in-process engine, which must honour the job too."""
    received: list[dict] = []

    class _Fallback:
        def diarize(self, audio, **per_call):
            received.append(per_call)
            return ("result", {}, {})

    diarizer = NativeSpeakerDiarizer(_SidecarConfig(), base_url="http://fake-sidecar")
    monkeypatch.setattr(diarizer, "_fallback_engine", lambda: _Fallback())
    monkeypatch.delenv("ENGINE_DIARIZER_REQUIRE_SIDECAR", raising=False)

    diarizer._refuse_or_fallback_impl(RuntimeError("down"), AUDIO, True, return_centroids=False)

    assert received == [{"return_centroids": False}]


# ------------------------------------------------------------------ _run_diarize hands tc over


class _Recorder:
    def __init__(self):
        self.calls: list[dict] = []

    def diarize(self, audio, **kwargs):
        self.calls.append(kwargs)
        return ("df", {}, {})


class _NativeRecorder(NativeSpeakerDiarizer):
    def __init__(self):
        self.calls: list[dict] = []

    def diarize(self, audio, **kwargs):  # type: ignore[override]
        self.calls.append(kwargs)
        return ("df", {}, {})


@pytest.mark.parametrize(
    ("embeddings", "gender"), [(True, True), (False, True), (True, False), (False, False)]
)
def test_run_diarize_passes_this_jobs_answers_to_the_native_engine(embeddings, gender):
    diarizer = _NativeRecorder()
    tc = _build(enable_native_embeddings=embeddings, request_gender=gender)

    _run_diarize(diarizer, AUDIO, None, tc=tc)

    assert diarizer.calls[0]["return_centroids"] is embeddings
    assert diarizer.calls[0]["request_gender"] is gender


def test_run_diarize_passes_this_jobs_centroid_answer_to_the_pyannote_engine():
    diarizer = _Recorder()

    _run_diarize(diarizer, AUDIO, None, tc=_build(enable_native_embeddings=False))

    assert diarizer.calls == [{"return_centroids": False}]


def test_run_diarize_without_a_job_config_restricts_nothing():
    native, pyannote = _NativeRecorder(), _Recorder()

    _run_diarize(native, AUDIO, None)
    _run_diarize(pyannote, AUDIO, None)

    assert native.calls[0]["return_centroids"] is None
    assert native.calls[0]["request_gender"] is None
    assert pyannote.calls == [{"return_centroids": None}]


# ------------------------------------------------------------------ the in-process engine


class _Track:
    def __init__(self, start, end):
        self.start, self.end = start, end


class _Annotation:
    def labels(self):
        return ["SPEAKER_00"]

    def itertracks(self, yield_label=True):
        return iter([(_Track(0.0, 1.0), None, "SPEAKER_00")])


def _pyannote_with_centroids():
    """The real ``SpeakerDiarizer.diarize`` over a stubbed pipeline that returns a centroid."""
    from types import SimpleNamespace

    from app.transcription.diarizer import SpeakerDiarizer

    class _StubbedPipeline(SpeakerDiarizer):
        def _run_pipeline_with_oom_retry(self, *args, **kwargs):
            return SimpleNamespace(speaker_embeddings=np.array([[3.0, 4.0]]))

        def _extract_diarization(self, output, prefer_exclusive: bool = True):
            return _Annotation()

    engine = _StubbedPipeline(
        _build(
            num_speakers=None,
            min_speakers=1,
            max_speakers=4,
            diarization_device="cpu",
            device_index=0,
            enable_overlap_detection=False,
            enable_native_embeddings=True,
        )
    )
    engine._pipeline = object()
    return engine


def test_the_pyannote_engine_returns_centroids_when_the_call_allows_them():
    _, _, centroids = _pyannote_with_centroids().diarize(AUDIO)

    assert list(centroids) == ["SPEAKER_00"]


def test_the_pyannote_engine_returns_none_when_the_call_forbids_them():
    _, _, centroids = _pyannote_with_centroids().diarize(AUDIO, return_centroids=False)

    assert centroids == {}


# ------------------------------------------------------------------ the local provider


@pytest.mark.parametrize(
    ("return_centroids", "request_gender"),
    [(True, True), (False, True), (True, False), (False, False)],
)
def test_the_local_provider_builds_the_jobs_config_from_the_files_answers(
    monkeypatch, tmp_path, return_centroids, request_gender
):
    from app.services.diarization.local_provider import LocalDiarizationProvider
    from app.services.diarization.types import DiarizeConfig

    built: list[TranscriptionConfig] = []
    handed: list = []

    def _from_env(cls, **overrides):
        built.append(_build(**overrides))
        return built[-1]

    monkeypatch.setattr(tconfig.TranscriptionConfig, "from_environment", classmethod(_from_env))
    monkeypatch.setattr("app.transcription.audio.load_audio", lambda path: AUDIO)
    monkeypatch.setattr(
        "app.transcription.model_manager.ModelManager.get_instance",
        lambda: MagicMock(get_diarizer=MagicMock(return_value=MagicMock())),
    )

    def _run(diarizer, audio, wav_path, tc=None):
        handed.append(tc)
        from app.transcription.diarize_result import DiarizeResult

        empty = DiarizeResult(
            start=np.array([]), end=np.array([]), speaker=np.array([], dtype=object)
        )
        return empty, {}, {}

    monkeypatch.setattr("app.transcription.engine.stages._run_diarize", _run)
    config = DiarizeConfig(return_centroids=return_centroids, request_gender=request_gender)

    LocalDiarizationProvider().diarize(str(tmp_path / "a.wav"), config)

    # The config the engine runs with, and the one _run_diarize reads the answers from.
    assert handed == built
    assert built[0].enable_native_embeddings is return_centroids
    assert built[0].request_gender is request_gender
