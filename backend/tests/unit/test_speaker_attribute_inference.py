"""Characterization of the inference runtime split out of ``speaker_attribute_task``."""

import numpy as np
import pytest

from app.tasks import speaker_attribute_inference as sai


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 2),
        ("", 2),
        ("   ", 2),
        ("5", 5),
        (" 3 ", 3),
        ("0", 0),
        ("-4", 0),
        ("abc", 2),
    ],
)
def test_max_concurrency_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("SPEAKER_ATTRIBUTE_MAX_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("SPEAKER_ATTRIBUTE_MAX_CONCURRENCY", raw)
    assert sai._max_concurrency() == expected


@pytest.mark.parametrize(
    ("value", "expected"), [("true", True), ("TRUE", True), ("false", False), ("", False)]
)
def test_is_gpu_worker(monkeypatch, value, expected):
    monkeypatch.setenv("PRELOAD_GPU_MODELS", value)
    assert sai._is_gpu_worker() is expected


def test_model_in_use_releases_only_when_last_user_leaves_on_cpu_worker(monkeypatch):
    released = []
    monkeypatch.setattr(
        "app.services.speaker_attribute_service.release_cached_attribute_service",
        lambda: released.append(1),
    )
    monkeypatch.delenv("PRELOAD_GPU_MODELS", raising=False)
    with sai._model_in_use():
        with sai._model_in_use():
            pass
        assert released == []
        assert sai._model_users == 1
    assert released == [1]
    assert sai._model_users == 0


def test_model_in_use_keeps_model_on_gpu_worker_and_on_error(monkeypatch):
    released = []
    monkeypatch.setattr(
        "app.services.speaker_attribute_service.release_cached_attribute_service",
        lambda: released.append(1),
    )
    monkeypatch.setenv("PRELOAD_GPU_MODELS", "true")
    with sai._model_in_use():
        pass
    assert released == []
    monkeypatch.setenv("PRELOAD_GPU_MODELS", "false")
    with pytest.raises(RuntimeError), sai._model_in_use():
        raise RuntimeError("boom")
    assert released == [1]
    assert sai._model_users == 0


def test_load_models_with_timeout_delegates(monkeypatch):
    seen: dict[str, object] = {}

    def fake(fn, *, timeout, label):
        seen.update(fn=fn, timeout=timeout, label=label)

    monkeypatch.setattr("app.utils.hf_hub_offline.load_with_timeout", fake)

    class Svc:
        def load_models(self):
            pass

    svc = Svc()
    sai._load_models_with_timeout(svc, timeout=7)
    assert seen["fn"] == svc.load_models
    assert seen["timeout"] == 7
    assert seen["label"] == "Gender-detection model load"


def test_run_gender_inference_parallel_aggregates_and_skips_bad_clips(monkeypatch):
    clips = {
        0.0: np.zeros(16000, dtype=np.float32),
        1.0: np.zeros(100, dtype=np.float32),  # too short: dropped
        2.0: None,  # decode failure: dropped
        3.0: np.zeros(16000, dtype=np.float32),
        4.0: "raise",
    }

    def fake_extract(source, start, duration):
        assert source == "src"
        clip = clips[start]
        if isinstance(clip, str):
            raise OSError("ffmpeg died")
        return clip

    monkeypatch.setattr(sai, "extract_audio_segment_np", fake_extract)

    class Svc:
        def _run_inference(self, audio):
            return "female", 0.5

    items = [(7, {"start": s, "end": s + 1.0}) for s in clips] + [(8, {"start": 3.0, "end": 4.0})]
    probs, counts = sai._run_gender_inference_parallel("src", items, Svc())
    assert probs == {7: {"male": 0.0, "female": 1.0}, 8: {"male": 0.0, "female": 0.5}}
    assert counts == {7: 2, 8: 1}
