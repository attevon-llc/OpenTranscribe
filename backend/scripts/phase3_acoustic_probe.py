"""Phase-3 acoustic probe (issue #193): validate window embedding vs speaker centroids.

Confirms we can embed an arbitrary audio window with the diarization pipeline's WeSpeaker
model and that a window from speaker X's region matches X's centroid (cosine). This
determines the embedding-call format BEFORE wiring acoustic_recheck into the engine.
Run in the GPU worker container.
"""

from __future__ import annotations

import argparse
import collections

import numpy as np


def _l2(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n else v


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--min-speakers", type=int, default=2)
    ap.add_argument("--max-speakers", type=int, default=2)
    args = ap.parse_args()

    import torch

    from app.transcription.audio import load_audio
    from app.transcription.config import TranscriptionConfig
    from app.transcription.diarizer import SpeakerDiarizer

    audio = load_audio(args.audio)
    cfg = TranscriptionConfig.from_environment(
        min_speakers=args.min_speakers, max_speakers=args.max_speakers
    )
    diar = SpeakerDiarizer(cfg)
    diar.load_model()
    diarize_df, _overlap, centroids = diar.diarize(audio)
    if not centroids:
        print("FAIL: no speaker centroids produced")
        return
    labels = [lab for lab in centroids if centroids[lab] is not None]
    if len(labels) < 2:
        print("FAIL: <2 speaker centroids")
        return
    print("speakers:", labels, "centroid dim:", np.asarray(centroids[labels[0]]).shape)

    emb = diar._pipeline._embedding  # type: ignore[attr-defined]
    sr = int(getattr(emb, "sample_rate", 16000))
    dev = getattr(emb, "device", torch.device("cpu"))
    print("embedding sample_rate:", sr, "device:", dev)

    def embed(start: float, end: float):
        s, e = int(start * sr), int(end * sr)
        clip = np.ascontiguousarray(audio[s:e]).astype(np.float32)
        wf = torch.from_numpy(clip)
        last = ""
        for desc, batch in [("(1,1,N)", wf.view(1, 1, -1)), ("(1,N)", wf.view(1, -1))]:
            try:
                # Pass a CPU tensor — the fork's embedding wrapper pins + transfers
                # to its device internally (pinning requires a dense CPU tensor).
                out = emb(batch)
                arr = out.detach().cpu().numpy() if hasattr(out, "detach") else np.asarray(out)
                v = arr.reshape(-1)
                if v.size:
                    return v, desc
            except Exception as ex:  # noqa: BLE001
                last = f"{desc}: {type(ex).__name__}: {str(ex)[:70]}"
        print("  embed failed:", last)
        return None, None

    regions: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    for st, en, sp in zip(diarize_df.start, diarize_df.end, diarize_df.speaker, strict=True):
        if en - st >= 2.0:
            regions[str(sp)].append((float(st), float(en)))

    ok = 0
    for sp in labels:
        regs = regions.get(sp)
        if not regs:
            print(f"{sp}: no >=2s region")
            continue
        st, en = regs[len(regs) // 2]
        mid = (st + en) / 2.0
        v, fmt = embed(mid - 0.75, mid + 0.75)
        if v is None:
            continue
        sims = {lab: float(_l2(np.asarray(centroids[lab])) @ _l2(v)) for lab in labels}
        best = max(sims, key=lambda k: sims[k])
        verdict = "OK" if best == sp else "MISMATCH"
        if best == sp:
            ok += 1
        print(
            f"window {sp} [{st:.1f}-{en:.1f}] fmt={fmt} cos={ {k: round(x, 3) for k, x in sims.items()} } best={best} {verdict}"
        )
    print(
        f"\nPROBE RESULT: {ok}/{len(labels)} windows matched their own speaker — "
        f"{'embedding accessor WORKS, safe to wire Phase 3' if ok == len(labels) else 'NEEDS WORK'}"
    )


if __name__ == "__main__":
    main()
