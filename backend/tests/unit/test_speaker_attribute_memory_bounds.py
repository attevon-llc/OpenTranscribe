"""Memory bounds for speaker-attribute (gender) detection (issue #1066).

A burst of long meetings OOM-killed a 6 GiB CPU worker: each detection fed whole merged
speaking turns (minutes long in a meeting) to wav2vec2, whose activation memory grows with
input length, and every prefork child kept its own copy of the model. These tests pin the
three bounds: the clip cap, the per-host concurrency slots, and the release of the model on
CPU workers.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
from contextlib import contextmanager
from unittest.mock import MagicMock

import numpy as np
import pytest
import torch

from app.services import speaker_attribute_service as sas
from app.services.audio_segment_utils import center_window
from app.tasks import speaker_attribute_task as sat
from app.utils.host_slots import host_slot
from tests.unit.test_speaker_attribute_task_tracking import _make_file_with_speech
from tests.unit.test_speaker_attribute_task_tracking import _task_row_for
from tests.unit.test_speaker_attribute_task_tracking import tracking_env  # noqa: F401

SR = 16000

# --- the clip cap ------------------------------------------------------------------------


@pytest.fixture
def recording_service():
    """A loaded service whose feature extractor records the length it was handed."""
    svc = sas.SpeakerAttributeService()
    svc._model_loaded = True
    seen: list[int] = []

    def extractor(audio, **_kwargs):
        seen.append(len(audio))
        return {"input_values": torch.zeros(1, 10)}

    svc._feature_extractor = extractor
    svc._model = MagicMock(return_value=MagicMock(logits=torch.tensor([[0.2, 0.8]])))
    return svc, seen


def test_run_inference_never_feeds_the_model_more_than_the_cap(recording_service, monkeypatch):
    monkeypatch.delenv("SPEAKER_ATTRIBUTE_MAX_CLIP_SECONDS", raising=False)
    svc, seen = recording_service

    svc._run_inference(np.zeros(300 * SR, dtype=np.float32))  # a 5-minute speaking turn

    assert seen == [int(sas.DEFAULT_MAX_CLIP_SECONDS * SR)]


def test_run_inference_leaves_a_short_clip_alone(recording_service):
    svc, seen = recording_service

    svc._run_inference(np.zeros(3 * SR, dtype=np.float32))

    assert seen == [3 * SR]


def test_clip_cap_is_env_configurable_and_rejects_garbage(monkeypatch):
    monkeypatch.setenv("SPEAKER_ATTRIBUTE_MAX_CLIP_SECONDS", "8")
    assert sas.max_clip_seconds() == 8.0
    for bad in ("abc", "0", "1.5"):
        monkeypatch.setenv("SPEAKER_ATTRIBUTE_MAX_CLIP_SECONDS", bad)
        assert sas.max_clip_seconds() == sas.DEFAULT_MAX_CLIP_SECONDS


def test_center_crop_takes_the_middle():
    audio = np.arange(100 * SR, dtype=np.float32)
    cropped = sas.center_crop(audio, 10)
    assert len(cropped) == 10 * SR
    assert cropped[0] == 45 * SR


def test_center_window_shrinks_only_long_segments():
    assert center_window({"start": 5.0, "end": 12.0}, 20) == {"start": 5.0, "end": 12.0}
    assert center_window({"start": 100.0, "end": 400.0}, 20) == {"start": 240.0, "end": 260.0}


# --- per-host concurrency slots -----------------------------------------------------------


@pytest.fixture
def slot_name(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    return "test-slots"


def test_host_slot_admits_only_limit_holders(slot_name):
    with host_slot(slot_name, 2) as first, host_slot(slot_name, 2) as second:
        with host_slot(slot_name, 2) as third:
            assert (first, second, third) == (True, True, False)
    with host_slot(slot_name, 2) as again:
        assert again is True


def test_host_slot_zero_means_unbounded(slot_name):
    with host_slot(slot_name, 0) as a, host_slot(slot_name, 0) as b:
        assert a and b


def _hold_slot_forever(name: str, tempdir: str, ready) -> None:
    import tempfile

    tempfile.tempdir = tempdir
    with host_slot(name, 1) as got:
        ready.put(got)
        signal.pause()  # held until the test SIGKILLs this process


def test_a_sigkilled_holder_does_not_leak_its_slot(slot_name, tmp_path):
    """The OOM killer sends SIGKILL; the slot must come back without any cleanup code."""
    ctx = multiprocessing.get_context("fork")
    ready = ctx.Queue()
    holder = ctx.Process(target=_hold_slot_forever, args=(slot_name, str(tmp_path), ready))
    holder.start()
    try:
        assert ready.get(timeout=10) is True
        with host_slot(slot_name, 1) as while_held:
            assert while_held is False
        assert holder.pid is not None
        os.kill(holder.pid, signal.SIGKILL)
        holder.join(timeout=10)
        with host_slot(slot_name, 1) as after_kill:
            assert after_kill is True
    finally:
        if holder.is_alive():
            holder.kill()


# --- the task -----------------------------------------------------------------------------


def _long_turn_file(db_session, user):
    """One speaker with a single 5-minute uninterrupted turn."""
    return _make_file_with_speech(db_session, user, speakers=1, seg_seconds=150.0)


def test_task_fetches_only_capped_windows(db_session, normal_user, tracking_env, monkeypatch):  # noqa: F811
    media_file, _ = _long_turn_file(db_session, normal_user)
    seen: list[float] = []

    def fake_inference(audio_source, work_items, service):
        seen.extend(seg["end"] - seg["start"] for _, seg in work_items)
        return {}, {}

    monkeypatch.setattr(sat, "_run_gender_inference_parallel", fake_inference)

    sat.detect_speaker_attributes_task.apply(args=[str(media_file.uuid), normal_user.id]).get()

    assert seen, "no work items reached inference"
    assert max(seen) <= sas.DEFAULT_MAX_CLIP_SECONDS + 1e-6, seen


def test_task_requeues_itself_when_every_slot_is_taken(
    db_session,
    normal_user,
    tracking_env,  # noqa: F811
    monkeypatch,
    slot_name,
):
    """A busy host defers the detection; it must not run past the bound, nor be lost."""
    media_file, _ = _make_file_with_speech(db_session, normal_user)
    monkeypatch.setattr(sat, "_INFERENCE_SLOT_NAME", slot_name)
    monkeypatch.setenv("SPEAKER_ATTRIBUTE_MAX_CONCURRENCY", "1")
    attempts = {"n": 0}
    real_host_slot = sat.host_slot

    @contextmanager
    def _all_taken():
        yield False

    def busy_once(name, limit):
        attempts["n"] += 1
        return _all_taken() if attempts["n"] == 1 else real_host_slot(name, limit)

    monkeypatch.setattr(sat, "host_slot", busy_once)

    result = sat.detect_speaker_attributes_task.apply(
        args=[str(media_file.uuid), normal_user.id]
    ).get()

    assert attempts["n"] == 2, "the task did not retry after finding no free slot"
    assert result["status"] == "success", result
    task = _task_row_for(db_session, media_file.id)
    assert task is not None and task.status == "completed"


def test_task_gives_up_cleanly_when_slots_never_free(
    db_session,
    normal_user,
    tracking_env,  # noqa: F811
    monkeypatch,
    slot_name,
):
    media_file, _ = _make_file_with_speech(db_session, normal_user)
    monkeypatch.setattr(sat, "_INFERENCE_SLOT_NAME", slot_name)
    monkeypatch.setenv("SPEAKER_ATTRIBUTE_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(sat.detect_speaker_attributes_task, "max_retries", 2)

    with host_slot(slot_name, 1) as held:
        assert held
        result = sat.detect_speaker_attributes_task.apply(
            args=[str(media_file.uuid), normal_user.id]
        ).get()

    assert result == {"status": "error", "reason": "no_inference_slot"}
    task = _task_row_for(db_session, media_file.id)
    assert task is not None and task.status == "failed"


@pytest.mark.parametrize(("gpu_worker", "expect_release"), [(False, True), (True, False)])
def test_cpu_worker_releases_the_model_after_detection(
    db_session,
    normal_user,
    tracking_env,  # noqa: F811
    monkeypatch,
    gpu_worker,
    expect_release,
):
    media_file, _ = _make_file_with_speech(db_session, normal_user)
    if gpu_worker:
        monkeypatch.setenv("PRELOAD_GPU_MODELS", "true")
    else:
        monkeypatch.delenv("PRELOAD_GPU_MODELS", raising=False)
    released = {"n": 0}
    monkeypatch.setattr(
        sas, "release_cached_attribute_service", lambda: released.__setitem__("n", 1)
    )

    sat.detect_speaker_attributes_task.apply(args=[str(media_file.uuid), normal_user.id]).get()

    assert bool(released["n"]) is expect_release


def test_release_drops_the_cached_model(monkeypatch):
    svc = sas.SpeakerAttributeService()
    svc._model_loaded = True
    svc._model = MagicMock()
    monkeypatch.setattr(sas, "_cached_service", svc)

    sas.release_cached_attribute_service()

    assert sas._cached_service is None
    assert svc._model is None and svc._model_loaded is False
