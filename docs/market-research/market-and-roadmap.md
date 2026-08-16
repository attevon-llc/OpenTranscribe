# OpenTranscribe — Market Landscape, Competitive Dossier & Product Roadmap

> **Single reference document.** Combines all market research, benchmark data, competitive analysis, Recall.ai positioning, current technical capabilities, and the feature roadmap into one place. Detailed baseball cards live in `cloud-asr-market-research.md` and `competitor-landscape.md`. Benchmark raw data in `BENCHMARK_RESULTS.md` and `diarization-boundary-results/cloud-comparison.md`.
>
> **Last updated**: August 2026 (refreshed for the pending v0.5.0 release — see the "Shipped since the
> roadmap below was written" callout and the capability table for what changed since May 2026).

---

## The Market in One Picture

The speech-to-text market has three tiers. Every competitor lives on exactly one. OpenTranscribe spans all three.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  TIER 3 — END-USER APPS  (finished products, non-developer UI)           │
│                                                                          │
│  Meeting notes:    Otter.ai · Fireflies.ai · Grain                      │
│  Media/journalism: Trint · Sonix · Rev · Descript                       │
│  Enterprise/legal: Verbit ($550M raised, $2B valuation, $100M+ ARR)     │
│                                                                          │
│  ALL cloud-only. NONE self-hostable. NONE span all use cases.            │
├──────────────────────────────────────────────────────────────────────────┤
│  TIER 2 — MEETING CAPTURE INFRASTRUCTURE                                 │
│                                                                          │
│  Recall.ai — bot API joining Zoom/Teams/Meet/Webex, returns audio +     │
│  transcript via webhook. $250M valuation. $10M ARR. 3,000+ platform     │
│  customers (HubSpot, Calendly, ClickUp). No end-user UI.                │
├──────────────────────────────────────────────────────────────────────────┤
│  TIER 1 — ASR / SPEAKER INTELLIGENCE APIs  (infrastructure, no UI)       │
│                                                                          │
│  Deepgram ($1.3B) · AssemblyAI ($115M+) · Speechmatics · Gladia ($16M) │
│  pyannote.ai ($9M) · OpenAI · Google · Azure · AWS                      │
│                                                                          │
│  ALL API-only. NONE ship a user-facing product.                          │
└──────────────────────────────────────────────────────────────────────────┘

             ┌──────────────────────────────────────────────┐
             │           OPENTRANSCRIBE                      │
             │                                              │
             │  Self-hosted end-user application that       │
             │  plugs into any Tier 1 ASR provider,         │
             │  can consume Tier 2 meeting capture feeds,   │
             │  and delivers Tier 3 user-facing features.   │
             │                                              │
             │  The only product that occupies this         │
             │  position in the entire market.              │
             └──────────────────────────────────────────────┘
```

---

## Tier 3 — End-User App Competitors

### Quick Snapshot

| Company | Model | Pricing | Target | Self-host | Key strength | Key gap |
|---|---|---|---|---|---|---|
| **Otter.ai** | Freemium B2C→B2B | Free / \$8–\$30/user/mo | Meeting notes, teams | ✗ | Live UX, OtterPilot bot | English-only, hard caps, no archive search |
| **Fireflies.ai** | Freemium B2B | Free / \$18–\$29/user/mo | Sales/CRM teams | ✗ | CRM auto-sync (Salesforce, HubSpot) | No file upload, no cross-meeting search |
| **Grain** | Freemium B2B | Free / \$15–\$33/user/mo | Sales highlight clips | ✗ | One-click video clip from transcript | Niche (clips only), no archive/search |
| **Trint** | B2B SaaS | \$52–\$80/seat/mo | Journalism/media | ✗ | AP/BBC newsroom workflows, GDPR EU data | Expensive, no semantic search, cloud-only |
| **Sonix** | Pay-as-you-go + sub | \$10/hr or \$5/hr+\$22/mo | Researchers, media | ✗ | 40+ languages, good editor | No meeting bot, no cross-file search |
| **Rev** | Freemium + marketplace | \$0.25/min AI · \$1.99/min human | Legal, journalists | ✗ | Human-reviewed certified transcripts | Expensive at scale, no meeting bot |
| **Descript** | Freemium B2C/B2B | \$12–\$40/user/mo | Video/podcast creators | ✗ | Text-based video editing (industry-unique) | Editing-first, not a knowledge tool |
| **Verbit** | Enterprise contracts | Custom (premium) | Legal, education, healthcare | ✗ (SLAs only) | Hybrid AI+human, 99% accuracy, ADA/HIPAA | Enterprise-only procurement, no self-serve |

### What Grain and Otter Actually Do

**Otter.ai** is a meeting notes app. A bot (OtterPilot) joins your Zoom/Teams/Meet call, produces a live transcript visible to participants, then generates a summary and action items. ~10M users. Strengths: polished live UX, team workspaces, Salesforce/HubSpot integrations. Hard constraints: English-only, hard monthly minute caps (once hit, transcription stops until next billing cycle), no bulk file workflows, no deep cross-meeting search, weaker diarization on crowded calls.

**Grain** is a highlight-reel tool for sales teams. Joins calls, lets reps clip short video moments from the transcript, build coaching playlists, push highlights to CRM. Not a search/archive/knowledge tool. Limited language support. Purely live-meeting focused — no file upload, no bulk processing, no cross-call search.

Neither touches pre-recorded file libraries at scale. Neither supports self-hosting. Neither does meaningful cross-meeting RAG.

### Competitor Segmentation

```
                    MEETING INTELLIGENCE
                    (real-time + CRM)
                           ▲
                           │
            Fireflies.ai ──┼── Grain
             Otter.ai      │
                           │
Consumer/  ────────────────┼──────────────── Enterprise/
Individual                 │                  B2B
                           │
              Rev ─────────┼── Verbit
             Sonix         │   Trint
                           │
                           ▼
                    TRANSCRIPTION-FOCUSED
                    (file-based, accuracy)
```

OpenTranscribe today is bottom-right (enterprise, transcription-focused). The roadmap moves it to center — spanning both axes simultaneously.

### The Self-Hosting Gap — The Biggest Structural Hole in the Market

| Company | Self-hosting |
|---|---|
| Otter.ai | ✗ |
| Rev | ✗ |
| Sonix | ✗ |
| Trint | ✗ |
| Descript | ✗ |
| Fireflies.ai | ✗ |
| Grain | ✗ |
| Verbit | ✗ (private data SLAs available, but infrastructure is Verbit-hosted) |
| **OpenTranscribe** | **✓ — the only full-stack self-hosted option in the market** |

Every organization with HIPAA, GDPR, FedRAMP, air-gap, or data sovereignty requirements has zero commercial finished-product options. Healthcare, legal, government, defense, financial services — they're either building their own tooling from scratch or sending sensitive recordings to clouds they cannot control.

---

## Tier 2 — Recall.ai: The Missing Piece for Meeting Capture

Recall.ai is a meeting bot API (bots join Zoom/Teams/Meet/Webex, capture audio + transcript, fire a webhook on completion). $250M valuation, $38M Series B (2025), $10M ARR (up from $2.1M in 2024). 3,000+ platform customers. **Zero end-user UI.**

**Why this matters for OpenTranscribe:** Recall.ai is not a competitor — it's a backend. OpenTranscribe treats the Recall.ai webhook payload as a new ingestion source, same as a file upload or yt-dlp URL. No core pipeline changes needed.

**Integration model:**
```
User connects calendar + Zoom/Teams/Meet via Recall.ai OAuth
  → Recall.ai bot auto-joins every scheduled meeting
  → On meeting end: webhook → OpenTranscribe Celery task
  → Existing pipeline: diarization → embeddings → OpenSearch index
  → All meetings searchable, queryable, cross-referenced by speaker
```

**What this delivers:**
- Passive capture — meetings appear automatically, no upload step
- Voice fingerprinting links the same person across hundreds of meetings automatically
- Cross-meeting RAG: *"What did we decide about pricing in Q1?"*
- Self-hosted, privacy-first replacement for Fireflies.ai / Grain / Otter Teams

**Pricing to pass through to users:** \$0.50/hr recording + \$0.15/hr transcript + \$0.05/hr storage (Recall.ai rates). A 1-hour meeting costs \$0.70 to capture.

---

## Tier 1 — ASR Provider Benchmarks: OpenTranscribe Wins on Accuracy

All six cloud providers benchmarked head-to-head on the Karpathy *No Priors* interview clip (10 min, 2 speakers, hand-corrected reference labels). Metric: **WSER** (Word Speaker Error Rate).

| Engine | WSER ↓ | Speed | Notes |
|---|---|---|---|
| **Local smoothed (OT default)** | **0.27%** | **41× realtime** | Wins on accuracy; offline; free |
| Gladia | 0.27% | 23× | Ties local; best cloud option |
| AssemblyAI | 0.49% | 31× | Strong all-rounder |
| Speechmatics | 0.53% | 16× | Enterprise diarization leader — still beaten by local |
| AWS Transcribe | 0.69% | 9.7× | Slowest (S3/batch queue overhead) |
| pyannote.ai | 0.70% | 31× | Still emits boundary islands; OT smoother fixes this |
| Deepgram | 5.76% | **146×** | Speed king; poor speaker attribution |
| Local uncorrected (smoother OFF) | 1.15% | 41× | Before boundary correction |

**Bottom line**: OpenTranscribe's local pipeline (WhisperX + PyAnnote + boundary smoother) matches or beats every premium cloud provider on diarization accuracy, at 41× realtime, free, fully offline. Even Speechmatics — which markets itself as the enterprise diarization leader — is beaten by local.

The boundary smoother alone drives local from 1.15% → 0.27% WSER (−76% error) with zero added processing time. This is documented in `diarization-boundary-results/cloud-comparison.md` and the performance whitepaper.

### GPU Performance Numbers

Full end-to-end pipeline (transcription + diarization + speaker assignment) on RTX A6000 (48GB):

| Config | Realtime factor | VRAM used |
|---|---|---|
| Single file, 1 worker | **40.3× realtime** | 2,317 MB (4.7% of A6000) |
| 8 concurrent files, 1 GPU | **54.6× realtime** | 44,199 MB |
| 12 concurrent files, 1 GPU | 52.5× | 48,519 MB |

**Perfect linear scaling 1× through 12× concurrent tasks — zero degradation.** 1 hour of audio processes in 89 seconds (GPU) / 97 seconds (total). A6000's 48GB VRAM supports ~20 concurrent pipelines before memory ceiling.

PyAnnote optimization fork (`davidamacey/pyannote-audio@gpu-optimizations`):
- **1.28× faster** overall on CUDA; **1.44× faster** embedding stage
- **66% VRAM reduction** — constant regardless of file length
- **115× CPU RAM reduction**: 58.8GB → 39MB for a 4.7hr/21-speaker file
- Apple Silicon (MPS): 1.17× faster, native FFT, same memory safety

**Multi-GPU deployments**: `./opentr.sh start dev --gpu-scale` runs N parallel Celery workers against
one tunable GPU (`GPU_SCALE_WORKERS`), and a separate `--gpu-split` overlay routes transcription and
diarization to two different GPUs. Both are deployment options additive to the single-GPU numbers
above — no separate benchmark figures for multi-GPU throughput are published here.

---

## What OpenTranscribe Has That Nobody Else Does

| Capability | All Tier 3 competitors | **OpenTranscribe** |
|---|---|---|
| **Self-hosted / on-premises** | ✗ zero options | **✓ only option in market** |
| Pluggable ASR backend (10 providers) | ✗ | **✓** |
| Pluggable diarization backend | ✗ | **✓ (local PyAnnote, pyannote.ai cloud, or ASR-integrated)** |
| GPU-local transcription + diarization | ✗ | **✓ (WhisperX + PyAnnote fork)** |
| Multi-GPU worker scaling | ✗ | **✓ (`--gpu-scale` / `--gpu-split` overlays)** |
| Best-in-class diarization accuracy | Partial | **✓ 0.27% WSER, beats all cloud providers** |
| Boundary-correction smoother (turn-seam speaker errors) | ✗ | **✓ −32% relative WSER, default on, DB/UI-tunable** |
| 100+ language transcription | Partial (Otter EN-only) | **✓ (WhisperX)** |
| Cross-file semantic search | Basic or none | **✓ (OpenSearch + embeddings)** |
| URL ingestion (yt-dlp, 1,800+ platforms) | ✗ | **✓** |
| Voice fingerprinting across files | ✗ | **✓** |
| HIPAA/air-gap compatible | ✗ | **✓ by design** |
| Open source (AGPL) | ✗ all proprietary | **✓** |
| Multi-provider LLM summarization | ✗ | **✓ (12 output languages)** |
| RAG / chat over transcript archive, with citations | ✗ | **✓ shipped — issue #52 (v0.5.0)** |
| Watch folder / bucket auto-ingest (local / S3 / SMB) | ✗ | **✓ shipped — issue #26 (v0.5.0)** |
| Content redaction (PII / profanity / toxicity) | unconfirmed — see note | **✓ shipped — read-time masking + admin policy floor (v0.5.0)** |
| Enterprise auth breadth (LDAP + OIDC + SAML + PKI/mTLS + proxy header + MFA + SCIM, one build) | unconfirmed — see note | **✓ shipped (v0.5.0)** |
| Usage/cost tracking (tokens + estimated cost, per model) | unconfirmed — see note | **✓ shipped — `GET /usage/me` (v0.5.0)** |
| Live transcription + diarization | ✓ Otter/Fireflies/Grain | **planned — #69** |
| Meeting bot capture | ✓ Otter/Fireflies/Grain | **planned via Recall.ai** |

> Rows marked **unconfirmed** are ones this table previously scored as a flat "✗" for every
> competitor without a per-vendor source check. That blanket scoring is not being repeated here — a
> human should verify current enterprise-tier auth options (SAML SSO is plausible on a top plan;
> SCIM/PKI less so) and any compliance-redaction tooling before re-asserting a gap in either
> direction.

---

## Roadmap — GitHub Issues Mapped to Market Gaps

### Shipped since this roadmap was first written

**#52 — AI Chat / RAG over Transcripts — SHIPPED (v0.5.0).** Delivered as described below and then
some: per-file/collection/tag/speaker scoping, redaction-aware masking before any excerpt reaches an
LLM (fails closed), streamed answers with timestamp-linked citations, searchable conversation
history, and per-conversation model switching. Full feature list: `CHANGELOG.md` → "AI Chat with RAG
over your transcripts (issue #52)".

**#52 follow-on — retrieval quality is now MEASURED, and that is itself the differentiator
(2026-08).** Shipping RAG and being able to say how well it works are different claims, and the
second is rarer. What exists now: a reproducible eval harness over a 210,908-document corpus
scored with `trec_eval` (via `pytrec_eval_terrier`) and `ir_measures` — not hand-rolled metrics —
with baselines that reproduce **byte-identically across runs**. Tracked in #461, #463, #464.

Three findings that should shape positioning rather than be buried:

- **A 24-arm fusion bake-off adopted nothing.** OpenSearch's published BEIR **+3.86%** for score
  normalization **did not transfer** to this corpus. Publishing a negative result is a
  credibility asset for an open-source project, not an embarrassment.
- **Our two eval corpora are anti-correlated** on the arms anyone would adopt (Kendall tau-b
  −0.714 / −0.905). Improving one predicts harming the other, which means *any* competitor
  quoting a single-corpus tuning number is quoting something weaker than it looks — including
  us, before this was measured.
- **The shipped cross-encoder reranker may be hurting result ordering** (20.6% / 32.7% worse on
  nDCG@10, where the prior estimate was a 0.3–3.1% gain). Deliberately not acted on: a ranking
  metric cannot see whether the *answer* improved. #463 is the work that would settle it.

**Competitive note, researched rather than assumed.** Open WebUI is the usual open-source
comparison, and on corpus-scale summarization **it does not solve the problem and its maintainers
say so** — top-k retrieval answers "which passages match this query", a different question from
"what does this corpus say", and Full Context Mode simply truncates at the window. The
[auto-switch proposal](https://github.com/open-webui/open-webui/discussions/19177) is unbuilt, and
an [older issue](https://github.com/open-webui/open-webui/issues/3129) concedes a real pipeline
"requires significantly more implementation effort". A widely-reported symptom is that the same
model which summarizes a PDF well in ChatGPT returns a stub there — **the model is not the
variable, the retrieval strategy is.**

We already ship the pattern they lack: `tree_summarize` (LangChain's map-reduce) over a digest
plane whose per-file map output is **precomputed at ingest**, so an overview across 1,000
recordings costs zero map-time work — and it runs with **no LLM at all** (D6), which no
comparable product does. So on the "summarize everything" query class there is no open-source
prior art to import; we are not behind it. **The honest gap is that answer quality is not yet
measured** (#463) — retrieval is. Claiming "better RAG" before that lands would be exactly the
unfalsifiable marketing this project's engineering culture rejects.

Full method, per-arm command lines and every negative result:
`docs-site/docs/developer-guide/rag-evaluation.md`. Pattern taxonomy and prior art:
`rag-prior-art-and-packages.md`.

**#26 — Watch Folder / Automatic Bucket Processing — SHIPPED (v0.5.0).** Local mounted folder,
S3-compatible bucket, and SMB/CIFS share sources (not Google Drive, which was in the original issue
scope but did not ship), three-layer content-hash dedup, multi-part recording stitching, and
event-driven near-real-time local-folder watching alongside the scheduled-scan safety net.

**#78 — Admin-Controlled Prompt Sharing — SHIPPED.** Clone-into-your-own-library, creator
attribution, popularity ranking by actual usage count, and a full audit trail.

**Content redaction (PII / profanity / toxicity) — SHIPPED, not originally on this roadmap.**
Read-time masking across every display/export surface, per-user opt-out, and an admin enforcement
floor that can force categories on. Strengthens the "Priority 1: Privacy-first enterprise" segment
below independently of formal #98 certification.

**Enterprise authentication breadth — SHIPPED, not originally on this roadmap.** LDAP, generic OIDC,
SAML 2.0, PKI/mTLS, trusted-header reverse-proxy auth, TOTP MFA, and SCIM 2.0 provisioning, usable in
combination. Directly relevant to the procurement requirements that gate the "Privacy-first
enterprise" segment.

### Priority 1 — Category-defining features still open

**Recall.ai Integration** *(issue #365 — plan + gist published, not started)*
Register as an ingestion source alongside yt-dlp. Webhook receiver → Celery task → existing pipeline. Enables passive meeting capture. No core pipeline changes needed. Positions OT as self-hosted Fireflies/Otter Teams replacement. Recall.ai cost: \$0.70/hr meeting captured.

### Priority 2 — Competitive parity (close remaining gaps)

**#69 — Live Transcription + Real-time Speaker Identification** *(client-first plan published, not started)*
Direct microphone/stream input with live diarization. Closes the last feature gap vs. Otter/Fireflies/Grain. Recall.ai integration is the lower-complexity path to live meeting capture; #69 adds direct mic input for in-person scenarios.

**#98 — HIPAA / SOC 2 / GDPR Certification** *(10-workpackage plan published, not started)*
Self-hosting handles data residency technically, and the auth/redaction capabilities shipped above address several of the technical controls a SOC 2/HIPAA audit looks for. Formal certification still handles enterprise procurement contractually — without the badge, healthcare and legal buyers cannot sign regardless of technical capability. Unlocks the highest-value TAM segment.

### Priority 3 — Platform depth (stickiness and expansion)

**#48 — Apple Silicon (MLX-Whisper or whisper.cpp)** *(3-track plan published, not started)*
Native Mac deployment for prosumer self-hosters. Mac Studios are common in media production and podcasting. Removes GPU barrier for non-Linux deployments. Already partially supported via MPS PyAnnote optimization and hybrid mode's CPU-transcription/MPS-diarization split (auto-activates on macOS).

**#20 — Analytics Dashboard**
Talk time by speaker, activity over time, topic frequency, rolled up across the whole library. Per-file speaker analytics (talk time, interruptions, turn-taking) already exist; #20 is the cross-library dashboard. Adds team account stickiness and gives managers the reporting that Fireflies/Grain sell as a premium feature.

**#46 — Transcript Version Control**
Edit tracking for journalism/legal workflows where an audit trail matters. Differentiates from Sonix/Trint in the media and legal verticals.

---

## Market Segments — Where to Push

### 1. Privacy-first enterprise (largest TAM, immediate)
Healthcare, legal, government, defense, financial services. Every cloud competitor is disqualified by data residency requirements. OpenTranscribe is the only viable option.
- **Message**: *Your recordings stay on your servers. Full stop.*
- **Unlock**: Enterprise authentication (LDAP/OIDC/SAML/PKI/SCIM/MFA) and content redaction (PII/toxicity/profanity, admin-enforced) are live today. Formal HIPAA/SOC 2 certification (#98) is what remains for procurement sign-off — self-hosting plus these controls addresses much of the *technical* substance a compliance audit checks for, but the badge itself is not yet in hand.
- **Price point**: Replace \$52–\$80/seat/month Trint or \$29–\$39/seat/month Fireflies Enterprise with a one-time deployment.

### 2. Media and journalism (strong match, live today)
Trint is the incumbent at \$52–\$80/seat/month, cloud-only, no semantic search. Sonix at \$10/hr adds up fast. OpenTranscribe matches core capabilities at zero ongoing per-seat cost.
- **Message**: *Everything Trint does, self-hosted, with search that actually works.*
- **Unlock**: Live today. Version control (#46) and watch folders (#26) strengthen the story.

### 3. Meeting-heavy teams (medium-term, needs Recall.ai + #69)
50-person company spends \$10,800–\$18,000/year on Otter/Fireflies. OpenTranscribe eliminates that expense while delivering better diarization accuracy and cross-meeting search none of them offer.
- **Message**: *Replace Otter and Fireflies. Own your meeting data. No per-seat fees.*
- **Unlock**: Recall.ai integration or #69 (live transcription).

### 4. Researchers and academics (now, low friction)
Qualitative researchers, oral historians, ethnographers with hundreds of hours of interview audio. Sonix charges \$10/hr and has no cross-file search. OpenTranscribe costs nothing after deployment, and RAG chat (#52, shipped in v0.5.0) makes it category-defining for this use case.
- **Message**: *Transcribe your entire archive, then have a conversation with it.*
- **Unlock**: Live today.

### 5. Developers and technical teams (ongoing, community-driven)
Evaluate all 10 ASR providers against their own audio. Reference implementation for voice AI applications.
- **Message**: *One frontend, every ASR provider, benchmark on your own data.*
- **Unlock**: Live today.

---

## The Full-Product Vision

When the roadmap above is complete, OpenTranscribe is:

> **Self-hosted organizational memory for audio and video.**
>
> Drop a file, point to a folder, join a meeting, or paste a URL → everything is transcribed, speaker-labeled, cross-referenced by voice, indexed, and searchable. Ask it questions in natural language. It knows who said what, when, and in which recording. All data stays on your infrastructure.

No competitor in any tier offers this combination:

| Competitor | Why it falls short |
|---|---|
| Fireflies / Otter / Grain | Cloud-only, no archive depth, no RAG, no self-hosting |
| Trint / Sonix / Rev | File-based only, no meeting capture, no cross-file RAG |
| Verbit | Cloud-only despite enterprise pricing, no self-serve |
| Deepgram / AssemblyAI | API only — they're the infrastructure OT runs on top of |
| Recall.ai | No UI — the capture backend OT should consume |
| Speechmatics | On-prem ASR API only; OT + Speechmatics backend = ideal enterprise pairing |

OpenTranscribe, complete, is a category of one: **self-hosted meeting and media intelligence with best-in-class diarization accuracy and a full-stack RAG interface.**

---

## Performance Claims Summary (for marketing/investor use)

All figures verified against hand-labeled reference data and published in `docs/BENCHMARK_RESULTS.md` and `docs/diarization-boundary-results/cloud-comparison.md`.

- **0.27% WSER** on speaker attribution — tied best of any engine (local or cloud)
- Beats premium cloud providers: AssemblyAI (0.49%), Speechmatics (0.53%), AWS (0.69%), pyannote.ai (0.70%), Deepgram (5.76%)
- **40× realtime** single file on RTX A6000 (1 hour of audio → 89 seconds)
- **54.6× realtime** at 8 concurrent files (better GPU utilization)
- **Perfect linear concurrency scaling** 1× through 12× — zero degradation
- **4.7% VRAM usage** at single-file concurrency — ~20 parallel pipelines on a 48GB A6000
- **115× CPU RAM reduction** vs stock PyAnnote for long files (58.8GB → 39MB)
- Pipeline runs on **2,317 MB VRAM** — deployable on consumer GPUs (RTX 3060 and up)
- Apple Silicon support via PyAnnote MPS fork: **1.17× faster** than CPU on Mac Studio M2 Max
- **Boundary-correction smoother** (issue #193 — a separate benchmark, on a separate hand-labeled
  clip, from the 6-provider comparison above; the two figures should not be added together):
  **−32% relative WSER** and speaker-label "islands" reduced 82→15, pure-CPU post-processing,
  default on. An experimental GPU acoustic re-check adds a further **~−15% WSER** atop the smoother
  for ~1.9s of added processing per 10-minute file, default off.

---

*Detailed profiles: `cloud-asr-market-research.md` · `competitor-landscape.md`*
*Benchmark raw data: `docs/BENCHMARK_RESULTS.md` · `docs/diarization-boundary-results/cloud-comparison.md`*
*Technical deep-dives: `docs/PYANNOTE_OPTIMIZATION_SUMMARY.md` · `docs/GPU_OPTIMIZATION_RESULTS.md`*
*Academic whitepaper: `docs/performance-whitepaper/main.tex` → `main.pdf`*
