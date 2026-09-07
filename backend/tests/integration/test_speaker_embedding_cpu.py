"""Functional CPU-device regression test for speaker embedding extraction (issue #584).

Issue #584 was a queue-routing bug: `extract_speaker_embeddings_task` was hardcoded onto the
GPU-only Celery queue, so on a lite/CPU-only deployment the task never ran at all — it silently
never got picked up by any worker. That is fixed and unit-tested at the routing/mock level in
`tests/unit/test_postprocess.py::TestSpeakerEmbeddingQueueRouting`.

This file is the piece that was still missing: proof that *if* the task is correctly routed to
a CPU worker, the actual embedding extraction underneath (`SpeakerEmbeddingService`, backed by
`pyannote.audio.Inference`) produces a real, usable embedding on CPU — not silently zeros, NaNs,
the wrong shape, or a crash swallowed by the broad `except Exception` in
`extract_embedding_from_segment`.

Scope is deliberately narrow: "does CPU produce a valid, GPU-parity embedding" — not full
speaker-ID correctness (a separate ground-truth test, issue-tracked separately, owns that).

Device forcing uses the actual production knob: `app.utils.hardware_detection.detect_hardware()`
reads the `TORCH_DEVICE` env var (see `detect_hardware()`, hardware_detection.py:517-534) and
`SpeakerEmbeddingService.__init__` calls `detect_hardware()` fresh on every construction, so
`monkeypatch.setenv("TORCH_DEVICE", "cpu")` before constructing the service is the real
"deploy this in lite/CPU-only mode" code path — not a monkeypatch of internals.

Marked ``models`` (not ``gpu``): despite exercising the CPU path, this test needs real pyannote
model weights (WeSpeaker embedding model) to be present, which is exactly what the ``models``
marker gates on (see pyproject.toml's marker table) and it is auto-skipped via
``pytest.importorskip`` / a preload check when the weights are unavailable (e.g. fast CI),
mirroring `tests/redaction/test_presidio.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import numpy as np
import pytest

pytestmark = pytest.mark.models

MEDIA_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "media"
BOUNDARY_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "boundary"
SAMPLE_WAV = MEDIA_FIXTURES / "sample_short.wav"

# Real 2-speaker audio (issue #193's Karpathy clip), persisted on the host outside the repo per
# tests/fixtures/boundary/README.md. Gitignored — used only when present; the correctness
# assertions on `sample_short.wav` above do not depend on it.
KARPATHY_AUDIO = Path(
    "/mnt/nvm/repos/transcribe-app/benchmark/diarization-boundary/karpathy/"
    "karpathy_kwSVtQ7dziU/karpathy_10m.wav"
)

# Long, clean, single-speaker runs read off tests/fixtures/boundary/karpathy_10m.ref.words.json
# ground truth (word-level speaker labels), picked for being well separated in time and >5s long.
SARAH_SEGMENT = {"start": 0.0, "end": 7.0}
ANDREJ_SEGMENT = {"start": 7.7, "end": 20.0}


@pytest.fixture(autouse=True)
def _force_in_process_backend(monkeypatch):
    """Pin every test in this module to the in-process PyAnnote backend.

    Since issue #571 a v4 service prefers the diar-native sidecar and then loads no
    model and has no torch device at all — which is the right default, and exactly
    wrong for this module, whose subject is the `TORCH_DEVICE` device-selection knob
    on the in-process path. `USE_NATIVE_SPEAKER_EMBEDDINGS=false` is the production
    escape hatch for that, so this pins the backend through the real mechanism rather
    than by patching internals.
    """
    monkeypatch.setenv("USE_NATIVE_SPEAKER_EMBEDDINGS", "false")


def _embedding_service(mode: Literal["v3", "v4"] = "v4"):
    """Construct a SpeakerEmbeddingService, skipping if weights are unavailable."""
    pytest.importorskip("pyannote.audio", reason="pyannote.audio not installed")
    from app.services.speaker_embedding_service import SpeakerEmbeddingService

    try:
        return SpeakerEmbeddingService(mode=mode)
    except (OSError, PermissionError) as exc:
        # OSError: huggingface_hub network/local-cache-miss failures (e.g.
        # LocalEntryNotFoundError, which subclasses OSError) when weights are absent.
        # PermissionError: SpeakerEmbeddingService._initialize_model's own explicit raise
        # when Model.from_pretrained returns None (missing/ungated HF token). Both mean
        # "the model weights genuinely aren't available here" — a real skip, not masking a
        # code bug. Anything else (e.g. a bug in our own extraction code) is allowed to
        # fail the test instead of being silently swallowed.
        pytest.skip(f"pyannote embedding model unavailable: {exc}")


@pytest.fixture
def cpu_service(monkeypatch):
    """SpeakerEmbeddingService forced onto CPU via the real TORCH_DEVICE knob.

    Function-scoped (not module-scoped) deliberately: `monkeypatch` only undoes at the end of
    the *test*, so a module-scoped version of this fixture would leave TORCH_DEVICE=cpu set for
    every later test in the file — which is exactly what caused
    `test_cpu_matches_gpu_dimensionality_and_direction`'s own "GPU" service to silently come back
    as CPU (device leaked across tests, not caught by inspection — only by running the suite).
    """
    monkeypatch.setenv("TORCH_DEVICE", "cpu")
    service = _embedding_service()
    assert str(service.device) == "cpu", (
        f"TORCH_DEVICE=cpu did not force CPU device selection; got {service.device}. "
        "This is the exact mechanism lite/CPU-only deployments rely on — if this assertion "
        "fails, the device-forcing knob itself is broken, independent of embedding quality."
    )
    return service


def _expected_dim() -> int:
    from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4

    return int(PYANNOTE_EMBEDDING_DIMENSION_V4)


def test_cpu_extraction_produces_valid_embedding(cpu_service) -> None:
    """Core regression guard: CPU-forced extraction must yield a real, well-formed embedding.

    If CPU embedding extraction were silently broken (e.g. an exception swallowed by
    `extract_embedding_from_segment`'s broad `except Exception: return None`, or a model
    misload returning garbage), this test fails: either `embedding` is None, its shape does
    not match the expected v4 dimensionality, or it contains NaN/all-zero values.
    """
    assert SAMPLE_WAV.exists(), f"fixture missing: {SAMPLE_WAV}"

    embedding = cpu_service.extract_embedding_from_segment(
        str(SAMPLE_WAV), {"start": 1.0, "end": 6.0}
    )

    assert embedding is not None, "CPU extraction returned None — extraction silently failed"
    assert isinstance(embedding, np.ndarray)
    assert embedding.shape == (_expected_dim(),), (
        f"Expected shape ({_expected_dim()},), got {embedding.shape}"
    )
    assert not np.isnan(embedding).any(), "Embedding contains NaN values"
    assert not np.allclose(embedding, 0.0), "Embedding is all-zero — extraction produced garbage"
    # extract_embedding_from_segment L2-normalizes; a real embedding norms to ~1.0.
    norm = float(np.linalg.norm(embedding))
    assert 0.9 < norm < 1.1, f"Embedding not unit-normalized: norm={norm}"


def test_cpu_matches_gpu_dimensionality_and_direction() -> None:
    """GPU-parity check: CPU and GPU must produce embeddings of the same shape and (near)
    the same direction for the identical segment — a real regression check, not just "did not
    crash". A broken CPU path (e.g. wrong preprocessing, wrong model variant selected due to a
    device-branch bug) would still produce *a* vector of the right shape but pointing in a
    substantially different direction than the GPU reference, which this catches via cosine
    similarity.
    """
    import torch

    if not torch.cuda.is_available():
        pytest.skip("No CUDA device available on this host — cannot compute the GPU reference")

    assert SAMPLE_WAV.exists(), f"fixture missing: {SAMPLE_WAV}"
    segment = {"start": 1.0, "end": 6.0}

    gpu_service = _embedding_service()
    assert str(gpu_service.device).startswith("cuda"), (
        f"Expected GPU auto-detection with CUDA available, got {gpu_service.device}"
    )
    gpu_embedding = gpu_service.extract_embedding_from_segment(str(SAMPLE_WAV), segment)
    assert gpu_embedding is not None

    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    try:
        mp.setenv("TORCH_DEVICE", "cpu")
        cpu_service = _embedding_service()
        assert str(cpu_service.device) == "cpu"
        cpu_embedding = cpu_service.extract_embedding_from_segment(str(SAMPLE_WAV), segment)
    finally:
        mp.undo()

    assert cpu_embedding is not None
    assert cpu_embedding.shape == gpu_embedding.shape, (
        f"CPU shape {cpu_embedding.shape} != GPU shape {gpu_embedding.shape} for the same "
        "segment — a device-dependent shape mismatch would break every downstream consumer "
        "(OpenSearch kNN index, cosine speaker matching)."
    )
    # Both are L2-normalized, so dot product == cosine similarity.
    cosine = float(np.dot(cpu_embedding, gpu_embedding))
    assert cosine > 0.98, (
        f"CPU and GPU embeddings for the identical segment diverge (cosine={cosine:.4f}). "
        "Minor floating-point differences between CPU/GPU kernels are expected (hence <1.0, "
        "not requiring exact equality), but this should be near-identical, not merely "
        "'same shape, different vector'."
    )


def test_cpu_distinguishes_real_different_speakers(cpu_service) -> None:
    """Distinctness check on REAL two-speaker audio (not the synthetic sample_transcript.json
    dialogue, which is single-voice narration and cannot support this check — see
    tests/fixtures/media/README.md). Uses the Karpathy clip's ground-truth word-level speaker
    labels (tests/fixtures/boundary/karpathy_10m.ref.words.json) to pick two long, clean,
    single-speaker runs. A CPU embedding path that collapsed to a constant vector, or that
    ignored the audio content entirely, would pass the shape/NaN checks above but fail here —
    this is what makes this test more than a tautology.
    """
    if not KARPATHY_AUDIO.exists():
        pytest.skip(f"Karpathy audio not present on this host: {KARPATHY_AUDIO}")

    words_path = BOUNDARY_FIXTURES / "karpathy_10m.ref.words.json"
    words = json.loads(words_path.read_text())
    # Sanity-check the hardcoded segment windows still land inside single-speaker runs.
    sarah_words = [w for w in words if SARAH_SEGMENT["start"] <= w["start"] < SARAH_SEGMENT["end"]]
    andrej_words = [
        w for w in words if ANDREJ_SEGMENT["start"] <= w["start"] < ANDREJ_SEGMENT["end"]
    ]
    assert sarah_words and all(w["speaker"] == "Sarah" for w in sarah_words)
    assert andrej_words and all(w["speaker"] == "Andrej" for w in andrej_words)

    sarah_embedding = cpu_service.extract_embedding_from_segment(str(KARPATHY_AUDIO), SARAH_SEGMENT)
    andrej_embedding = cpu_service.extract_embedding_from_segment(
        str(KARPATHY_AUDIO), ANDREJ_SEGMENT
    )
    same_speaker_embedding = cpu_service.extract_embedding_from_segment(
        str(KARPATHY_AUDIO), {"start": 34.54, "end": 41.0}
    )  # a second, disjoint Sarah run

    assert sarah_embedding is not None
    assert andrej_embedding is not None
    assert same_speaker_embedding is not None

    cross_speaker_cosine = float(np.dot(sarah_embedding, andrej_embedding))
    same_speaker_cosine = float(np.dot(sarah_embedding, same_speaker_embedding))

    assert same_speaker_cosine > cross_speaker_cosine, (
        f"Same-speaker cosine ({same_speaker_cosine:.4f}) should exceed cross-speaker cosine "
        f"({cross_speaker_cosine:.4f}) — if CPU extraction produced embeddings that ignore "
        "actual voice content (e.g. a constant/degenerate vector), these would be roughly equal."
    )
    assert cross_speaker_cosine < 0.9, (
        f"Cross-speaker cosine similarity is implausibly high ({cross_speaker_cosine:.4f}) for "
        "two different real speakers — suggests the CPU embedding is not discriminating voices."
    )
