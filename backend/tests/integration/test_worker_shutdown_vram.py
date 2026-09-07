"""Issue #782 — proves ``release_worker_resources()`` actually frees VRAM.

Every other unit in the #782 plan is written and green (the ``worker_shutdown``, ``ask``,
``mark_shutting_down`` unit tests, and the stop-grace-period wiring test), but all of those
run against mocks or an unloaded ``ModelManager``: they prove the function runs without
raising, not that it releases the GPU memory it claims to. This is the ``-m gpu`` test that
closes that gap, mirroring ``test_diarizer_lifecycle.py``'s handoff-residue methodology
(Phase D.2) but pointed at the worker-shutdown path instead of a single model's unload.

FALSIFIABILITY (why this test would actually fail if the fix regressed): ``release_
worker_resources`` calls ``ModelManager.release_all()``, which frees both the transcriber
and the diarizer (``app/core/worker_shutdown.py::_release_models``, module docstring:
"NOT ``release_transcriber()`` -- that frees the transcriber only"). If a future edit
swapped that call for ``release_transcriber()`` alone, the diarizer -- ~950 MB process
footprint plus ~500 MB CUDA context per pipeline, measured at ~1.5 GB total
(``app/transcription/CLAUDE.md``, PyAnnote fallback specifics) -- would stay resident. That
1.5 GB residue is roughly 4x ``RESIDUE_GATE_MB`` below, so the gate would catch it rather
than pass on a shrunken but still-real leak.

Run (in-container -- the prod image has no pytest, so this needs the test image; issue
#577). ``-o addopts= -m gpu`` is mandatory: pyproject's default selector is
``-m 'not integration and not gpu'``, which both deselects this test AND (issue #719) hides
every CUDA device from the process, so an unadorned run either silently exits 0 or hits the
``cuda-device-guard`` hard failure -- never a false pass:

    ./scripts/run-diarization-gpu-tests.sh \\
        tests/integration/test_worker_shutdown_vram.py -v -o addopts= -m gpu
"""

from __future__ import annotations

import gc
import os
import time
from pathlib import Path

import pytest

from tests import gpu_memory

pytestmark = pytest.mark.gpu

# Phase A.6b floor for a single transcriber unload, established in
# test_diarizer_lifecycle.py:49 (``RESIDUE_GATE_MB = 350``, "Phase A.6b floor was 278 MB").
# release_worker_resources() releases the transcriber via the identical ModelManager code
# path plus the diarizer plus the trailing CUDA sweep, so it must clear the same floor: it
# does strictly more release work than the sibling test's single-model gate, never less.
RESIDUE_GATE_MB = 350


def _in_container() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("OPENTRANSCRIBE_IN_CONTAINER") == "1"


@pytest.fixture(scope="module")
def ensure_container() -> None:
    if not _in_container():
        pytest.skip("Lifecycle tests require the benchmark container (see D.2 docstring)")


@pytest.fixture(scope="module")
def torch_cuda() -> object:
    """torch + an available CUDA device, else skip.

    ``importorskip`` rather than ``try/except ImportError: pytest.skip`` -- same outcome
    for a genuinely absent torch (CPU-only worker), but it cannot swallow an ImportError
    raised from *inside* torch's own import, which would be a real failure reported as a
    skip (issue #431).
    """
    torch = pytest.importorskip("torch", reason="torch not installed (CPU-only environment)")
    if not torch.cuda.is_available():
        pytest.skip("CUDA not available")
    return torch


def _settled_nvml_reading(max_wait_s: float = 2.0, interval_s: float = 0.1) -> gpu_memory.GpuMemory:
    """Poll NVML until two consecutive readings agree, instead of betting a fixed sleep.

    A bare ``time.sleep`` before the residue read either fires too early (the driver
    hasn't finished settling, so the reading is a timing artifact rather than the real
    post-release state) or wastes time on an idle card that settles in one poll. This
    re-checks the actual condition the wait is standing in for: consecutive samples
    within 5 MB of each other. Bounded at ``max_wait_s`` so a card that never quite
    settles still returns its last reading rather than hanging the test.
    """
    previous = gpu_memory.read()
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        time.sleep(interval_s)
        current = gpu_memory.read()
        if abs(current.device_used_mb - previous.device_used_mb) < 5.0:
            return current
        previous = current
    return previous


def test_release_worker_resources_frees_vram(
    ensure_container: None,
    torch_cuda: object,
) -> None:
    """ModelManager-loaded transcriber+diarizer must be gone after worker shutdown.

    Loads BOTH models through the real ``ModelManager`` (no mocks -- this is an
    integration test), takes an NVML baseline via the co-tenant-subtracted
    ``gpu_memory`` helpers, calls the real ``release_worker_resources()``, and asserts
    the co-tenant-subtracted residue lands at or below the measured floor. Also asserts
    the release completed inside its own watchdog budget, since a wedged release is the
    other failure mode this module exists to prevent (docker SIGKILLing the process
    before the watchdog's own forced exit can run cleanly).
    """
    import torch

    from app.core import worker_shutdown
    from app.transcription.config import TranscriptionConfig
    from app.transcription.model_manager import ModelManager

    # Captured BEFORE anything in this process touches CUDA, so every PID in it belongs
    # to someone else. This host runs unrelated work on other GPUs; a raw nvidia-smi
    # total is not attributable to us without this subtraction -- see tests/gpu_memory.py
    # for the run where a co-tenant's own model load was misattributed as a regression.
    #
    # ⚠️ This MUST precede the release_all() below, and the first version of this test got
    # that backwards. `ModelManager.release_all()` ends in `_cleanup_gpu()`, which calls
    # `torch.cuda.empty_cache()` -- and that INITIALISES this process's CUDA context even
    # when there is nothing to free. Running it first put our own PID into the co-tenant
    # set, so `usage_excluding` subtracted our own memory and every delta collapsed to ~0.
    # `assert_self_is_measured` caught it (VacuousMeasurementError) rather than letting it
    # report a comfortable pass, which is the entire reason that guard exists.
    co_tenants = gpu_memory.co_tenant_pids()
    before = gpu_memory.read()
    baseline = gpu_memory.usage_excluding(before, co_tenants)

    mm = ModelManager.get_instance()
    # Clean slate against a sibling module having left models loaded in this same pytest
    # process. Safe to run after the baseline: on a fresh container it frees nothing, and
    # if it DID free something, that memory was in the baseline and residue only gets
    # smaller -- it can never manufacture a pass.
    mm.release_all()

    cfg = TranscriptionConfig(
        model_name="base",
        compute_type="float16",
        device="cuda",
        device_index=0,
        beam_size=1,
        batch_size=8,
        hf_token=os.environ.get("HUGGINGFACE_TOKEN"),
    )

    transcriber = mm.get_transcriber(cfg)
    assert transcriber.is_loaded

    diarizer = mm.get_diarizer(cfg)
    assert diarizer.is_loaded

    started = time.monotonic()
    worker_shutdown.release_worker_resources()
    elapsed = time.monotonic() - started

    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()

    after = _settled_nvml_reading()
    # Raises rather than reporting ~0 residue if the baseline was taken too late to be a
    # baseline -- turns "measured nothing" into an ERROR instead of a false pass.
    gpu_memory.assert_self_is_measured(after, co_tenants)
    post_release = gpu_memory.usage_excluding(after, co_tenants)
    residue = post_release - baseline

    # worker_shutdown._BUDGET_S is the module's own watchdog ceiling
    # (OT_WORKER_SHUTDOWN_BUDGET_S, default 20s): past this, the watchdog force-exits the
    # process rather than let docker's stop_grace_period SIGKILL it. A release that only
    # "passes" by racing that timer is not evidence the shutdown path is safe.
    assert elapsed < worker_shutdown._BUDGET_S, (
        f"release_worker_resources() took {elapsed:.1f}s, at or beyond its own "
        f"{worker_shutdown._BUDGET_S:.1f}s watchdog budget -- the watchdog would have "
        f"force-exited the process before this assertion could even run."
    )

    assert residue <= RESIDUE_GATE_MB, (
        f"worker shutdown residue regression: {residue:.1f} MB above baseline "
        f"(gate: {RESIDUE_GATE_MB} MB). baseline={baseline:.1f} "
        f"post_release={post_release:.1f}. Dropping the diarizer release (calling "
        f"release_transcriber() instead of release_all() -- see _release_models()'s "
        f"docstring) is measured at ~1.5 GB residue, well above this gate.\n"
        f"  before: {gpu_memory.describe(before, co_tenants)}\n"
        f"  after:  {gpu_memory.describe(after, co_tenants)}\n"
        f"A co-tenant PID present in 'after' but absent from 'before' started mid-test "
        f"and is counted against us -- re-run on a quieter card before believing this "
        f"number."
    )
