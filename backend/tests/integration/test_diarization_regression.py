"""Phase D.3 — regression tests against Phase A.3 reference RTTMs.

For each reference config in docs/diarization-vram-profile/raw/rttm/,
run the current pipeline with that config and assert DER == 0 against
the committed reference. This catches any future code change that
accidentally perturbs the diarization output — the Phase A RTTMs are
the "golden" answer.

Run (in-container — the prod image has no pytest, so this needs the test image;
issue #577):

    ./scripts/run-diarization-gpu-tests.sh \\
        tests/integration/test_diarization_regression.py -v -o addopts= -m gpu

(The path is ``tests/...``, not ``backend/tests/...``: docker-compose.benchmark.yml
mounts ``./backend`` at ``/app``.)

Refs: plan i-need-a-full-stateful-origami.md D.3.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

# Skip entire module if GPU not available or not running in the benchmark container.
pytestmark = pytest.mark.gpu

BENCHMARK_ROOT = Path("/app/benchmark/test_audio")
RTTM_ROOT = Path("/app/docs/diarization-vram-profile/raw/rttm")

# Phase A.3 reference: fp32 bs=16 unlimited r=0 for each file.
REFERENCE_CONFIGS = [
    ("0.5h_1899s.wav", 16, "off"),
    ("2.2h_7998s.wav", 16, "off"),
]


def _in_container() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("OPENTRANSCRIBE_IN_CONTAINER") == "1"


@pytest.fixture(scope="module")
def ensure_container() -> None:
    if not _in_container():
        pytest.skip("Regression tests require the benchmark container (see D.3 docstring)")


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


def _load_audio(path: Path) -> dict:
    import torch

    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    if sr != 16000:
        raise AssertionError(f"Reference corpus must be 16kHz, got {sr}")
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return {
        "waveform": torch.from_numpy(np.ascontiguousarray(data)).unsqueeze(0),
        "sample_rate": 16000,
        "uri": path.stem,
    }


def _read_rttm(rttm_path: Path) -> Any:
    from pyannote.core import Annotation
    from pyannote.core import Segment

    ann = Annotation(uri=rttm_path.stem)
    with open(rttm_path) as f:
        for line in f:
            parts = line.strip().split()
            if not parts or parts[0] != "SPEAKER":
                continue
            start = float(parts[3])
            dur = float(parts[4])
            ann[Segment(start, start + dur)] = parts[7]
    return ann


@pytest.mark.parametrize("audio,bs,mp", REFERENCE_CONFIGS)
def test_regression_against_reference_rttm(
    ensure_container: None,
    torch_cuda: object,
    audio: str,
    bs: int,
    mp: str,
) -> None:
    """Replay the Phase A.3 reference config and assert DER == 0."""
    from pyannote.audio import Pipeline
    from pyannote.metrics.diarization import DiarizationErrorRate

    rttm_name = f"{Path(audio).stem}__cap-unl__bs-{bs}__mp-{mp}__r0.rttm"
    ref_path = RTTM_ROOT / rttm_name
    if not ref_path.exists():
        pytest.skip(f"Reference RTTM missing: {ref_path}")

    reference = _read_rttm(ref_path)
    wav = BENCHMARK_ROOT / audio
    if not wav.exists():
        pytest.skip(f"Reference audio missing: {wav}")

    import torch

    # Bypass the fork's free-VRAM auto-scaler for reproducibility with the A.3
    # reference — this is the ONLY thing controlling batch size here.
    # `pipeline.vram_budget_mb` is never read once this env var is set (the fork's
    # `_force` branch short-circuits before it), so setting it here would be dead
    # code exercising nothing: previously this test parametrized 3 "budget" values
    # that all produced byte-identical runs, tripling GPU time for zero coverage.
    os.environ["PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE"] = str(bs)

    token = os.environ.get("HUGGINGFACE_TOKEN")
    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1", token=token)
    if pipeline is None:
        pytest.skip("pyannote pipeline unavailable (token or network)")
    pipeline.to(torch.device("cuda"))

    audio_dict = _load_audio(wav)
    output = pipeline(audio_dict)
    hypothesis = getattr(output, "speaker_diarization", output)

    # Drop the pipeline before DER compute to free VRAM for the next param.
    del pipeline
    import gc

    gc.collect()
    torch.cuda.empty_cache()

    metric = DiarizationErrorRate(collar=0.25, skip_overlap=False)
    der = float(metric(reference, hypothesis))

    # Regression gate: the reference IS the current behaviour at the same
    # config, so DER must be exactly 0. A non-zero here means something
    # changed in the pipeline between A.3 (2026-04-20) and this test run.
    assert der == pytest.approx(0.0, abs=1e-4), (
        f"DER regression: {der:.4f} on {audio} bs={bs} mp={mp}. "
        f"Pipeline output drifted from the Phase A.3 reference. Check "
        f"docs/diarization-vram-profile/raw/rttm/{rttm_name} vs current output."
    )

    # Speaker count must match reference.
    ref_spk = len(reference.labels())
    hyp_spk = len(hypothesis.labels())
    assert hyp_spk == ref_spk, (
        f"Speaker-count regression: ref={ref_spk} hyp={hyp_spk} on {audio} bs={bs}"
    )
