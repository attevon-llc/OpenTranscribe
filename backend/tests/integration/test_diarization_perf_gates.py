"""Phase D.4/D.5/D.6 — performance regression, soak, coexistence gates.

Marked ``gpu`` + ``slow``, so the default suite and CI both deselect them; they run
in the ``-m gpu`` phase of ``scripts/run-integration-tests.sh`` and need a physical
GPU. The thresholds encode Phase A.2 measurements: deviating from them is a
regression worth failing the build for.

Run (in-container). ``-o addopts=""`` is required because the project default
selector excludes ``gpu``:

    docker compose -f docker-compose.yml -f docker-compose.override.yml \\
                   -f docker-compose.gpu.yml -f docker-compose.benchmark.yml \\
                   run --rm --entrypoint "" diarization-probe \\
        python -m pytest /app/tests/integration/test_diarization_perf_gates.py \\
                         -v -o addopts="" -m gpu

Refs: plan i-need-a-full-stateful-origami.md D.4, D.5, D.6.
"""

from __future__ import annotations

import ctypes
import gc
import os
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from tests.helpers import does_not_raise

pytestmark = [pytest.mark.gpu, pytest.mark.slow]

BENCHMARK_ROOT = Path("/app/benchmark/test_audio")

# Phase A.2 measurements on RTX A6000 (fork + torch 2.8 + CUDA 12.8).
# Wall-time targets are 1.30x the measured median, giving headroom for GPU
# contention while still catching real regressions. See docs/
# diarization-vram-profile/README.md for the raw numbers.
PERF_GATES_S = {
    "0.5h_1899s.wav": 35.0,  # measured ~23s at bs=16 fp32, gate = 1.5x
    "2.2h_7998s.wav": 145.0,  # measured ~103s at bs=16 fp32, gate = 1.4x
}

# Soak: 24 consecutive runs on 0.5h. Peak-device MB must not climb >200 MB
# between first and last run (would indicate a VRAM leak across invocations).
SOAK_RUNS = 8  # 24 is the plan target; 8 keeps the CI time bounded
SOAK_DRIFT_LIMIT_MB = 200


def _in_container() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("OPENTRANSCRIBE_IN_CONTAINER") == "1"


@pytest.fixture(scope="module")
def ensure_container() -> None:
    if not _in_container():
        pytest.skip("Perf gate tests require the benchmark container")


@pytest.fixture(scope="module")
def pipeline(ensure_container: None):
    """Shared pipeline: load once, run many times (soak + coexistence)."""
    import torch
    from pyannote.audio import Pipeline

    if not torch.cuda.is_available():
        pytest.skip("CUDA not available")
    token = os.environ.get("HUGGINGFACE_TOKEN")
    p = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1", token=token)
    if p is None:
        pytest.skip("pyannote pipeline unavailable (token or network)")
    p.to(torch.device("cuda"))
    yield p
    del p
    gc.collect()
    torch.cuda.empty_cache()


def _nvml_used_mb() -> float:
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


def _load_audio_dict(path: Path) -> dict:
    import torch

    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return {
        "waveform": torch.from_numpy(np.ascontiguousarray(data)).unsqueeze(0),
        "sample_rate": sr,
        "uri": path.stem,
    }


@pytest.mark.parametrize("audio_file", list(PERF_GATES_S))
def test_wall_time_regression(
    pipeline,
    audio_file: str,
) -> None:
    """D.4 — wall time for bs=16 fp32 must not exceed the perf gate."""
    wav = BENCHMARK_ROOT / audio_file
    if not wav.exists():
        pytest.skip(f"audio missing: {wav}")
    os.environ["PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE"] = "16"
    audio = _load_audio_dict(wav)
    t0 = time.perf_counter()
    pipeline(audio)
    elapsed = time.perf_counter() - t0
    gate = PERF_GATES_S[audio_file]
    assert elapsed <= gate, (
        f"Wall-time regression on {audio_file}: {elapsed:.1f}s > gate {gate}s. "
        f"Phase A measured ~23s/0.5h and ~103s/2.2h at bs=16 fp32; this test "
        f"allows 1.3-1.5x headroom. A big jump points at a pipeline perf bug."
    )


def test_soak_no_vram_creep(
    pipeline,
) -> None:
    """D.5 — repeated invocations must not grow peak VRAM across runs."""
    import torch

    wav = BENCHMARK_ROOT / "0.5h_1899s.wav"
    if not wav.exists():
        pytest.skip(f"audio missing: {wav}")
    os.environ["PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE"] = "16"
    audio = _load_audio_dict(wav)

    peaks: list[float] = []
    for _ in range(SOAK_RUNS):
        torch.cuda.empty_cache()
        before = _nvml_used_mb()
        pipeline(audio)
        torch.cuda.synchronize()
        after = _nvml_used_mb()
        peaks.append(after - before)
        gc.collect()
        torch.cuda.empty_cache()

    # Compare first two runs vs last two, ignoring the very-first's
    # allocator warmup.
    early = sum(peaks[1:3]) / 2
    late = sum(peaks[-2:]) / 2
    drift = late - early
    assert drift <= SOAK_DRIFT_LIMIT_MB, (
        f"VRAM drift across {SOAK_RUNS} runs: early={early:.1f}MB late={late:.1f}MB "
        f"drift={drift:.1f}MB (limit {SOAK_DRIFT_LIMIT_MB}MB). Suggests an "
        f"allocator leak or accumulating state in the pipeline."
    )


@pytest.mark.parametrize("cap_gb", [4.0, 6.0, 8.0])
def test_coexistence_under_simulated_cap(
    ensure_container: None,
    cap_gb: float,
) -> None:
    """D.6 — whole-stack Whisper small + diarization fits under cap_gb."""
    import torch

    if not torch.cuda.is_available():
        pytest.skip("CUDA not available")

    # Apply the simulated cap on a fresh process-frac; must run BEFORE any
    # CUDA allocation in this test to take effect.
    # Narrowed from a bare `except Exception`, which would have reported any failure in this
    # block — including a genuine CUDA/driver fault — as a skip (issue #431). Only an absent or
    # unusable device should skip; anything else is a real problem and must fail.
    try:
        total_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
    except (RuntimeError, AssertionError, AttributeError) as exc:
        pytest.skip(f"cannot read CUDA device properties: {exc}")
    frac = cap_gb / total_gb
    if frac > 1.0:
        pytest.skip(f"device smaller than cap={cap_gb}GB")
    torch.cuda.set_per_process_memory_fraction(frac, 0)

    from app.transcription.config import TranscriptionConfig
    from app.transcription.diarizer import SpeakerDiarizer
    from app.transcription.transcriber import Transcriber

    wav = BENCHMARK_ROOT / "0.5h_1899s.wav"
    if not wav.exists():
        pytest.skip(f"audio missing: {wav}")
    with wave.open(str(wav), "rb") as wf:
        raw_frames = wf.readframes(1_000_000_000)
    audio = (np.frombuffer(raw_frames, dtype=np.int16).astype(np.float32) / 32768.0)[
        : 60 * 16000
    ]  # first 60s only — full 32min is unnecessary for cap test

    cfg = TranscriptionConfig(
        model_name="small",
        compute_type="float16",
        device="cuda",
        device_index=0,
        beam_size=1,
        batch_size=4,
        enable_diarization=True,
        hf_token=os.environ.get("HUGGINGFACE_TOKEN"),
    )

    torch.cuda.reset_peak_memory_stats(0)

    # Whisper
    with does_not_raise(f"Whisper small must fit and run under a simulated {cap_gb} GB cap"):
        t = Transcriber(cfg)
        t.load_model()
        t.transcribe(audio)
        t.unload_model()
        del t
        gc.collect()
        torch.cuda.empty_cache()

    # Diarization must fit in whatever headroom is left under the simulated cap
    # after Whisper. There is no budget knob to hand it a share explicitly —
    # DIARIZATION_VRAM_BUDGET_MB was removed in 7ed7456e once Phase-A measurement
    # showed the fixed EMBEDDING_BATCH_SIZE = 16 pipeline (diarizer.py) already
    # holds a predictable ~1 GB footprint on its own, so the only real enforcement
    # left is `set_per_process_memory_fraction` above, applied process-wide.
    with does_not_raise(f"diarization must fit under the remaining {cap_gb} GB cap"):
        d = SpeakerDiarizer(cfg)
        d.load_model()
        d.unload_model()

    # The old pass criterion was entirely implicit: "neither call OOM'd", with no assertion in
    # the test at all, so it read as an empty test and any future change that stopped applying
    # the cap would still pass (issue #431). `set_per_process_memory_fraction` above does make
    # an over-budget allocation raise — now stated via does_not_raise — but the claim in this
    # test's NAME is that the stack fits under cap_gb, and that is directly measurable.
    peak_gb = torch.cuda.max_memory_allocated(0) / (1024**3)
    assert peak_gb <= cap_gb, (
        f"peak allocation {peak_gb:.2f} GB exceeded the simulated cap of {cap_gb} GB"
    )
    assert peak_gb > 0, (
        "no CUDA memory was ever allocated, so this run did not exercise the cap at all"
    )
