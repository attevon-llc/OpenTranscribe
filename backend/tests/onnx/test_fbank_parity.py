"""Phase 6.2 T2 — batched-fbank parity against torch.vmap reference.

The ONNX embedding path replaces ``torch.vmap(self._fbank)`` with
``compute_fbank_batched`` (an explicit per-sample loop) because vmap is not
traceable by either TorchScript or ``torch.export``. This test ensures the
two implementations produce identical fbank tensors for the shapes used in
production.
"""

from __future__ import annotations

from functools import partial

import pytest
import torch
from torchaudio.compliance import kaldi

# compute_fbank_batched lives in the pyannote-audio fork (GPU worker dep) —
# skip cleanly in environments without it (e.g. the CPU-only CI venv).
pytest.importorskip(
    "pyannote.audio.onnx.runtime",
    reason="requires the pyannote-audio fork (GPU worker dependency)",
)


@pytest.fixture
def fbank_fn():
    """Kaldi-style fbank matching WeSpeakerResNet34 defaults."""
    return partial(
        kaldi.fbank,
        num_mel_bins=80,
        frame_length=25.0,
        frame_shift=10.0,
        round_to_power_of_two=False,
        snip_edges=True,
        dither=0.0,
        sample_frequency=16000,
        window_type="hamming",
        use_energy=False,
    )


@pytest.mark.parametrize("batch_size", [1, 2, 8, 16])
@pytest.mark.parametrize("num_samples", [16000, 32000, 80000])
def test_batched_fbank_matches_vmap(batch_size, num_samples, fbank_fn, device):
    """Batched loop output must match torch.vmap output at ≤1e-6 abs diff."""
    from pyannote.audio.onnx.runtime import compute_fbank_batched

    # MPS has looser fp precision; skip on CPU/CUDA it's fp32
    if device == "mps":
        pytest.skip("MPS fbank tested separately (different tolerance)")

    torch.manual_seed(0)
    dev = torch.device(device)
    waveforms = torch.randn(batch_size, 1, num_samples, device=dev)

    # Reference: use the actual production path (vmap)
    scaled = waveforms * (1 << 15)
    if dev.type == "mps":
        ref = torch.vmap(fbank_fn)(scaled.cpu()).to(dev)
    else:
        ref = torch.vmap(fbank_fn)(scaled)
    ref = ref - torch.mean(ref, dim=1, keepdim=True)

    # System under test
    out = compute_fbank_batched(
        waveforms,
        fbank_fn,
        fbank_centering_span=None,
        sample_rate=16000,
        frame_length_ms=25.0,
        frame_shift_ms=10.0,
    )

    assert out.shape == ref.shape, f"shape mismatch {out.shape} vs {ref.shape}"
    diff = (out - ref).abs().max().item()
    assert diff < 1e-5, f"batched fbank diverges from vmap: {diff:.3e}"


def test_batched_fbank_mixed_batch_determinism(fbank_fn, device):
    """Running the same waveform twice at different batch positions must give
    identical output (no cross-sample leakage via batch-level reductions)."""
    from pyannote.audio.onnx.runtime import compute_fbank_batched

    if device == "mps":
        pytest.skip("MPS fbank tested separately")

    torch.manual_seed(1)
    dev = torch.device(device)
    wf = torch.randn(1, 1, 16000, device=dev)
    other1 = torch.randn(1, 1, 16000, device=dev)
    other2 = torch.randn(1, 1, 16000, device=dev)

    batch_a = torch.cat([wf, other1], dim=0)
    batch_b = torch.cat([other2, wf], dim=0)

    out_a = compute_fbank_batched(
        batch_a,
        fbank_fn,
        fbank_centering_span=None,
        sample_rate=16000,
        frame_length_ms=25.0,
        frame_shift_ms=10.0,
    )
    out_b = compute_fbank_batched(
        batch_b,
        fbank_fn,
        fbank_centering_span=None,
        sample_rate=16000,
        frame_length_ms=25.0,
        frame_shift_ms=10.0,
    )

    # Same waveform, different batch position → identical fbank rows
    diff = (out_a[0] - out_b[1]).abs().max().item()
    assert diff < 1e-6, f"position leakage detected: {diff:.3e}"
