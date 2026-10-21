"""A sticky CUDA fault must not take toxicity scoring down for the life of the worker.

GPU-free: the pipeline is a fake that raises the same message the driver produced on the
review stack ("unspecified launch failure"), which also makes the move back to CPU fail.
"""

from __future__ import annotations

import pytest

from app.services.redaction import device
from app.services.redaction.detectors import toxicity

FAULT = RuntimeError("CUDA error: unspecified launch failure")


class _Pipe:
    def __init__(self, fail: Exception | None):
        self.fail = fail
        self.calls = 0

    def __call__(self, payload, **_kwargs):
        self.calls += 1
        if self.fail:
            raise self.fail
        items = payload if isinstance(payload, list) else [payload]
        return [[{"label": "toxic", "score": 0.9}] for _ in items]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(device, "_cuda_faulted", False)
    monkeypatch.setattr(toxicity, "_place_on_device", lambda *_a: None)
    monkeypatch.setattr(toxicity, "_pipes", {})
    yield


def test_cuda_fault_reloads_on_cpu_and_pins_the_process_to_cpu(monkeypatch):
    broken, healthy = _Pipe(FAULT), _Pipe(None)
    toxicity._pipes["m"] = broken
    monkeypatch.setattr(
        toxicity, "_get_pipe", lambda name: toxicity._pipes.setdefault(name, healthy)
    )

    scores = toxicity._run_pipe("m", broken, ["a", "b"], batch_size=2)

    assert [s[0]["score"] for s in scores] == [0.9, 0.9]
    assert broken.calls == 1 and healthy.calls == 1
    assert device._cuda_faulted is True
    assert device.resolve_device() == "cpu"


def test_a_model_error_is_not_mistaken_for_a_cuda_fault():
    pipe = _Pipe(ValueError("bad input"))
    with pytest.raises(ValueError, match="bad input"):
        toxicity._run_pipe("m", pipe, ["a"])
    assert device._cuda_faulted is False
    assert pipe.calls == 1


def test_fault_classifier():
    assert device.is_cuda_fault(FAULT)
    assert not device.is_cuda_fault(ValueError("shape mismatch"))
