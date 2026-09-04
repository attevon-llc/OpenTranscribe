"""The measurement issue #571 turns on: are the two embedding backends the same model?

`SpeakerEmbeddingService` can serve v4 (256-d) embeddings either by loading
`pyannote/wespeaker-voxceleb-resnet34-LM` in-process or by calling the diar-native
sidecar's `/embed_window`. The claim is that these are *the same weights*, re-exported
to ONNX — not "close enough". That claim is what licenses replacing the in-process load,
and what licenses leaving already-indexed voiceprints in place instead of re-embedding
them.

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

⚠️ Issue #669: this module used to compare the sidecar's output against a *live*
in-process PyAnnote load, gated behind ``pytest.importorskip("pyannote.audio")``. That
made the ONLY test backing the 0.9999997 claim silently skip the moment PyAnnote is
uninstalled — exactly when the claim needs defending, and exactly what would happen the
day PyAnnote removal (issue #572) actually lands.

The core equivalence assertions below instead compare the sidecar's live output against
a **frozen reference vector fixture**
(``tests/fixtures/embeddings/pyannote_v4_reference_vectors.json``), captured once from
the real in-process PyAnnote backend on 2026-09-04 (see that file's ``generation_note``)
and committed. No ``pyannote.audio`` import happens anywhere in this comparison, at
collection or run time. A separate, clearly-named test
(``test_in_process_pyannote_still_reproduces_the_frozen_vector``) keeps the in-process
model itself honest against the same fixture, and is the one test in this module that
still needs PyAnnote installed — its own skip only removes *that* check, not the
sidecar-vs-frozen-vector claim the rest of the module makes.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from tests.compose_project import compose_service_container

pytestmark = pytest.mark.models

SAMPLE_WAV = Path(__file__).resolve().parent.parent / "fixtures" / "media" / "sample_short.wav"
SAMPLE_RATE = 16_000

FROZEN_VECTORS_PATH = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "embeddings"
    / "pyannote_v4_reference_vectors.json"
)


def _load_frozen_vectors() -> dict[str, np.ndarray]:
    """Load the committed PyAnnote reference vectors, keyed by clip name.

    Raises (does not skip) if the fixture is missing — an absent frozen fixture is a
    broken test file, not an environment gap.
    """
    payload = json.loads(FROZEN_VECTORS_PATH.read_text())
    clips = payload["clips"]
    return {name: np.asarray(entry["vector"], dtype=np.float64) for name, entry in clips.items()}


FROZEN = _load_frozen_vectors()


def _sidecar_url() -> str | None:
    """Resolve a diar-native URL this process can actually reach, or None.

    `DIAR_NATIVE_URL` names the compose hostname, which resolves inside the network but
    not from a host-side pytest run, so fall back to the running container's address.
    """
    from app.services.native_embedding_client import native_embedding_available

    candidates = [os.environ.get("DIAR_NATIVE_URL"), "http://localhost:8701"]
    # Scoped to the project under test: the service label alone is not, and every
    # concurrently-running stack (dev + any `--fresh` deployment) carries a diar-native
    # container with that same label. See `tests/compose_project.py`.
    container = compose_service_container("diar-native")
    if container is not None:
        try:
            addr = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv, no shell
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                    container,
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
    """The legacy backend, forced on via the production escape hatch.

    Only used by the one test that explicitly re-validates the frozen fixture against a
    live PyAnnote load (`test_in_process_pyannote_still_reproduces_the_frozen_vector`).
    Every sidecar-vs-frozen-vector comparison in this module does not depend on this
    fixture and runs with PyAnnote absent.
    """
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


def test_both_backends_produce_the_native_dimension(sidecar_url, audio) -> None:
    from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4

    clip = audio[: 10 * SAMPLE_RATE]
    native = _native(clip, sidecar_url)
    assert native.shape == (PYANNOTE_EMBEDDING_DIMENSION_V4,)
    assert FROZEN["one_window"].shape == (PYANNOTE_EMBEDDING_DIMENSION_V4,)


def test_the_sidecar_agrees_with_the_frozen_vector_at_exactly_one_model_window(
    sidecar_url, audio
) -> None:
    """Same weights, same input, same answer — the claim the whole change rests on.

    Compares the LIVE sidecar output against a FROZEN PyAnnote vector (captured
    2026-09-04, see `pyannote_v4_reference_vectors.json`), so this assertion survives
    PyAnnote's absence entirely — no `pyannote.audio` import anywhere in this test.

    Measured 0.9999997 mean / 0.9999993 min over 134 AMI windows when both sides were
    live; 0.999 is comfortably below that and far above anything a *different* model
    could reach (`pyannote/embedding`, the other embedding model in this codebase, is not
    even the same dimension).
    """
    from app.services.native_embedding_client import NATIVE_EMBEDDING_WINDOW_SAMPLES

    clip = audio[:NATIVE_EMBEDDING_WINDOW_SAMPLES]
    if clip.size < NATIVE_EMBEDDING_WINDOW_SAMPLES:
        pytest.skip(f"{SAMPLE_WAV.name} is shorter than one model window")

    cosine = float(np.dot(_native(clip, sidecar_url), FROZEN["one_window"]))
    assert cosine > 0.999, (
        f"the sidecar disagrees with the frozen PyAnnote reference vector at exactly one "
        f"model window (cosine={cosine:.6f}, expected >0.999). They are supposed to be the "
        "same exported weights — a drop here means the ONNX export diverged from the "
        "checkpoint, and native and legacy voiceprints are no longer comparable."
    )


def test_the_sidecar_agrees_with_the_frozen_vector_on_a_sub_window_clip(sidecar_url, audio) -> None:
    """Short clips are repeat-filled to the model window; posting them raw scores ~0.

    This is the case the pre-#571 code got wrong. Measured 0.989 mean / 0.945 min at
    2-3 s when both sides were live; the raw-slice behaviour it replaced measured +0.007
    to +0.081 over the same lengths, so the threshold discriminates the fix from the bug
    by a wide margin. Compared against the frozen reference vector, not a live PyAnnote
    load.
    """
    clip = audio[: int(2.5 * SAMPLE_RATE)]
    cosine = float(np.dot(_native(clip, sidecar_url), FROZEN["sub_window_2_5s"]))
    assert cosine > 0.90, (
        f"sub-window agreement collapsed (cosine={cosine:.4f}). Anything near zero means "
        "the clip reached the sidecar shorter than its 160,000-sample window and was "
        "zero-padded, so the embedding is mostly silence."
    )


def test_the_sidecar_agrees_with_the_frozen_vector_on_a_clip_longer_than_one_window(
    sidecar_url, audio
) -> None:
    """Long clips are tiled and mean-pooled; the legacy path pools the whole signal once.

    Not identical by construction — measured min 0.9977 over 20-60 s AMI regions when
    both sides were live — but far inside the same-speaker distribution, which is what
    has to hold for the two backends' vectors to coexist in one index. Compared against
    the frozen reference vector, not a live PyAnnote load.
    """
    from app.services.native_embedding_client import NATIVE_EMBEDDING_WINDOW_SAMPLES

    if audio.size <= NATIVE_EMBEDDING_WINDOW_SAMPLES:
        # Build a longer signal from the fixture so this runs on the committed 10 s WAV.
        # Must match exactly how the frozen reference vector's clip was constructed
        # (see the fixture generation note) or the two sides are not comparable.
        clip = np.concatenate([audio, audio[::-1], audio])
    else:
        clip = audio
    assert clip.size > NATIVE_EMBEDDING_WINDOW_SAMPLES

    cosine = float(np.dot(_native(clip, sidecar_url), FROZEN["longer_than_window"]))
    assert cosine > 0.95, (
        f"tiled agreement collapsed (cosine={cosine:.4f}); mean-pooled tiles no longer "
        "track whole-signal pooling."
    )


def test_in_process_pyannote_still_reproduces_the_frozen_vector(in_process_service, audio) -> None:
    """PyAnnote-requiring: re-validates the frozen fixture itself, not the sidecar.

    This is the ONE test in this module that needs `pyannote.audio` installed, and it is
    named and scoped so its absence is legible: skipping it means "we didn't re-verify
    the frozen vector against a live PyAnnote load today", NOT "the native embedding
    equivalence claim is unverified" — the four tests above cover that claim with no
    PyAnnote dependency at all.
    """
    from app.services.native_embedding_client import NATIVE_EMBEDDING_WINDOW_SAMPLES

    clip = audio[:NATIVE_EMBEDDING_WINDOW_SAMPLES]
    if clip.size < NATIVE_EMBEDDING_WINDOW_SAMPLES:
        pytest.skip(f"{SAMPLE_WAV.name} is shorter than one model window")

    live = _in_process(in_process_service, clip)
    cosine = float(np.dot(live, FROZEN["one_window"]))
    assert cosine > 0.9999, (
        f"a fresh in-process PyAnnote load no longer reproduces the frozen reference "
        f"vector (cosine={cosine:.6f}, expected >0.9999). Either the checkpoint changed "
        "or the fixture needs regenerating — regenerate deliberately, not to silence this."
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
