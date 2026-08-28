"""The measurement issue #571 turns on: are the two embedding backends the same model?

`SpeakerEmbeddingService` can serve v4 (256-d) embeddings either by loading
`pyannote/wespeaker-voxceleb-resnet34-LM` in-process or by calling the diar-native
sidecar's `/embed_window`. The claim is that these are *the same weights*, re-exported
to ONNX — not "close enough". That claim is what licenses replacing the in-process load,
and what licenses leaving already-indexed voiceprints in place instead of re-embedding
them, so it is asserted here against both real implementations rather than argued in a
comment.

Reference measurement (2026-08-28, 134 AMI ground-truth single-speaker windows across 6
meetings, 21 speakers), cosine between the two paths on identical audio:

    exactly one model window (10 s): mean 0.9999997, min 0.9999993
    20-60 s, tiled:                  min 0.9977
    2-3 s, repeat-filled:            mean 0.989, min 0.945

against a same-speaker mean of 0.846 and a different-speaker mean of 0.094 on the same
vectors — i.e. the backend divergence is two orders of magnitude smaller than the signal
it has to preserve. The thresholds below are set well inside those margins so the test
fails on a real regression (a changed export, a changed window, a changed mask) rather
than on float noise.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.models

SAMPLE_WAV = Path(__file__).resolve().parent.parent / "fixtures" / "media" / "sample_short.wav"
SAMPLE_RATE = 16_000


def _sidecar_url() -> str | None:
    """Resolve a diar-native URL this process can actually reach, or None.

    `DIAR_NATIVE_URL` names the compose hostname, which resolves inside the network but
    not from a host-side pytest run, so fall back to the running container's address.
    """
    from app.services.native_embedding_client import native_embedding_available

    candidates = [os.environ.get("DIAR_NATIVE_URL"), "http://localhost:8701"]
    try:
        ids = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv, no shell
            ["docker", "ps", "-q", "--filter", "label=com.docker.compose.service=diar-native"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout.split()
        for container_id in ids[:1]:
            addr = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv, no shell
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                    container_id,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            ).stdout.split()
            candidates.extend(f"http://{ip}:8701" for ip in addr if ip)
    except (OSError, subprocess.SubprocessError):
        pass

    for url in candidates:
        if url and native_embedding_available(url):
            return url
    return None


@pytest.fixture(scope="module")
def sidecar_url() -> str:
    url = _sidecar_url()
    if url is None:
        pytest.skip(
            "diar-native sidecar not reachable from this process. Start it with "
            "`./opentr.sh start dev --with-diar-native`, or set DIAR_NATIVE_URL."
        )
    assert url is not None  # pytest.skip above is NoReturn, but mypy cannot see that
    return url


@pytest.fixture(scope="module")
def in_process_service():
    """The legacy backend, forced on via the production escape hatch."""
    pytest.importorskip("pyannote.audio", reason="pyannote.audio not installed")
    from _pytest.monkeypatch import MonkeyPatch

    from app.services.speaker_embedding_service import SpeakerEmbeddingService

    mp = MonkeyPatch()
    mp.setenv("USE_NATIVE_SPEAKER_EMBEDDINGS", "false")
    try:
        service = SpeakerEmbeddingService(mode="v4")
    except (OSError, PermissionError) as exc:
        pytest.skip(f"pyannote embedding model unavailable: {exc}")
    finally:
        mp.undo()
    assert service.backend == "pyannote", (
        "USE_NATIVE_SPEAKER_EMBEDDINGS=false did not force the in-process backend; "
        "this test would otherwise compare the sidecar against itself and pass trivially"
    )
    return service


@pytest.fixture(scope="module")
def audio() -> np.ndarray:
    assert SAMPLE_WAV.exists(), f"fixture missing: {SAMPLE_WAV}"
    from app.services.audio_segment_utils import load_full_audio_np

    samples = load_full_audio_np(str(SAMPLE_WAV), SAMPLE_RATE)
    assert samples.size > 0
    return samples


def _native(samples: np.ndarray, url: str) -> np.ndarray:
    from app.services.native_embedding_client import embed_waveform

    out = embed_waveform(samples, base_url=url)
    assert out is not None, "the sidecar returned no embedding for a valid clip"
    return out


def _in_process(service, samples: np.ndarray) -> np.ndarray:
    import torch

    out: np.ndarray | None = service.extract_embedding_from_waveform(
        torch.from_numpy(np.ascontiguousarray(samples)).unsqueeze(0), SAMPLE_RATE
    )
    assert out is not None, "the in-process model returned no embedding for a valid clip"
    return out


def test_both_backends_produce_the_native_dimension(sidecar_url, in_process_service, audio) -> None:
    from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4

    clip = audio[: 10 * SAMPLE_RATE]
    native = _native(clip, sidecar_url)
    legacy = _in_process(in_process_service, clip)
    assert native.shape == (PYANNOTE_EMBEDDING_DIMENSION_V4,)
    assert legacy.shape == (PYANNOTE_EMBEDDING_DIMENSION_V4,)


def test_the_two_backends_agree_at_exactly_one_model_window(
    sidecar_url, in_process_service, audio
) -> None:
    """Same weights, same input, same answer — the claim the whole change rests on.

    Measured 0.9999997 mean / 0.9999993 min over 134 AMI windows; 0.999 is comfortably
    below that and far above anything a *different* model could reach (`pyannote/embedding`,
    the other embedding model in this codebase, is not even the same dimension).
    """
    from app.services.native_embedding_client import NATIVE_EMBEDDING_WINDOW_SAMPLES

    clip = audio[:NATIVE_EMBEDDING_WINDOW_SAMPLES]
    if clip.size < NATIVE_EMBEDDING_WINDOW_SAMPLES:
        pytest.skip(f"{SAMPLE_WAV.name} is shorter than one model window")

    cosine = float(np.dot(_native(clip, sidecar_url), _in_process(in_process_service, clip)))
    assert cosine > 0.999, (
        f"the sidecar and the in-process model disagree at exactly one model window "
        f"(cosine={cosine:.6f}, expected >0.999). They are supposed to be the same "
        "exported weights — a drop here means the ONNX export diverged from the "
        "checkpoint, and native and legacy voiceprints are no longer comparable."
    )


def test_the_backends_agree_on_a_sub_window_clip(sidecar_url, in_process_service, audio) -> None:
    """Short clips are repeat-filled to the model window; posting them raw scores ~0.

    This is the case the pre-#571 code got wrong. Measured 0.989 mean / 0.945 min at
    2-3 s; the raw-slice behaviour it replaced measured +0.007 to +0.081 over the same
    lengths, so the threshold discriminates the fix from the bug by a wide margin.
    """
    clip = audio[: int(2.5 * SAMPLE_RATE)]
    cosine = float(np.dot(_native(clip, sidecar_url), _in_process(in_process_service, clip)))
    assert cosine > 0.90, (
        f"sub-window agreement collapsed (cosine={cosine:.4f}). Anything near zero means "
        "the clip reached the sidecar shorter than its 160,000-sample window and was "
        "zero-padded, so the embedding is mostly silence."
    )


def test_the_backends_agree_on_a_clip_longer_than_one_window(
    sidecar_url, in_process_service, audio
) -> None:
    """Long clips are tiled and mean-pooled; the legacy path pools the whole signal once.

    Not identical by construction — measured min 0.9977 over 20-60 s AMI regions — but far
    inside the same-speaker distribution, which is what has to hold for the two backends'
    vectors to coexist in one index.
    """
    from app.services.native_embedding_client import NATIVE_EMBEDDING_WINDOW_SAMPLES

    if audio.size <= NATIVE_EMBEDDING_WINDOW_SAMPLES:
        # Build a longer signal from the fixture so this runs on the committed 10 s WAV.
        clip = np.concatenate([audio, audio[::-1], audio])
    else:
        clip = audio
    assert clip.size > NATIVE_EMBEDDING_WINDOW_SAMPLES

    cosine = float(np.dot(_native(clip, sidecar_url), _in_process(in_process_service, clip)))
    assert cosine > 0.95, (
        f"tiled agreement collapsed (cosine={cosine:.4f}); mean-pooled tiles no longer "
        "track whole-signal pooling."
    )


def test_the_service_uses_the_sidecar_when_it_is_available(sidecar_url, monkeypatch) -> None:
    """The default path: a v4 service with a live sidecar loads no in-process model.

    Without this, every assertion above could hold while production still paid the
    40-60 s model load and ~500 MB of VRAM this change exists to remove.
    """
    from app.services.speaker_embedding_service import SpeakerEmbeddingService

    monkeypatch.setenv("USE_NATIVE_SPEAKER_EMBEDDINGS", "true")
    monkeypatch.setattr(
        "app.services.native_embedding_client.native_embedding_available",
        lambda base_url=None: True,
    )
    service = SpeakerEmbeddingService(mode="v4")
    assert service.backend == "native"
    assert service.inference is None
    assert service.device is None
