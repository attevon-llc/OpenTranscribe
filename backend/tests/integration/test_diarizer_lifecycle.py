"""Phase D.2 — model-lifecycle + VRAM-policy integration tests.

Asserts three invariants:

1. Transcriber load -> transcribe -> unload -> diarizer load -> run completes
   without error using the production ModelManager.
2. Handoff residue (NVML delta from pre-load baseline to post-
   release_transcriber) is within _CUDA_CONTEXT_MB + 50 MB tolerance.
   Measured floor is 300 MB on (torch 2.8.0+cu128, driver 580.126, A6000);
   tolerance = 350 MB. Any regression above this means new unreleased
   state was introduced in Transcriber.unload_model or its callees.
3. SpeakerDiarizer's embedding batch size is pinned at EMBEDDING_BATCH_SIZE = 16
   (superseding the earlier DIARIZATION_VRAM_BUDGET_MB budget-aware auto-scaler,
   removed in 7ed7456e once Phase-A measurement showed bs=16 matches larger
   batches on throughput/DER at a fraction of the VRAM — see
   docs/diarization-vram-profile/README.md). PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE
   is set to match, so pyannote's own batching agrees with ours.

Run (in-container):

    docker compose -f docker-compose.yml -f docker-compose.override.yml \\
                   -f docker-compose.gpu.yml -f docker-compose.benchmark.yml \\
                   run --rm --entrypoint "" diarization-probe \\
        python -m pytest /app/tests/integration/test_diarizer_lifecycle.py -v

Refs: plan i-need-a-full-stateful-origami.md D.2.
"""

from __future__ import annotations

import ctypes
import os
import wave
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.gpu

BENCHMARK_ROOT = Path("/app/benchmark/test_audio")

# Phase A.6b measured floor + 50 MB tolerance for future jitter.
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

    ``importorskip`` rather than ``try/except ImportError: pytest.skip`` — same outcome for a
    genuinely absent torch (CPU-only worker), but it cannot swallow an ImportError raised from
    *inside* torch's own import, which would be a real failure reported as a skip (issue #431).
    """
    torch = pytest.importorskip("torch", reason="torch not installed (CPU-only environment)")
    if not torch.cuda.is_available():
        pytest.skip("CUDA not available")
    return torch


def _nvml_used_mb() -> float:
    """Current NVML device_used in MB for device 0, or 0.0 on failure."""
    try:
        lib = ctypes.CDLL("libnvidia-ml.so.1")
        lib.nvmlInit_v2()
        handle = ctypes.c_void_p()
        lib.nvmlDeviceGetHandleByIndex(0, ctypes.byref(handle))

        class _Mem(ctypes.Structure):
            _fields_ = [
                ("total", ctypes.c_ulonglong),
                ("free", ctypes.c_ulonglong),
                ("used", ctypes.c_ulonglong),
            ]

        mem = _Mem()
        lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem))
        return float(mem.used) / (1024**2)
    except Exception:
        return 0.0


def _load_audio(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != 16000:
            raise AssertionError("expected 16kHz audio")
        return (
            np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32)
            / 32768.0
        )


def test_handoff_residue_within_gate(
    ensure_container: None,
    torch_cuda: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transcriber -> release must not leave > RESIDUE_GATE_MB above baseline."""
    import gc
    import time

    import torch

    from app.transcription.config import TranscriptionConfig
    from app.transcription.transcriber import Transcriber

    wav_path = BENCHMARK_ROOT / "0.5h_1899s.wav"
    if not wav_path.exists():
        pytest.skip(f"benchmark audio missing: {wav_path}")
    audio = _load_audio(wav_path)

    baseline = _nvml_used_mb()

    cfg = TranscriptionConfig(
        model_name="base",
        compute_type="float16",
        device="cuda",
        device_index=0,
        beam_size=1,
        batch_size=8,
        enable_diarization=False,
    )
    t = Transcriber(cfg)
    t.load_model()
    t.transcribe(audio)
    t.unload_model()
    del t
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()
    time.sleep(0.5)  # let NVML settle

    post_release = _nvml_used_mb()
    residue = post_release - baseline

    assert residue <= RESIDUE_GATE_MB, (
        f"Handoff residue regression: {residue:.1f} MB above baseline "
        f"(gate: {RESIDUE_GATE_MB} MB). baseline={baseline:.1f} "
        f"post_release={post_release:.1f}. Phase A.6b floor was 278 MB; a "
        f"jump suggests new unreleased state in Transcriber.unload_model."
    )


def test_embedding_batch_is_pinned_at_16(
    ensure_container: None,
    torch_cuda: object,
) -> None:
    """Loaded diarizer must pin embedding_batch_size = 16 regardless of VRAM.

    The Phase A finding is that bs=16 saturates throughput; any future code
    change that auto-scales above 16 or falls below 16 on "tight" GPUs is a
    regression we want to catch here.
    """
    from app.transcription.config import TranscriptionConfig
    from app.transcription.diarizer import SpeakerDiarizer

    assert SpeakerDiarizer.EMBEDDING_BATCH_SIZE == 16, (
        "The Phase A pinned value changed. Before touching it, re-run "
        "scripts/vram-probe-diarization.py --small-batch-sweep and update "
        "docs/diarization-vram-profile/README.md."
    )

    cfg = TranscriptionConfig(
        model_name="base",
        compute_type="float16",
        device="cuda",
        device_index=0,
        hf_token=os.environ.get("HUGGINGFACE_TOKEN"),
    )
    diarizer = SpeakerDiarizer(cfg)
    diarizer.load_model()
    assert diarizer._pipeline is not None
    assert diarizer._pipeline.embedding_batch_size == 16
    assert os.environ.get("PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE") == "16"
    diarizer.unload_model()


def test_model_manager_sequential_handoff(
    ensure_container: None,
    torch_cuda: object,
) -> None:
    """ModelManager.release_transcriber + get_diarizer completes."""
    import torch

    from app.transcription.config import TranscriptionConfig
    from app.transcription.model_manager import ModelManager

    cfg = TranscriptionConfig(
        model_name="base",
        compute_type="float16",
        device="cuda",
        device_index=0,
        beam_size=1,
        batch_size=8,
        hf_token=os.environ.get("HUGGINGFACE_TOKEN"),
    )
    mm = ModelManager.get_instance()
    mm.release_all()  # clean slate

    transcriber = mm.get_transcriber(cfg)
    assert transcriber.is_loaded
    mm.release_transcriber()
    torch.cuda.empty_cache()

    diarizer = mm.get_diarizer(cfg)
    assert diarizer.is_loaded
    mm.release_all()
    torch.cuda.empty_cache()
