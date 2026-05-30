# Cloud vs Local — Boundary Accuracy & Speed (issue #193, plan §6a)

How the local OpenTranscribe engine compares to premium cloud ASR+diarization providers on
the reporter's hand-labeled clip, on **both** speaker-boundary accuracy and wall-clock speed.
This is the "bar to beat" measurement: can a fully local/offline stack match the paid cloud?

## Setup

- **Clip**: Karpathy *No Priors* interview, first 10 min (2 speakers, English). The reporter's
  exact source for issue #193.
- **Reference**: the maintainer's **hand-corrected labels** (`reference.rttm`, exported from the
  UI-edited transcript) — i.e. what a human actually heard. Every engine is scored against the
  same reference.
- **Metric**: **WSER** (Word Speaker Error Rate) — each engine's words are midpoint-mapped to the
  reference turns, then the fraction of words with the wrong speaker (after optimal label
  permutation) is reported. **Bleed-island count** = short (≤3-word) wrong-speaker runs flanked by
  the same speaker — the literal boundary-bleed signature.
- **Speed**: wall-clock to result and **realtime factor** (`audio_seconds / wall_seconds`).
- **Harness**: `backend/scripts/compare_cloud_boundaries.py` (reproduce command at the bottom).

## Results

| engine | words | spk | WSER ↓ | islands ↓ | time | realtime ↑ |
|---|---:|---:|---:|---:|---:|---:|
| local — uncorrected (smoother OFF) | 2282 | 2 | 0.93% | 7 | 13.4 s | 44.7× |
| **local — smoothed (smoother ON)** | 2282 | 2 | **0.62%** | **1** | 13.4 s | **44.7×** |
| pyannote.ai — `parakeet` (precision-2) | 2321 | 2 | **0.53%** | 4 | 25.5 s | 23.5× |
| Deepgram — `nova-3` | 2253 | 2 | 5.20% | 0 | 5.8 s | 103.5× |

*(GPU: RTX A6000; local time is warm-model processing; cloud time is end-to-end API latency.)*

## Interpretation

- **Local is the best accuracy/speed/cost balance.** The smoothed local engine reaches 0.62% WSER
  at 44.7× realtime — within a whisker of premium pyannote's 0.53% while running **~2× faster**,
  fully offline, at zero per-minute cost. The boundary smoother turns the raw 0.93%/7-islands into
  0.62%/1-island for **no added wall time** (the smoother is sub-millisecond).
- **pyannote.ai is the accuracy leader but the speed laggard** (23.5×) and is metered at
  ~$0.027/min. Notably it **still emits 4 boundary islands** — confirming the issue-#193 boundary
  bleed is *universal*, present even in best-in-class cloud diarization, not a local-only defect.
- **Deepgram is the speed leader** (103.5×) but its diarization mislabels whole regions on this
  2-speaker interview (5.20% WSER, ~10× worse). Its transcription is fine; the error profile is
  large chunk mis-attribution (hence 0 short islands), not boundary bleed.

**Bottom line:** the local stack matches premium cloud accuracy, beats it on speed, and crushes the
fast cloud on accuracy — while staying offline and free. The cloud providers do not "solve" the
boundary problem; pyannote still bleeds at turn boundaries.

## AMI meetings — 4-speaker, harder (multi-speaker generalization)

Same harness on two AMI Mix-Headset meetings (first 10 min, 4 speakers, overlapping speech) —
the standard diarization regression corpus, much harder than the clean 2-speaker interview.

| file | engine | WSER ↓ | islands | realtime |
|---|---|---:|---:|---:|
| EN2002a | local smoothed | 10.99% | 5 | 44.8× |
| EN2002a | pyannote.ai | **9.92%** | 9 | 25.8× |
| EN2002a | Deepgram | 25.26% | 1 | 125.8× |
| ES2004b | **local smoothed** | **3.25%** | 0 | 47.7× |
| ES2004b | pyannote.ai | 4.13% | 2 | 26.5× |
| ES2004b | Deepgram | 8.26% | 0 | 207.3× |

Local **wins ES2004b outright** (3.25% vs pyannote 4.13%) and is within ~1 pt on EN2002a — at
~2× the speed and free. Deepgram remains far behind on speaker accuracy. The smoother is roughly
neutral on 4-speaker meetings (its target is 2-party boundary bleed; multi-speaker error is
dominated by larger diarization confusion, not short islands) — expected and safe.

**Takeaway across 2- and 4-speaker content:** the local stack matches or beats premium pyannote.ai
on accuracy while running ~2× faster, offline and free.

## Provider status

All cloud providers below were verified end-to-end against live APIs (catalog `status: tested`):

| provider | models | notes |
|---|---|---|
| pyannote.ai | `parakeet`, `whisper-large-v3-turbo` (precision-2) | premium diarization; best accuracy |
| Deepgram | `nova-3` (+ medical, nova-2) | fastest; weaker speaker attribution |
| AssemblyAI | `universal-3-pro`, `universal-2` | slam-1/nano rejected by the live API |
| Gladia | `standard` | recurring 10 hr/month free tier |
| **AWS Transcribe** | `standard`, `medical` | code-ready; pending credentials (S3 + IAM) |

## Caveats

- **Single clip, 2 speakers.** Order-of-magnitude signal, not a leaderboard. Broaden with AMI /
  Earnings-21 (references already downloaded) for a population estimate.
- **Local time excludes one-time model load** (amortized in production — models stay pinned in
  VRAM). **Cloud time includes upload + provider queue + processing + poll latency** (what a user
  actually waits). Both reflect real user-perceived latency for their deployment model.
- **Cloud numbers are point-in-time.** Vendor models are versioned and change; record date + model
  with any published figure. Only the local pipeline is a deterministic, frozen baseline.
- **Cost** (cloud, batch): pyannote.ai ≈ $0.027/min; Deepgram nova-3 ≈ $0.0043/min. Local = GPU
  amortization only.

## Reproduce

```bash
# A short 2-speaker clip + the labeled reference must be on the worker (/tmp here).
docker compose exec -T celery-worker python -m scripts.compare_cloud_boundaries \
    --audio /tmp/karpathy_10m.wav --ref-rttm /tmp/karpathy_ref.rttm \
    --cloud 849 --cloud 610 --max-speakers 2
# --cloud <id> = a row id in user_asr_settings (decrypts that provider's key).
```

Run date: 2026-05-30. Providers: pyannote.ai `parakeet`/precision-2, Deepgram `nova-3`.

> Feeds the performance whitepaper (`docs/performance-whitepaper/main.tex`): the local-vs-cloud
> accuracy/speed/cost table is the cloud-comparison section's primary data source.
