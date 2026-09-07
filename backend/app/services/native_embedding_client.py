"""Speaker embeddings from the diar-native sidecar (issue #571, Blocker 1).

``diar-server``'s ``/embed_window`` runs the **same weights** the legacy in-process
path loads as ``pyannote/wespeaker-voxceleb-resnet34-LM`` — diar-native re-exports
that model to ONNX from the same HF pipeline. Measured against 134 AMI
ground-truth single-speaker windows: cosine between the two paths on the same
audio is **0.9999997** (min 0.9999993), i.e. bit-equivalent within float32. So for
the v4 (256-dim) embedding mode this is a true replacement, not an approximation,
and the standalone PyAnnote embedding model does not need to be loaded at all.

⚠️ **The window length is load-bearing, and getting it wrong fails silently.**
The sidecar embeds a fixed 160,000-sample (10 s) window and applies an *all-ones*
frame mask across it (``diar-core``'s ``embed_window`` → ``MaskedEmbeddingInput``
with ``vec![1.0; 589]``). A clip shorter than that is **zero-padded to 10 s and the
padding is pooled with weight 1**, so the embedding is dominated by silence. It
still comes back a plausible-looking unit-norm 256-vector — nothing raises. Measured
agreement with the in-process model on identical clips:

    clip length   0.8s     1s      2s      3s      5s      8s     10s
    cosine       +0.012  +0.028  +0.007  +0.081  +0.239  +0.901  +1.000

and at 3 s the sidecar's speaker discrimination collapses to EER 39.6% against the
in-process model's 0.1% on the very same windows. Hence ``fit_to_window`` below:
every request carries exactly ``NATIVE_EMBEDDING_WINDOW_SAMPLES`` real samples.

Longer-than-window audio is tiled, and the tile embeddings are mean-pooled and
re-normalized. That is not identical to ``Inference(window="whole")``, which pools
over the whole signal in one pass, but the divergence is two orders of magnitude
smaller than the same-speaker/different-speaker gap it has to survive (measured
0.9977–0.9996 for 20–60 s clips, against a same-speaker mean of 0.85 and a
different-speaker mean of 0.09).

⚠️ **Device routing (issue #679).** Every call in this module is embedding-only — it never
runs a full diarization — which is the case measured safe for CPU routing.

⚠️ **"Bit-identical between devices" is FALSE. Do not restore that claim.** It was asserted
upstream (max centroid delta 0.0) and repeated in five places in this repo until it was
measured here against two real sidecars — one ``DIAR_MODE=cuda``, one ``DIAR_MODE=cpu``,
same binary digest, same ``/models`` export, same 10 s clip::

    CUDA vs CUDA (same sidecar, same input, twice) : max delta 2.86e-04
    CPU  vs CPU  (same sidecar, same input, twice) : max delta 0.0
    CUDA vs CPU                                    : max delta 4.11e-04
                                                     cosine   0.999999816

**CUDA is not deterministic with itself** — cuDNN picks algorithms whose reductions vary
run to run — so the cross-device gap is barely larger than CUDA's own variance, and
byte-equality was never achievable on ANY device pair, same-device included. CPU is the
bit-reproducible one.

What IS true, and what the routing actually rests on: the vectors are equivalent *for
speaker matching*, which compares by cosine. 0.99999982 sits far above the same-speaker
mean of 0.85 and the different-speaker mean of 0.09 quoted above, so a voiceprint embedded
on CPU matches identically to one embedded on CUDA.

**Never write a byte-equality assertion between two embeddings** — not across devices, and
not across two runs on the same CUDA device. Compare with a cosine threshold.

Full ``/diarize`` additionally shifts segment boundaries by up to one segmentation frame
(measured 0.016875 s on a 30 s clip) because a posterior on the binarisation threshold can
land either side; that is a separate, larger effect and is why `/diarize` is never routed
to CPU by anything in this codebase.
So `_embed_window` sends `"device": "cpu"` gated on
``diarizer_native.sidecar_supports_cpu_device()``, which reads `/healthz`'s **``devices``**
(the providers actually LOADED, chosen at start-up by ``DIAR_DEVICES``) — never
``supported_devices``, which is a build-time capability list. Keying on the capability made
the gate true on every GPU deployment, so `/embed_window` was sent ``device: "cpu"``,
answered ``400 device 'cpu' is not loaded; this server is serving [cuda]``, and every
embedding silently fell back to the in-process model on a sidecar reporting healthy.

It is also never sent unconditionally: the sidecar's request structs have no
`deny_unknown_fields`, so a pre-#679 sidecar silently ignores an unknown `device` key and
answers 200 on CUDA regardless — indistinguishable from success while still occupying the
GPU slot this exists to spare.

The cosine equivalence above is what makes this a win rather than a tradeoff: moving the
call off the sidecar's GPU frees that slot for the diarize jobs sharing it, at no cost to
speaker matching. It is not a *bit-identical* win — see the measurement above — but nothing
in this codebase compares embeddings by anything other than cosine.
"""

from __future__ import annotations

import logging

import numpy as np

from app.transcription.diarizer_native import post_json
from app.transcription.diarizer_native import sidecar_ready
from app.transcription.diarizer_native import sidecar_supports_cpu_device

logger = logging.getLogger(__name__)

# diar-core's EmbeddingMeta.window_samples — the ONNX graph's fixed input width.
NATIVE_EMBEDDING_WINDOW_SAMPLES = 160_000
NATIVE_EMBEDDING_SAMPLE_RATE = 16_000
NATIVE_EMBEDDING_DIMENSION = 256

# Upper bound on requests per extraction. Nothing in the pipeline caps segment
# duration (``select_top_segments`` actively returns the LONGEST merged sections,
# and ``extract_reference_embedding`` passes whole files), so an uncapped tiling
# would issue one ~80 ms request per 10 s of audio without limit. 60 tiles covers
# 10 minutes of speech before any subsampling begins, which is past every
# realistic merged section. Beyond it the tiles are spread evenly across the clip
# — a sample of the whole segment, never a truncation of its head.
NATIVE_EMBEDDING_MAX_TILES = 60

_EMBED_TIMEOUT_S = 60.0


def native_embedding_available(base_url: str | None = None) -> bool:
    """True when the sidecar can serve embeddings right now.

    Shares ``diarizer_native.sidecar_ready`` so the embedding path and the
    diarization path agree on what "the sidecar can serve" means — readiness, not mere
    liveness. ``/embed_window`` runs the same weights ``/diarize`` does, so a sidecar
    whose models are unusable cannot serve embeddings either, however cheerfully its
    ``/healthz`` answers 200.
    """
    return sidecar_ready(base_url)


def fit_to_window(samples: np.ndarray) -> list[np.ndarray]:
    """Split a waveform into windows of exactly the sidecar's model width.

    Pure function, no I/O — this is the part that has to be right, so it is
    testable without a sidecar.

    Args:
        samples: 16 kHz mono float32 waveform.

    Returns:
        A list of arrays, each exactly ``NATIVE_EMBEDDING_WINDOW_SAMPLES`` long.
        Empty when there is nothing to embed.

        - Shorter than one window: the clip is **repeated** until it fills the
          window. Repeating keeps every pooled frame real speech from the target
          speaker, where zero-padding (what the sidecar does on its own) pools
          silence. Measured cosine against the in-process model's whole-window
          embedding of the original short clip: 0.989 mean / 0.945 min at 2–3 s.
        - One window or longer: evenly spaced full windows covering the clip, so
          the final window is the clip's *last* 160,000 samples rather than a
          short, silence-padded tail.
    """
    width = NATIVE_EMBEDDING_WINDOW_SAMPLES
    flat = np.ascontiguousarray(np.asarray(samples, dtype=np.float32).reshape(-1))
    if flat.size == 0:
        return []

    if flat.size < width:
        repeats = int(np.ceil(width / flat.size))
        return [np.ascontiguousarray(np.tile(flat, repeats)[:width])]

    full_windows = flat.size // width
    count = min(max(full_windows, 1), NATIVE_EMBEDDING_MAX_TILES)
    starts = np.linspace(0, flat.size - width, count).astype(int)
    return [np.ascontiguousarray(flat[s : s + width]) for s in starts]


def _embed_window(window: np.ndarray, base_url: str) -> np.ndarray:
    """One sidecar round trip. ``window`` must already be the model width.

    Sends ``"device": "cpu"`` (issue #679) ONLY when the sidecar's own ``/healthz``
    advertises ``"cpu"`` in ``supported_devices`` — never blind. An older sidecar has no
    such field, ignores an unrecognised key, and answers 200 on CUDA regardless, so
    sending it unconditionally would look identical to success while still occupying the
    GPU slot this exists to spare. Embedding-only work (this whole module never runs a
    full diarization) is the case the issue names: a 256-d ONNX forward pass over one
    10 s window is cheap enough that moving it off the sidecar's GPU is very likely a net
    win when that GPU is also serving concurrent diarize jobs, but that has not been
    measured against a live sidecar in this environment (none was running) — this
    trades a plausible, reasoned win for headroom on the diarize GPU slot, not a proven
    speedup on the embedding call itself.
    """
    import base64

    payload: dict = {"samples_b64": base64.b64encode(window.astype("<f4").tobytes()).decode()}
    if sidecar_supports_cpu_device(base_url):
        payload["device"] = "cpu"
    out = post_json(f"{base_url}/embed_window", payload, timeout=_EMBED_TIMEOUT_S)
    return np.asarray(out["embedding"], dtype=np.float32).reshape(-1)


def embed_waveform(samples: np.ndarray, base_url: str | None = None) -> np.ndarray | None:
    """Embed a waveform of any length via the sidecar.

    Args:
        samples: 16 kHz mono float32 waveform.
        base_url: Sidecar base URL; defaults to ``DIAR_NATIVE_URL``.

    Returns:
        An L2-normalized 256-d embedding, or ``None`` if the sidecar could not
        serve it. ``None`` means "use the in-process model" to every caller — it
        never means "this audio has no speaker", so callers must not treat it as
        a result.
    """
    from app.transcription.diarizer_native import default_base_url

    windows = fit_to_window(samples)
    if not windows:
        return None

    url = (base_url or default_base_url()).rstrip("/")
    vectors: list[np.ndarray] = []
    try:
        for window in windows:
            vec = _embed_window(window, url)
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vectors.append(vec / norm)
    except Exception as exc:  # noqa: BLE001 — any sidecar loss degrades to the in-process model
        logger.warning("diar-native /embed_window failed (%s); caller falls back to PyAnnote", exc)
        return None

    if not vectors:
        return None

    pooled = np.mean(np.vstack(vectors), axis=0)
    norm = float(np.linalg.norm(pooled))
    if norm <= 0:
        return None
    normalized: np.ndarray = (pooled / norm).astype(np.float32)
    return normalized
