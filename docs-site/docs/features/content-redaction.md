---
sidebar_position: 7
---

# Content Redaction (PII, Profanity & Toxicity)

OpenTranscribe can automatically detect and mask **personally identifiable information
(PII)**, **profanity**, and **toxic language** in your transcripts — similar to the
moderation features in commercial services like Amazon Transcribe.

A core principle: **the full original transcript is always kept in the database.**
Redaction is a *read-time* transform — masking is applied when the transcript is viewed
or exported, never by destroying data. An authorized owner can always toggle back to the
original (unless an administrator has forced a category for compliance).

Redaction is **opt-in (off by default)** — it adds a moderation scan after transcription
and delays the transcript display until the scan completes, so each user enables it
explicitly in **Settings → Content Redaction** (administrators can force it for everyone).
Detection only runs for users who have it enabled.

## How it works

1. **Detect once, cache forever.** When redaction is enabled and a transcription
   completes, a background job on a dedicated CPU service scans every segment once and
   stores *redaction spans* (which characters to mask, and why) on the transcript. Once
   cached, changing categories or switching the mask style is **instant and never
   re-scans**. (If you enable redaction later, the scan runs the first time you open the
   file.)
2. **Mask at read time.** Whenever the transcript is shown (UI), exported (SRT/VTT/TXT/…),
   surfaced in search snippets, or sent to an AI summary, the cached spans are applied
   with your chosen style. This is microsecond-fast and requires no models.
3. **Owner reveal.** The file owner (and admins) can flip **"Show original"** to view the
   un-redacted transcript. Categories an administrator has *forced* stay masked even then.

**Display while scanning.** When redaction is enabled, the transcript is **withheld until
the scan finishes** so there is never an un-redacted display window. The file page shows a
live **"Redacting…" status chip** and, the moment detection completes, a WebSocket event
**auto-loads the redacted transcript** — no refresh needed (same pattern as speaker/gender
enrichment). Detection is fast (see Performance), so this is typically seconds.

## The detectors & models

Redaction combines **rule-based** detection (instant, deterministic) with **lightweight
ML** (for things rules can't catch, like names). You can enable/disable each detector in
**Settings → Content Redaction**.

| Detector | Method | Model / technique | Speed (CPU) | Notes |
|---|---|---|---|---|
| **Profanity** | Wordlist + regex | Curated English list, word-boundary matched | ~0.06 ms/seg | Custom words & allowlist are per-user, applied at read time |
| **Structured PII** | Regex | [Microsoft Presidio](https://github.com/microsoft/presidio) pattern recognizers (email, phone, SSN, credit-card, IBAN, IP) | sub-ms | Deterministic; credit cards validated with the Luhn check |
| **Names / orgs / locations** | NER | [spaCy](https://spacy.io) `en_core_web_sm` (default) | ~8 ms/seg | Fast, light, bundled with Presidio |
| **Names (enhanced, opt-in)** | Zero-shot NER | [GLiNER](https://github.com/urchade/GLiNER) — [`knowledgator/gliner-pii-base-v1.0`](https://huggingface.co/knowledgator/gliner-pii-base-v1.0) | ~130 ms/seg (CPU) / ~5–15 ms (GPU) | Higher accuracy on diverse/non-Western names; off by default for speed |
| **Toxicity** | Text classification | [`unitary/toxic-bert`](https://huggingface.co/unitary/toxic-bert) (English), [`unitary/multilingual-toxic-xlm-roberta`](https://huggingface.co/unitary/multilingual-toxic-xlm-roberta) (multilingual) | ~8 ms/seg (batched) | Segment-level flag (toxic / obscene / threat / insult / identity-hate) |
| **LLM (optional)** | Generative | Your configured `LLM_PROVIDER` | provider-dependent | Catches context-dependent cases; off by default |

### Are these the best / lightest / most accurate models?

Short answer: for a **self-hosted, CPU-friendly, privacy-preserving** transcription app,
this stack is a strong, well-justified choice. The honest trade-offs:

- **Structured PII (regex via Presidio)** — for emails, phone numbers, SSNs, credit cards,
  IBANs and IPs, rule-based regex is *both* the lightest *and* the most reliable approach.
  ML adds nothing here; these formats are well-defined. This is also how commercial
  services implement them under the hood. Presidio is the de-facto open-source standard
  and is actively maintained by Microsoft.
- **Names (spaCy `en_core_web_sm`)** — the lightest credible option (~12 MB, ~8 ms/seg).
  It's not the most *accurate* NER available, but for English it's a sensible default.
  When you need better recall on diverse names, enable **GLiNER** — a 2024 zero-shot NER
  model that matches or beats much larger LLMs on NER while running on CPU
  ([paper](https://arxiv.org/abs/2311.08526)). We default it **off** purely for speed
  (it's ~16× heavier per segment); it's one toggle away.
- **Toxicity (`unitary/toxic-bert`)** — the most widely-used open toxicity classifier
  (trained on the Jigsaw Toxic Comment dataset), standard architecture, loads natively,
  multi-label. Lighter alternatives exist (e.g. the 2 M-param `Tiny-Toxic-Detector`) but
  they use custom architectures that require executing remote code — which we avoid for
  security. `toxic-bert` is the best balance of accuracy, breadth, and a clean security
  posture. References: [Detoxify](https://github.com/unitaryai/detoxify),
  [Jigsaw dataset](https://www.kaggle.com/c/jigsaw-toxic-comment-classification-challenge).

In other words: **rules where rules win (structured PII, profanity), small models where
you need them (names, toxicity), and a heavier model only when you opt in (GLiNER / LLM).**

## Performance

Measured on a 2× RTX A6000 host, **CPU-only**, ~12-word segments (representative of
diarized speech). Redaction runs in the background on a dedicated, horizontally-scalable
CPU service, so it never blocks transcription.

| Configuration | Per segment | Realtime factor | 4.7-hour file |
|---|---|---|---|
| **Fast default** (regex PII + spaCy NER + batched toxicity) | **16 ms** | **259×** | **~65 s** |
| Enhanced (GLiNER names enabled, CPU) | 145 ms | 29× | ~10 min |

So a nearly **5-hour** recording is fully scanned in about **a minute** with the default
configuration. Enabling GLiNER trades speed for higher name-recall; running it on GPU
(`REDACTION_DEVICE=cuda`) brings it back down to a few milliseconds per segment.

You can reproduce these numbers:

```bash
docker exec opentranscribe-celery-redaction \
    python -m app.scripts.benchmark_redaction --segments 500 --hours 0.58
```

### CPU load & threading

Redaction is multi-threaded at two levels:

- The worker runs `--pool=threads --concurrency=4` (env `REDACTION_CONCURRENCY`), so up to
  **4 files redact concurrently**, sharing one in-memory copy of the models.
- Each ML inference (toxicity, and GLiNER if enabled) uses PyTorch intra-op parallelism —
  on a 48-core host it fans out across **~20–24 cores per inference**. Regex, profanity
  and spaCy are much lighter.

Observed container CPU during a scan (48-core host):

| Configuration | CPU usage | Pattern | Memory |
|---|---|---|---|
| Fast default | bursts to ~2100% (~21 cores) on toxicity, ~600% on spaCy, idle between | spiky, short | ~1.2 GB |
| GLiNER-on (CPU) | sustained ~2000–2200% (~20–22 cores) for the whole PII pass | sustained, long | ~2.0 GB |

On busy hosts you can cap PyTorch threads (`OMP_NUM_THREADS`) and/or lower
`REDACTION_CONCURRENCY` to bound contention; the work simply takes proportionally longer.

### GPU acceleration (automatic)

GPU use is **automatic and dynamic** (`REDACTION_DEVICE=auto`, the default): the
`celery-redaction` container is connected to a GPU, and **before each scan** it checks
free VRAM. If the GPU has room (≥ `REDACTION_MIN_FREE_VRAM_GB`, default 1.5 GB) the
toxicity model runs on GPU; if the GPU is busy with transcription/diarization it
**transparently falls back to CPU** — no restart, no manual toggle. The model moves
GPU↔CPU as availability changes, and GPU inference is **serialized** (one at a time) so
peak VRAM stays bounded even under concurrency. Point it at a spare GPU with
`REDACTION_GPU_DEVICE_ID`, or force a mode with `REDACTION_DEVICE=cpu|cuda`.

Expected effect when on GPU:

- **GLiNER:** ~130 ms/seg (CPU) → **~5–15 ms/seg** on GPU — the big win, and it frees the
  ~20 CPU cores it otherwise pegs. This is the recommended way to run GLiNER.
- **Toxicity (toxic-bert):** ~8 ms/seg → sub-millisecond batched on GPU.
- **spaCy NER / regex / profanity:** little or no GPU benefit (CPU-bound by design).

The models are small (~1 GB combined), so they fit alongside transcription on a shared GPU.
For the **fast default**, GPU is only a modest gain; GPU mainly matters when you enable GLiNER.

**VRAM safety on a shared GPU.** When on GPU, redaction inference is **serialized** (one
file at a time) so peak VRAM stays bounded to a single inference even under heavy
concurrency. Before placing models on GPU, the worker checks free VRAM
(`REDACTION_MIN_FREE_VRAM_GB`, default 1.5) and **automatically falls back to CPU** if the
GPU is busy with transcription/diarization — so a single-GPU homelab degrades gracefully
instead of OOMing. Point redaction at a spare GPU with `REDACTION_GPU_DEVICE_ID`.

### Fast vs. enhanced name detection (GLiNER)

Both modes detect the same **structured PII** (email, phone, SSN, credit-card, …) via
regex — that part is always exact. They differ only in **name/org/location** detection:

- **Fast (default):** spaCy `en_core_web_sm` NER. Good recall on common English names,
  ~8 ms/segment. This is what GLiNER does *not* add for typical English content.
- **GLiNER (admin opt-in, Settings → Redaction Policy):** a zero-shot model with notably
  better recall on **diverse / non-Western / uncommon names**, and it also detects
  **addresses** as spans. The cost is ~9× slower on CPU (use a GPU). Toggle it in the
  admin Redaction Policy; changing it affects new transcripts, and **Re-scan all files**
  applies it to existing ones.

### Single machine vs. distributed deployment

Redaction is a **background post-step** — the user already has their transcript, so it is
intentionally a "polite" CPU citizen and never blocks user-facing work.

**Single machine (everything on one box).** Transcription (CPU preprocess + GPU), the
embedding/NLP/cleanup workers, and redaction all share the same cores. To prevent
thrashing, the `celery-redaction` service ships with conservative defaults:

- Runs at **lower OS priority** (`nice -n 10`) — it yields CPU to transcription and the API
  under contention, and only uses spare capacity.
- **Capped PyTorch threads** (`REDACTION_TORCH_THREADS=8`) so one inference can't grab all cores.
- **Low concurrency** (`REDACTION_CONCURRENCY=2`).
- Because detection is **once-per-file and cached**, the load is a brief one-time burst per
  upload, not a continuous drain.

**Distributed (e.g., AWS/K8s).** Run `celery-redaction` as its **own service/node** with no
transcription on it — then there's no contention. Raise `REDACTION_TORCH_THREADS`,
`REDACTION_CONCURRENCY`, and/or add replicas for maximum throughput, and optionally attach
a GPU with `REDACTION_DEVICE=cuda`. The queue-based design means scaling out is purely an
infrastructure change — no code changes.

Tuning knobs (env): `REDACTION_NICE`, `REDACTION_CONCURRENCY`, `REDACTION_TORCH_THREADS`,
`REDACTION_DEVICE`, `REDACTION_PII_USE_GLINER`.

## Language support

Detection runs in the transcript's language. Coverage today:

- **Profanity & PII (names):** English.
- **Structured PII (email/phone/SSN/credit-card/…):** language-independent (regex).
- **Toxicity:** English (`toxic-bert`) plus `es, fr, it, pt, tr, ru` via the multilingual model.

If a transcript's language **isn't supported** by a detector, that detector is **skipped**
for the file and you're **notified** — nothing is silently mis-scanned. The supported
languages are shown in **Settings → Content Redaction**.

## Settings

### Per-user (Settings → Content Redaction)

Off by default (opt-in). Each user controls, for their **own** uploads:

- Enable/disable redaction and individual detectors
- Which categories to redact (profanity, PII, toxicity, custom)
- Which PII entity types to mask
- **Mask style:** `[CATEGORY]` label (default), asterisks `****`, first-letter `f***`, or blurred (hover to reveal)
- Custom words to redact + an allowlist (never redact)
- Toxicity sensitivity threshold
- Whether to mask before sending to AI/LLM features
- Whether exports are censored by default

### Admin governance (Settings → Redaction Policy)

Administrators can **force** redaction for everyone (compliance floor). Forced categories
cannot be disabled by individual users — they may only add *more* redaction. Admins can:

- Force PII / toxicity / profanity redaction org-wide (individually)
- Mandate censored exports (no user can export the original)
- Mandate masking before external AI/LLM providers
- Re-scan all existing files (e.g., after a model upgrade)

## Privacy & security

- The original transcript is **never modified** — masking is read-time only.
- Redaction is **server-enforced**: the API returns masked text by default; revealing the
  original requires being the file owner (or an admin) and is **written to the audit log**.
- Non-owners with shared access can never reveal originals; admin-forced categories are
  non-overridable.
- Local models keep data on your infrastructure. The optional LLM detector and
  "mask before LLM" setting let you control what (if anything) reaches an external provider.
- **Disclaimer:** like all automated detection, redaction is not guaranteed to catch every
  instance of sensitive data. Review redacted output before sharing externally. It does
  not by itself satisfy de-identification requirements under laws such as HIPAA.

## References

- [Microsoft Presidio — PII detection & anonymization](https://github.com/microsoft/presidio)
- [GLiNER (zero-shot NER)](https://github.com/urchade/GLiNER) · [paper](https://arxiv.org/abs/2311.08526) · [model](https://huggingface.co/knowledgator/gliner-pii-base-v1.0)
- [spaCy NER](https://spacy.io/usage/linguistic-features#named-entities)
- [Detoxify](https://github.com/unitaryai/detoxify) · [toxic-bert](https://huggingface.co/unitary/toxic-bert)
- Amazon Transcribe: [toxicity](https://docs.aws.amazon.com/transcribe/latest/dg/toxicity.html) · [PII redaction](https://docs.aws.amazon.com/transcribe/latest/dg/pii-redaction.html)
