"""Diagnose candidate speaker-boundary bleeds for a transcribed file (issue #193).

Without a hand-labeled reference, this flags the SAME signature the reporter screened for:
short (≤N word) speaker runs sandwiched between two longer runs of the SAME other speaker,
with no real pause at the seam — i.e. likely "wrong-speaker islands". It reconstructs the
word-level speaker stream from the DB (each word inherits its segment's speaker) and lists
the suspects with timestamps + text, so a human can jump straight to them when labeling.

Run in-container with DB access:
    docker compose exec -T backend python -m scripts.diagnose_boundaries --uuid <FILE_UUID>
"""

from __future__ import annotations

import argparse
import json
import logging

logging.basicConfig(level=logging.WARNING)


def _word_stream(db, file_id: int) -> list[dict]:
    """Reconstruct the ordered word stream with each word's segment speaker."""
    from app.models.media import Speaker
    from app.models.media import TranscriptSegment

    segments = (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(TranscriptSegment.start_time)
        .all()
    )
    spk_map = {
        s.id: (s.display_name or s.name)
        for s in db.query(Speaker.id, Speaker.name, Speaker.display_name).filter(
            Speaker.media_file_id == file_id
        )
    }
    words: list[dict] = []
    for seg in segments:
        spk = spk_map.get(seg.speaker_id, "UNKNOWN")
        for w in seg.words or []:
            if w.get("start") is None:
                continue
            words.append(
                {
                    "word": w.get("word", ""),
                    "start": float(w["start"]),
                    "end": float(w.get("end", w["start"])),
                    "speaker": spk,
                }
            )
    return words


def find_candidate_islands(
    words: list[dict], max_words: int = 3, min_flank: int = 3, max_gap: float = 0.4
) -> list[dict]:
    """Return short same-other-speaker-flanked islands (candidate bleeds)."""
    spk = [w["speaker"] for w in words]
    st = [w["start"] for w in words]
    en = [w["end"] for w in words]
    runs: list[tuple[int, int, str]] = []
    i = 0
    n = len(spk)
    while i < n:
        j = i
        while j < n and spk[j] == spk[i]:
            j += 1
        runs.append((i, j, spk[i]))
        i = j

    out: list[dict] = []
    for r in range(1, len(runs) - 1):
        s_i, e_i, isl = runs[r]
        left, right = runs[r - 1], runs[r + 1]
        if left[2] != right[2] or left[2] == isl:
            continue
        if (e_i - s_i) > max_words:
            continue
        if (left[1] - left[0]) < min_flank or (right[1] - right[0]) < min_flank:
            continue
        if (st[s_i] - en[left[1] - 1]) > max_gap or (st[right[0]] - en[e_i - 1]) > max_gap:
            continue
        out.append(
            {
                "start": round(st[s_i], 2),
                "end": round(en[e_i - 1], 2),
                "n_words": e_i - s_i,
                "island_speaker": isl,
                "flank_speaker": left[2],
                "text": " ".join(words[k]["word"].strip() for k in range(s_i, e_i)),
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uuid", help="MediaFile UUID")
    ap.add_argument("--file-id", type=int, help="MediaFile integer id")
    ap.add_argument("--max-words", type=int, default=3)
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    args = ap.parse_args()

    from app.db.session_utils import session_scope
    from app.models.media import MediaFile

    with session_scope() as db:
        if args.file_id:
            file_id = args.file_id
        else:
            mf = db.query(MediaFile).filter(MediaFile.uuid == args.uuid).first()
            if not mf:
                raise SystemExit(f"No file with uuid={args.uuid}")
            file_id = mf.id
        words = _word_stream(db, file_id)

    speakers = sorted({w["speaker"] for w in words})
    islands = find_candidate_islands(words, max_words=args.max_words)

    if args.json:
        print(
            json.dumps(
                {
                    "file_id": file_id,
                    "n_words": len(words),
                    "speakers": speakers,
                    "islands": islands,
                },
                indent=2,
            )
        )
        return

    print(f"file_id={file_id}  words={len(words)}  speakers={speakers}")
    print(f"candidate boundary-bleed islands (≤{args.max_words} words): {len(islands)}\n")
    by_len: dict[int, int] = {}
    for isl in islands:
        by_len[isl["n_words"]] = by_len.get(isl["n_words"], 0) + 1
    print("by length:", {k: by_len[k] for k in sorted(by_len)}, "\n")
    for isl in islands[:80]:
        mm, ss = divmod(int(isl["start"]), 60)
        print(
            f"  {mm:02d}:{ss:02d}  [{isl['n_words']}w]  {isl['flank_speaker']} → "
            f'({isl["island_speaker"]}) "{isl["text"]}"'
        )
    if len(islands) > 80:
        print(f"  ... and {len(islands) - 80} more")


if __name__ == "__main__":
    main()
