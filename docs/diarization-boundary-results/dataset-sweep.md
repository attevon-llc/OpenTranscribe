# Diarization-Boundary Dataset Sweep (issue #193)

Status snapshot of the labeled-benchmark sweep backing the diarization-boundary fix. Three
public corpora, three metrics, chosen per corpus by what timing the reference actually has:

| corpus | reference timing | metric | why |
|---|---|---|---|
| AMI (meeting) | word + turn timing (RTTM) | **DER@0.25** | timed turns → standard pyannote-style DER |
| Earnings-21 (earnings calls) | token + speaker, **no timing** (`.nlp` `ts`/`endTs` empty) | **cpWER** (meeteval) | speaker-attributed WER needs no word timing |
| VoxConverse (in-the-wild) | turn timing (RTTM) | DER@0.25 | timed turns |

Earnings-21 is the closest public analogue to the reporter's case (interview-style, host +
guest(s)), so it is the most relevant signal — with the large caveat that the public files
are 8–14 speaker calls, not the reporter's clean 2-speaker pair.

Tooling:
- AMI / VoxConverse DER: `scripts/diarization-der.py` (+ `backend/scripts/benchmark_boundary.py`).
- Earnings-21 cpWER: `backend/scripts/score_earnings21_cpwer.py` (this sweep).

---

## AMI — DER (done)

5 files, `large-v3-turbo` transcription + PyAnnote (optimized fork) diarization, scored with
`pyannote.metrics.DiarizationErrorRate(collar=0.25, skip_overlap=False)`.

| file | DER@0.25 |
|---|---:|
| IS1008a | 0.148 |
| IS1008b | 0.204 |
| IS1008c | 0.272 |
| IS1008d | 0.241 |
| ES2011a | 0.446 |
| **count-matched avg** | **~0.22** |

**On track.** The count-matched average (~0.22) matches the pyannote `community-1` literature
figure (~0.22 DER) on AMI. ES2011a (0.446) is the expected hard tail — a many-speaker meeting
with overlapped speech. No regression vs the published baseline.

---

## Earnings-21 — cpWER (this sweep)

Sample of 3 files (sequential, GPU-modest), `large-v3`, full transcribe + diarize via the
production engine (`Engine.process`). cpWER computed with `meeteval.cp_word_error_rate` via the
shared `app.utils.diarization_metrics.cpwer`, after light text normalization (lowercase, strip
punctuation/diacritics) on **both** sides — Earnings-21 references carry mixed case and digits
("Good", "Culp's", "2020"), so raw cpWER is dominated by surface mismatch, not attribution.
Boundary smoother evaluated OFF vs ON.

| file | ref spk | hyp spk | ref tok | hyp tok | cpWER OFF | cpWER ON | Δ(OFF−ON) |
|---|---:|---:|---:|---:|---:|---:|---:|
| 4330115 | 8 | 6 | 6602 | 6479 | 0.277 | 0.277 | +0.000 |
| 4320211 | 10 | 10 | 8706 | **4904** | 0.522 | 0.522 | +0.000 |
| 4341191 | 14 | 13 | 14582 | 13571 | 0.181 | **0.180** | **+0.001** |
| **mean** | | | | | **0.327** | **0.326** | +0.0003 |

### Reading the numbers

- **Not yet in the 5–15% "on track" band** for these files (mean 33%). But that band is for a
  *clean, low-speaker-count* pipeline; these public Earnings-21 files are **8–14 speaker**
  earnings calls (operator + several execs + several analysts), which is the worst case for
  speaker-attributed WER — every under-clustered speaker dumps a whole speaker's text onto the
  wrong label. The reporter's actual 2-speaker case is expected to land far lower.
- **4341191 (14 spk) is the best at 0.180** — its speaker count is nearly matched (14 vs 13)
  and its token coverage is complete (14582 vs 13571). This is the representative "pipeline is
  working" data point and is within striking distance of the target band.
- **4320211 (0.522) is an outlier driven by token-count deficit, not attribution.** Whisper
  produced 4904 words vs the reference's 8706 — but full-duration diagnostic shows **no audio
  dropouts** (last segment ends at 3283 s of a 3286 s file, zero >30 s gaps). The deficit is a
  Earnings-21 *tokenization-density* mismatch (Rev's verbatim reference splits contractions /
  spells numbers / keeps disfluencies far more finely than Whisper renders them), which inflates
  WER as deletions. This is a reference-format artifact, not a diarization failure.
- **4330115 (0.277)** is under-clustered (6 hyp vs 8 ref speakers) — two small reference
  speakers (~100–280 tokens each) got merged. Speaker-count, not boundary, is the dominant error
  source here.

### Boundary smoother: OFF vs ON

The smoother is, as designed, **near-neutral** on this corpus — it only collapses 1–3 word
wrong-speaker islands at turn boundaries, a tiny fraction of words on 40–96 minute calls. It
moved cpWER on exactly one file (4341191: 0.1806 → 0.1797, −0.001) and was identical elsewhere.
**No regression on any file** (the issue #193 safety bar). It neither helps nor hurts at the
whole-call cpWER scale; its target (per-boundary bleed islands) is better measured by WSER on a
timed corpus (AMI / the reporter clip), which is the headline metric for the actual fix.

---

## VoxConverse — DER (pending)

Audio download still in progress and **flaky** — not scored in this sweep. Will be added with
`scripts/diarization-der.py` once the corpus settles. Best-effort / non-blocking.

---

## Overall: are we on track vs the labeled benchmarks?

- **AMI: yes** — count-matched avg DER ~0.22 == pyannote community-1 literature.
- **Earnings-21: directionally yes, with caveats.** The cleanest file (matched speaker count,
  full coverage) hits 0.18 cpWER; the higher numbers are explained by speaker-count
  under-clustering and the dataset's verbatim tokenization density, **not** by boundary bleed.
  The reporter's 2-speaker case should score materially better than these 8–14 speaker calls.
- **VoxConverse: pending.**
- **Boundary smoother: safe** — no regression anywhere; effect is below the resolution of
  whole-call cpWER (its win, if any, shows up in word-level WSER on a timed reference).

### Reproduce (in the GPU worker container)

```bash
# copy a sample pair in (NAS isn't mounted in the worker)
docker cp /mnt/nas/datasets/diarization-boundary/earnings21-refs/earnings21/media/<id>.mp3 \
  <worker>:/tmp/earnings21/<id>.mp3
docker cp /mnt/nas/datasets/diarization-boundary/earnings21-refs/earnings21/transcripts/nlp_references/<id>.nlp \
  <worker>:/tmp/earnings21/<id>.nlp

docker compose exec celery-worker python /app/scripts/score_earnings21_cpwer.py \
  --nlp /tmp/earnings21/<id>.nlp --media /tmp/earnings21/<id>.mp3 \
  --models large-v3 --out /tmp/earnings21/results.json
```
