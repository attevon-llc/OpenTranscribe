"""Benchmark the content-redaction detectors on a representative transcript.

Measures warm per-segment throughput of each detector (wordlist, Presidio+GLiNER PII,
toxicity) and projects total redaction time for each benchmark audio length. Run inside
the celery-redaction container (where the models are available)::

    docker exec opentranscribe-celery-redaction \
        python -m app.scripts.benchmark_redaction --segments 4200 --hours 4.7

If ``--from-db`` is passed it uses the longest completed transcript in the DB instead of
synthetic text (more representative of real PII/profanity density).
"""

from __future__ import annotations

import argparse
import time

# Representative sentence pool: mostly clean, with realistic PII + profanity so the
# detectors do real work (GLiNER/Presidio run on every segment regardless).
_POOL = [
    "So the quarterly numbers came in and they were better than we expected.",
    "My name is John Smith and you can reach me at john.smith@example.com.",
    "Honestly this whole situation is fucking ridiculous and I am done with it.",
    "Call me back at 555-123-4567 whenever you get a chance this afternoon.",
    "The team shipped the feature on time and the customers seem happy so far.",
    "Her social security number is 123-45-6789 which she should not have shared.",
    "Put the charge on card 4111 1111 1111 1111 and email the receipt over.",
    "We met at the office on Main Street near the old train station downtown.",
    "I think the design looks great but the loading time needs some work still.",
    "You are an idiot if you think that plan is going to work out for anyone.",
    "Let's circle back next week once the legal review has been completed fully.",
    "The weather was nice so we walked along the river before the meeting started.",
]


def _synth_segments(n: int) -> list[dict]:
    segs = []
    t = 0.0
    for i in range(n):
        text = _POOL[i % len(_POOL)]
        words = []
        cur = t
        for w in text.split():
            words.append({"word": w, "start": cur, "end": cur + 0.3, "score": 0.99})
            cur += 0.3
        segs.append({"text": text, "words": words, "start": t, "end": cur})
        t = cur + 0.2
    return segs


def _db_segments() -> tuple[list[dict], float]:
    from app.core.enums import FileStatus
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment

    with session_scope() as db:
        mf = (
            db.query(MediaFile)
            .filter(MediaFile.status == FileStatus.COMPLETED, MediaFile.duration.isnot(None))
            .order_by(MediaFile.duration.desc())
            .first()
        )
        if not mf:
            return [], 0.0
        rows = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == mf.id)
            .order_by(
                TranscriptSegment.start_time, TranscriptSegment.end_time, TranscriptSegment.id
            )
            .all()
        )
        segs = [{"text": r.text, "words": r.words} for r in rows]
        return segs, float(mf.duration or 0.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--segments", type=int, default=4200, help="synthetic segment count")
    ap.add_argument(
        "--hours", type=float, default=4.7, help="audio length these segments represent"
    )
    ap.add_argument("--from-db", action="store_true", help="use the longest completed transcript")
    args = ap.parse_args()

    from app.services.redaction.config import detection_config_for_all
    from app.services.redaction.detectors import pii_presidio
    from app.services.redaction.detectors import toxicity as tox
    from app.services.redaction.detectors import wordlist

    if args.from_db:
        segments, audio_s = _db_segments()
        if not segments:
            print("No completed transcript in DB; falling back to synthetic.")
            segments = _synth_segments(args.segments)
            audio_s = args.hours * 3600
    else:
        segments = _synth_segments(args.segments)
        audio_s = args.hours * 3600

    n = len(segments)
    cfg = detection_config_for_all()
    print(f"\nBenchmark: {n} segments (~{audio_s / 3600:.1f}h audio)\n" + "=" * 60)

    # Warm up the models (excluded from timing).
    print("Loading models (warm-up)...")
    pii_ok = pii_presidio.preload()
    tox_ok = tox.preload()
    print(f"  PII (Presidio+GLiNER): {pii_ok}   Toxicity: {tox_ok}")
    pii_presidio.detect_pii(segments[0]["text"], None, cfg)
    tox.score_texts([segments[0]["text"]])

    results = {}

    # Wordlist (read-time, cheap)
    t0 = time.perf_counter()
    for s in segments:
        wordlist.find_profanity_spans(s["text"], s.get("words"))
    results["wordlist"] = time.perf_counter() - t0

    # PII (Presidio + GLiNER) — per segment
    if pii_ok:
        t0 = time.perf_counter()
        for s in segments:
            pii_presidio.detect_pii(s["text"], s.get("words"), cfg)
        results["pii_presidio_gliner"] = time.perf_counter() - t0

    # Toxicity — batched
    if tox_ok:
        t0 = time.perf_counter()
        tox.score_texts([s["text"] for s in segments])
        results["toxicity_batched"] = time.perf_counter() - t0

    total = sum(results.values())
    print("\nDetector timings:")
    print(f"{'detector':<26}{'total_s':>10}{'ms/segment':>14}")
    for name, secs in results.items():
        print(f"{name:<26}{secs:>10.2f}{secs / n * 1000:>14.2f}")
    print("-" * 50)
    print(f"{'TOTAL':<26}{total:>10.2f}{total / n * 1000:>14.2f}")
    rtf = audio_s / total if total else 0
    print(f"\nRedaction realtime factor: {rtf:.1f}x  (audio_seconds / detect_seconds)")
    print(
        f"Added processing for ~{audio_s / 3600:.1f}h audio: {total:.1f}s ({total / 60:.1f} min)\n"
    )

    # Project to each benchmark file length using ms/segment * proportional segment count.
    per_seg = total / n
    print("Projected total redaction time per benchmark file (linear in segments):")
    for label, hrs in [("0.5h", 0.5), ("1.0h", 1.0), ("2.2h", 2.2), ("3.2h", 3.2), ("4.7h", 4.7)]:
        proj_segs = int(n * (hrs * 3600) / audio_s) if audio_s else n
        print(f"  {label:<6} ~{proj_segs:>5} segs  ->  {proj_segs * per_seg:>6.1f}s")


if __name__ == "__main__":
    main()
