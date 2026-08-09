# OpenTranscribe vs. OpenWhispr — Competitive & Technical Comparison

**Research date:** 2026-08-09
**Sources:** `github.com/OpenWhispr/openwhispr` (cloned locally at `/mnt/nvm/repos/openwhispr`, MIT license), cross-referenced against `/mnt/nvm/repos/transcribe-app` (backend code, `docs/ARCHITECTURE.md`, `docs/combined-engine-design.md`, `docs/diarization-boundary-results/`, `CHANGELOG.md [0.4.0]`, `README.md`).
**Scope:** Code-level research only. No code in either repo was modified as part of this document.

---

## 1. What each project actually is

| | **OpenTranscribe** | **OpenWhispr** |
|---|---|---|
| Form factor | Self-hosted web platform, Docker Compose, multi-container | Native desktop app (Electron), single install, single user |
| Target user | Teams/orgs — multi-user, self-hosted or enterprise deployment | Individual — dictation + meeting notes on your own machine |
| Core use case | Upload/record files, transcribe, diarize, search a library, chat with a knowledge base of recordings | Push-to-talk dictation into any app, live meeting notes with speaker ID, local AI notes/chat |
| License | (Attevon LLC, not MIT per your CLAUDE.md constraints) | **MIT** — confirmed, `LICENSE`: `Copyright (c) 2024 OpenWhispr Team` |
| Codebase size | Backend alone spans dozens of services/tasks across a distributed system | 587 files / **~128,000 LOC** in `src/` (JS/TS), 61 runtime deps + 23 dev deps in `package.json` |

These are not really the same product wearing different clothes — OpenWhispr is architecturally a **local-first dictation/meeting-notes desktop app**; OpenTranscribe is a **multi-tenant transcription platform**. The overlap is real (both do Whisper-based ASR + speaker diarization + AI chat over transcripts), but the design centers are different, which explains most of the size/complexity gap below. That said, OpenWhispr's *implementation techniques* are genuinely worth studying — several are things a heavier platform can also use.

---

## 2. Transcription: how each actually does it

### OpenWhispr (code-level, verified)

- **Local engine**: `whisper.cpp`, run as a **separately compiled C++ binary** (`whisper-server`), spawned via `child_process.spawn` from Electron's main process (`src/helpers/whisperServer.js:532`). Not a Node addon, not Python — a per-platform binary (`whisper-server-<platform>-<arch>[-cuda|-vulkan]`) pulled from OpenWhispr's own `whisper.cpp` fork (pinned tag `0.0.8`) at **build time**, not bundled generically for all platforms.
- **Audio path (dictation)**: browser `MediaRecorder` → single `Blob` → written to temp file → **ffmpeg** (via `ffmpeg-static`) converts to **16kHz mono 16-bit PCM WAV** (hard requirement of whisper.cpp) → one multipart/form-data POST (hand-built, no `form-data` package) to `whisper-server`'s local HTTP `/inference` endpoint → single JSON response parsed. **Strictly batch — no partial/streaming results for local transcription.**
- **`--no-timestamps` is used deliberately** — a comment in the code documents a whisper.cpp v1.9.x regression that corrupts word timestamps under their line-wrap mode, so they disable timestamp mode entirely and just take the plain text. **This means OpenWhispr dictation transcripts have no word-level timestamps at all** — a real capability gap vs. OpenTranscribe, where word/segment-level timestamps are foundational (needed for the whole diarization-merge and boundary-correction pipeline).
- **Streaming does exist, but only for 3 cloud providers** (Deepgram, AssemblyAI, OpenAI Realtime) — those use a completely different capture path: an inline `AudioWorkletProcessor` converting Float32 → Int16 PCM in an 800-sample ring buffer, streamed over WebSocket. Local whisper.cpp never streams.
- **No deterministic post-processing** (no punctuation restoration module, no formatting rules found). Optional "cleanup" is an **LLM rewrite pass** (`wrapCleanupTranscript`), not a dedicated ASR post-processor.
- **Meeting capture** is architecturally distinct from dictation: continuous raw 24kHz PCM to disk, replayed through diarization at meeting end.

### OpenTranscribe (for contrast)

- Local: faster-whisper/WhisperX on GPU (with word-level timestamp alignment as a first-class output, since diarization merge depends on it). Also 10 pluggable cloud ASR providers (Deepgram, AssemblyAI, OpenAI, Google, Azure, AWS, Speechmatics, Gladia, pyannote.ai) via a factory abstraction (`backend/app/services/asr/factory.py`).
- 3-stage Celery pipeline (`preprocess → gpu → postprocess`) rather than a single-process HTTP round trip — built for concurrent multi-user throughput, not single-user latency.

**Takeaway**: OpenWhispr's local transcription is legitimate and simple (subprocess + HTTP + ffmpeg), but it deliberately sacrifices word-level timestamps for local dictation, which is a real capability OpenTranscribe has that OpenWhispr does not. It's a fair trade for a dictation tool (you're pasting the text, not indexing it), but it would be a regression if it were used as-is for a searchable multi-file archive.

### No video support, anywhere — confirmed in code

The README claims "transcribe existing audio **and video**," but this doesn't hold up once traced through the code:

- **File upload is audio-only.** `src/components/notes/UploadAudioView.tsx:1433` — `accept=".mp3,.wav,.m4a,.webm,.ogg,.oga,.flac,.aac,.opus"`. No `.mp4`, `.mov`, `.mkv`, or `.avi` anywhere in the accept list.
- **Even the video-URL import path strips video immediately.** The YouTube/URL downloader calls yt-dlp with `-x --audio-format` (`src/helpers/urlAudioDownloader.js:929-930`) — yt-dlp's audio-extraction flag. So pasting a YouTube URL never gives the app a video file to work with; it downloads and discards the video stream before the app ever sees it.
- **No video playback UI exists at all** — zero `<video>` tags anywhere in `src/components`. Every stored recording is `.webm` audio (`audioStorage.js`), consistent with the app's identity as a dictation/notes tool, not a media library.
- **OpenTranscribe, for contrast**: `.mp4/.mkv/.mov/.avi` are first-class accepted upload types with dedicated content-type handling (`backend/app/services/storage_recovery_service.py`, `backend/app/services/media_download_service.py`), and there's a real in-app video player (`frontend/src/components/VideoPlayer.svelte`, `PlyrMiniPlayer.svelte`) — you can upload a video file and watch it alongside its transcript, not just extract audio from it.

This is a genuine, verifiable capability gap, not a nuance — "transcribe existing audio and video" in their README overstates what the product does; it transcribes audio, including audio *pulled from* video sources, but never handles or plays video itself.

---

## 3. Diarization: how each actually does it — this is the section that matters most to you

### OpenWhispr's diarization, traced end to end

OpenWhispr **does have real speaker diarization**, not a stub — but it's structurally simpler and less tuned than what you've built. Two separate mechanisms:

**(a) Offline/batch diarization (the one that produces the final transcript labels)**

1. Trigger: at meeting end (`src/helpers/ipcHandlers.js:9685`), raw 24kHz PCM → resampled to 16kHz mono WAV.
2. Spawns a **separately compiled binary**, `sherpa-onnx-diarize` (from `k2-fsa/sherpa-onnx` releases — not written by OpenWhispr), via `child_process.spawn`, with CLI flags:
   ```
   --segmentation.pyannote-model=<path>
   --embedding.model=<path>
   --clustering.num-clusters=<n>
   --clustering.cluster-threshold=<t>
   --min-duration-on=0.2  --min-duration-off=0.5
   ```
3. **Segmentation model**: `sherpa-onnx-pyannote-segmentation-3-0` — this is **pyannote's own segmentation-3.0 model**, exported to ONNX by the sherpa-onnx project. Same model family you use, just ONNX-exported and run through a compiled binary instead of PyTorch.
4. **Embedding model**: `3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx` — a CAM++ speaker-verification model (VoxCeleb-trained). **Not** the WeSpeaker 256-dim embeddings your fork uses — a different embedding model entirely.
5. **Clustering**: agglomerative clustering happens *inside the compiled sherpa-onnx binary* — not visible/vendored source in the OpenWhispr repo itself, just consumed via CLI flags (`num-clusters=-1` for auto-detect, `cluster-threshold=0.55` default).
6. **Output**: plain-text stdout lines (`start -- end speaker_N`), regex-parsed.
7. **Post-processing found**: exactly one — `capSpeakerClusters()`, a pure-JS pass that sums per-speaker duration and collapses any speaker beyond a target count into the dominant speaker. **No boundary-smoothing, no acoustic re-check, no WSER-style tuning of any kind was found anywhere in the repo.**
8. **Transcript merge**: segment-level (not word-level) overlap between diarization segments and transcript segments — `overlap = min(segEnd, dSeg.end) - max(segStart, dSeg.start)`, picks the diarization label with max overlap, falls back to nearest-midpoint distance. Mic-sourced audio is unconditionally labeled "you" (no diarization needed for the local speaker at all in the meeting-notes use case).
9. **Where the transcript-side timestamps actually come from, given `--no-timestamps` is set** (this looked like a contradiction until traced): `--no-timestamps` is a single flag passed once to the shared `whisper-server` process (`whisperServer.js:151`) — it applies to *every* call to that server, dictation and meetings alike, so Whisper itself never emits timing info for either path. The `seg.timestamp` the merge code reads (`diarization.js:471-472`, confirmed via `audioManager.js:1077`) is instead `(Date.now() - recordingStartTime) / 1000` — a JS-side wall-clock stamp of when the app *sent* that chunk, not anything Whisper computed. Segment end is then approximated as either the next chunk's capture timestamp or `segStart + 2.5s`. The diarization side, by contrast, has real model-derived timing from the sherpa-onnx segmentation pass. So the merge is fundamentally *rough capture-order chunk boundaries* (transcript) matched against *precise model timing* (diarization) — which is the underlying reason the merge has to be segment-level rather than word-level: there's no fine-grained transcript-side timing to align against in the first place, not just a design choice.

**(b) Live/online diarization (provisional labels during the meeting)**

- Runs in parallel via `onnxruntime-node` (a real Node native addon, not a subprocess) doing Silero VAD scoring + CAM++ embedding extraction on speech windows.
- Simple nearest-centroid clustering in plain JS (`_findNearestTransient`, cosine similarity against running centroids) — no formal clustering algorithm, just running centroids with a similarity threshold.
- Purely for responsive UI labels while the meeting is live; **the offline sherpa-onnx pass is what determines the final transcript labels** — live labels get overwritten.

**(c) Cross-meeting speaker memory** — a feature you may want to look at:

- SQLite tables (`speaker_profiles`, `speaker_mappings`, `note_speaker_embeddings`) persist a 512-dim CAM++ embedding **only when a user manually renames a speaker** (e.g. "Speaker 1" → "Alice") — this is explicit, one-time, per-person, not automatic bulk clustering across your whole library.
- Future-meeting matching: cosine similarity against every stored profile, requiring **both** an absolute similarity floor (`MATCH_THRESHOLD = 0.65`) **and** a margin over the second-best candidate (`MATCH_MARGIN = 0.03`) — specifically to avoid confidently mislabeling similar-sounding voices.
- Profile embeddings update via exponential moving average (`0.3 * new + 0.7 * stored`) rather than being overwritten, so a profile stabilizes over repeated meetings with the same person.
- This is conceptually similar in spirit to your cross-file speaker matching via OpenSearch kNN, but implemented as a simple local SQLite + cosine-similarity scheme rather than a search-cluster-backed system. It works because it's single-user/local-scale, not library/org-scale.

### Direct comparison to your pipeline

| | **OpenTranscribe** | **OpenWhispr** |
|---|---|---|
| Segmentation model | pyannote v4, your GPU-optimized fork (`davidamacey/pyannote-audio@gpu-optimizations`) | Stock pyannote segmentation-3.0, ONNX-exported by a third party (sherpa-onnx) |
| Embedding model | WeSpeaker 256-dim | CAM++ (3D-Speaker/VoxCeleb) — different model family |
| Batch-size / VRAM tuning | Explicit, fixed at 16, DER-invariance validated 1–128 | Not applicable — CPU-side ONNX Runtime, no batch-size tuning surfaced |
| Boundary correction | Custom 2-stage: boundary smoothing (**-32% WSER**, default on) + acoustic backchannel re-check (**+15% more**, opt-in) | **None found.** Only post-hoc speaker-count capping. |
| Word- vs. segment-level speaker assignment | Word-level (needed for boundary smoothing to work at all) | Segment-level overlap only |
| Benchmarked accuracy | 0.27% WSER on your hand-labeled clip, tied for best of 7 engines incl. AWS/AssemblyAI/pyannote.ai/Speechmatics; DER@0.25 ≈ 0.22 on AMI, matching published pyannote figures | No published accuracy benchmarks found in the repo (no equivalent of your `docs/diarization-boundary-results/`) |
| Cross-recording speaker ID | OpenSearch kNN voiceprint matching across the entire multi-user library, gender-informed cluster validation | SQLite cosine-similarity match against manually-named local profiles only |
| Hardware flexibility | CUDA GPU (incl. Blackwell), hybrid CPU+MPS mode, multi-GPU split | CPU-only ONNX Runtime for embeddings/VAD; diarization binary presumably CPU (no CUDA flag seen in the sherpa-onnx-diarize invocation) |

**Bottom line on diarization**: your concern about someone else's "similar and lighter" tool not actually doing the job well is well-founded here, and the evidence supports it directly — OpenWhispr uses an out-of-the-box pyannote segmentation model with no boundary-correction tuning and no published accuracy validation, while you have empirical, benchmarked evidence that your tuned pipeline **ties premium commercial cloud diarization APIs** on speaker-boundary accuracy. This is a legitimately strong, defensible differentiator, not marketing language — it's something you can cite (your own `docs/diarization-boundary-results/cloud-comparison.md`) that a lightweight competitor plainly has not done. The one thing worth borrowing conceptually is not a diarization algorithm improvement but the **speaker-count capping heuristic** (collapsing over-clustered minor speakers into the dominant one by cumulative duration) — simple, cheap, and complementary to what you already do; worth checking whether your pipeline already handles the "too many spurious speakers" failure mode as gracefully.

---

## 4. Model / API connections

### OpenWhispr

- **ASR**: local whisper.cpp (any GGML model size the user downloads) + cloud streaming (Deepgram, AssemblyAI, OpenAI Realtime, and **Corti** — healthcare-specific ambient/clinical streaming STT, `cortiStreaming.js`) + local NVIDIA **Parakeet** (fast multilingual ASR model, run via sherpa-onnx/ONNX as a local Whisper alternative, not a cloud provider).
- **LLM**: local `llama-server` (llama.cpp, GGUF models) + 9 cloud/network providers, one file each in `src/services/ai/inferenceProviders/`: `openai`, `anthropic`, `gemini`, `groq`, `enterprise`, `lan` (generic OpenAI-compatible LAN endpoint), `tinfoil`, `corti`, `openwhispr` (their own hosted cloud).
- **Provider abstraction**: one thin file per provider (`anthropic.ts`, `groq.ts`, etc.), registered in `inferenceProviders/index.ts`'s `PROVIDER_REGISTRY` — simpler than a factory-catalog pattern since there's no per-user credential encryption/multi-tenant concern to design around.
- **No LDAP/OIDC/SAML/PKI, no per-user encrypted API key storage** — makes sense, it's a single-user local app; there's exactly one user, so provider credentials are just local config, not a security-sensitive multi-tenant concern.

### OpenTranscribe (for contrast, already documented in your codebase)

- 10 ASR providers, 7 LLM providers (`openai`, `vllm`, `ollama`, `anthropic`, `openrouter`, `custom`, `bedrock` — `backend/app/services/llm_service.py:42`, a single `LLMProvider(StrEnum)` with per-provider branches, not a per-file pattern), per-user AES-256-GCM encrypted API keys, PBKDF2-SHA256 600k iterations — this is solving a different problem (multi-tenant credential isolation) that OpenWhispr's design doesn't need to address at all.

**What you can learn**: OpenWhispr's per-provider file pattern (`inferenceProviders/local.ts`, `.../deepgram.ts`, etc., each self-contained) is clean and easy to extend — worth comparing against your single-enum-with-branches pattern in `llm_service.py` for long-term maintainability as you add more providers, though your version has to handle strictly more (per-user settings resolution, encrypted secrets, shared-config permissions) that a single-user app doesn't.

### LLM provider gap analysis — what's actually worth adding

Comparing your 7 LLM providers against their 9 turns up three genuine gaps worth prioritizing, and several that aren't:

| Provider | Verdict | Why |
|---|---|---|
| **Tinfoil** | **Add — highest priority** | Runs inference inside confidential-computing enclaves (TEEs) so not even the provider can see plaintext data — a cryptographic guarantee, not a policy promise. This is a better fit for *your* FIPS-140-3-aligned, FedRAMP-aligned enterprise/government positioning than it is for OpenWhispr's actual individual-user market. It would let compliance-sensitive customers get cloud-LLM speed/quality with a hard technical guarantee against provider-side exposure — strengthens a differentiator you already lead on rather than just closing a parity gap. |
| **Gemini (Google)** | **Add** | You already integrate Google Cloud Speech for ASR but have no direct Google LLM path. Rounds out direct access to all three major labs (OpenAI/Anthropic/Google) instead of two, and Gemini's long-context models are a natural fit for your multi-section summarization stitching on long transcripts. |
| **Groq** | **Add** | Not a model — an inference host (custom LPU silicon) running open models (Llama/Mixtral-class) at very low latency. Matters anywhere latency is user-visible: chat streaming, live-ish summarization. Your `openrouter` provider may already route to Groq-hosted models indirectly, but that's subject to OpenRouter's markup and routing choices, not a guaranteed low-latency path. |
| Corti | Skip | Healthcare-vertical-specific (clinical note structuring, medical coding awareness) — only relevant if pursuing a healthcare vertical, which doesn't fit your current FedRAMP/government-leaning positioning. |
| `enterprise` / `lan` | Skip | Conceptually already covered by your existing `custom` / `vllm` / `ollama` self-hosted-endpoint options. |
| `openwhispr` | N/A | Their own hosted product, not a general-purpose provider pattern to borrow. |

### Other capability gaps found while researching this (not provider-related)

- **MCP server — real gap.** OpenWhispr ships a public API and an MCP (Model Context Protocol) server (README: "Public API & MCP... manage notes and transcriptions programmatically or connect your AI assistant"), Pro-tier and up. OpenTranscribe has no MCP server anywhere in `backend/app` or `docs/` (verified by grep — the only hit was this document). Given your product already centers on "AI chat over your transcripts," an MCP server would let Claude/other agents use OpenTranscribe as a tool directly (search transcripts, pull summaries, query speakers) rather than only being usable through your own chat UI. Worth a scoping discussion, not just a provider add — larger than the LLM-provider issues below.
- **Live/real-time meeting transcription — confirmed non-gap-by-design, but worth naming.** OpenWhispr auto-detects Zoom/Teams/FaceTime calls and transcribes+diarizes live as the meeting happens. OpenTranscribe is verified file-based/batch-only: `backend/app/api/websockets.py` exists but is purely a server→client push-notification channel (job progress, broadcast) — no provider or endpoint takes live audio-in for streaming transcription. This matches the project's own documented constraint (no real-time/streaming ASR claims). Not a quick fix — it's a different architecture, not a missing provider — but it's already tracked at the "ingest live meetings" level by existing issue **#365** ("Recall.ai meeting capture — ingest meetings, transcripts, speakers and metadata into the same library"), which takes the bot-ingestion route rather than building native live capture.
- **Custom vocabulary — not a gap, you're already there.** OpenTranscribe has `backend/app/api/endpoints/custom_vocabulary.py` and `backend/app/models/custom_vocabulary.py`, wired into at least the AssemblyAI provider. No need to chase this one.
- **NVIDIA Parakeet (local ASR)** — already tracked as issue **#366** ("NVIDIA NeMo (Parakeet / Canary) as a first-class local transcription engine"), so no new issue needed there either.

---

## 5. How OpenWhispr keeps the install small — the actual mechanism

This is the most transferable lesson, and it's a clear, replicable pattern:

1. **No PyTorch, no CTranslate2, anywhere in the codebase.** Confirmed by a repo-wide grep — zero matches for `pytorch`, `torch`, `ctranslate2`. This is the single biggest size lever: a Python+PyTorch+CUDA stack is multiple GB before you've added a single model; OpenWhispr never pays that cost because it never uses that stack.
2. **All heavy inference runs through compiled, statically-linked native binaries, not embedded runtimes**: `whisper-server` (whisper.cpp/GGML), `llama-server` (llama.cpp/GGUF), `sherpa-onnx-diarize` (ONNX Runtime statically linked inside the binary), `qdrant` (Rust binary for vector search). Each is a small, purpose-built C++/Rust binary instead of a general-purpose ML framework — GGML/ONNX Runtime compiled binaries are on the order of tens of MB, not gigabytes.
3. **Binaries are fetched per-platform at build time, not bundled for every OS/arch.** `scripts/download-whisper-cpp.js`, `download-llama-server.js`, `download-sherpa-onnx.js`, `download-qdrant.js` each pull exactly one binary for the target platform/arch during `prebuild:*` — a macOS installer never carries Windows/Linux binaries.
4. **Model *weights* are not bundled in the installer at all.** The prebuild scripts fetch the *inference engine binaries* and small auxiliary models (VAD, diarization segmentation/embedding models — these are small, tens of MB), but the actual Whisper transcription model weights (tiny/base/small/medium/large) and any local LLM GGUF weights are downloaded **on first use, chosen by the user**, via `modelManagerBridge.js` at runtime from Hugging Face. This is the same trick MacWhisper and similar tools use: ship an empty shell, let the user's first-run experience pull only the model size they actually want.
5. **`electron-builder`'s `files` allowlist explicitly excludes dev tooling** from the packaged app — `typescript`, `eslint`, `vite`, `rollup`, `postcss`, `electron-builder` itself, etc. are all excluded via `!**/node_modules/<pkg>/**` globs, even though they're in `node_modules` during development.
6. **`onnxruntime-node`** is used narrowly (VAD scoring, standalone embedding extraction) rather than pulled in for everything — ONNX Runtime's native addon is a fraction of PyTorch's footprint for inference-only workloads.

**What's directly applicable to your `Dockerfile.lite`**: you already have the core idea right (`requirements-lite.txt`, CPU-only, ~2-3GB vs ~12-13.8GB full). The OpenWhispr pattern confirms the biggest remaining lever if you ever wanted to shrink further would be replacing any PyTorch-based CPU inference paths with ONNX Runtime or GGML-based equivalents (e.g., `faster-whisper` already uses CTranslate2, which is much lighter than raw PyTorch — worth checking whether `requirements-lite.txt` still pulls in a full `torch` wheel for anything, e.g. sentence-transformers for embeddings, since that's exactly the kind of dependency OpenWhispr avoids entirely by using ONNX Runtime for its embedding models instead).

---

## 6. Search and "AI chat" without OpenSearch

- **Vector store**: **Qdrant**, run as a standalone compiled Rust binary (`src/helpers/qdrantManager.js:97`, `spawn(binaryPath, ["--config-path", configPath], ...)`), downloaded via `scripts/download-qdrant.js` — same sidecar-binary pattern as the ASR/diarization/LLM engines. Not embedded-in-process; a local server the app talks to over localhost.
- **Embeddings**: a local MiniLM-class model run through `onnxruntime-node` in a worker thread (same worker that handles speaker embeddings) — no OpenAI embeddings API dependency for local-only usage.
- **No BM25/full-text engine equivalent to OpenSearch was identified** — search appears to be vector-similarity-only via Qdrant, not a hybrid BM25+vector approach like your `HybridSearchService` (RRF fusion). This is a meaningful capability gap on OpenWhispr's side: pure semantic search without keyword/exact-match fallback will underperform on things like exact names, jargon, or short technical terms that dense embeddings handle poorly. Your hybrid RRF approach is more robust for this reason.
- **Why they can skip OpenSearch and you can't (by design)**: Qdrant-as-a-binary is a single-user, single-tenant, embedded-scale vector index — no cluster, no sharding, no multi-tenant filtering, no ML Commons plugin. Your OpenSearch deployment does double duty (full-text + neural search + multi-tenant org-scoping + redaction-aware snippet masking), which is inherently a heavier, cluster-capable system because it's serving concurrent multi-org queries at library scale, not one person's local notes.
- **AI chat**: LLM providers (local llama-server or cloud) generate answers grounded in Qdrant-retrieved context — architecturally the same RAG shape as your chat pipeline (retrieve → prompt → generate), just without your scoping/redaction/reranking/governance layers, which again are multi-tenant-specific concerns a single-user app doesn't need.

**What you can learn**: Qdrant is a legitimately interesting lightweight alternative to consider for any future single-tenant/self-hosted-lite deployment profile — it's a much smaller footprint than an OpenSearch cluster if you ever wanted a genuinely minimal "OpenTranscribe Lite" single-user mode that doesn't need multi-tenant search. It would not replace OpenSearch for your primary product, but the pattern of "compiled vector-DB binary spawned as a sidecar" is worth knowing about as an option.

---

## 7. What you can concretely borrow (MIT license permits direct reuse)

Ranked by how directly transferable each is to your codebase:

1. **Speaker-count capping post-process** — cheap, simple, complements your existing pipeline; check if you already handle over-clustering as gracefully (`capSpeakerClusters` logic: sort by cumulative speaking duration, keep top-N, collapse the rest into the dominant speaker).
2. **On-demand model weight downloads instead of bundling** — if not already true of every deployment mode, confirm your Docker images don't bake in every ASR model size; if they do, consider a first-run download flow for optional/larger model variants to shrink base images further.
3. **Per-provider file pattern for LLM inference** (`inferenceProviders/<name>.ts`, one file per provider, thin and uniform) — worth a structural comparison against your ASR/LLM factory pattern for long-term maintainability, even though your factory needs to do more.
4. **Sidecar-binary pattern for CPU-bound auxiliary inference** (VAD, small embedding models) via ONNX Runtime instead of full PyTorch, if you have any CPU-mode paths still pulling in `torch` for lightweight tasks.
5. **Cross-session speaker memory via simple threshold+margin matching** (`similarity >= floor AND similarity - second_best >= margin`) — a nice, easy-to-explain heuristic for avoiding false-positive speaker identity matches; worth comparing against whatever confidence-scoring your own speaker-ID suggestion feature uses.

What is **not** worth borrowing: their diarization tuning itself (yours is measurably better and validated), their lack of word-level timestamps in local dictation mode, and their lack of hybrid full-text+vector search (their pure-vector approach is a known weakness, not a strength).

---

## 8. Where OpenTranscribe is a clear market differentiator

Independent of anything above, these are capabilities a single-user desktop tool like OpenWhispr structurally cannot offer, because they only make sense at multi-user/organizational scale:

- **Empirically validated diarization accuracy** tying premium commercial cloud APIs (AWS Transcribe, pyannote.ai, AssemblyAI, Speechmatics) — a rare, citable, benchmarked claim most competitors (lightweight or not) don't have.
- **Multi-user, multi-tenant architecture**: organizations, RBAC, collection sharing, per-org search scoping.
- **Enterprise identity**: LDAP/AD, OIDC (7 named IdPs), SAML 2.0, PKI/CAC-PIV smart cards, SCIM 2.0 provisioning, reverse-proxy header auth — six methods simultaneously.
- **Compliance posture**: audit logging (AU-2/AU-3), FedRAMP-aligned controls (IA-5/AC-7/AC-8), FIPS 140-3 aligned crypto — none of this is meaningful for a local single-user app.
- **Hybrid BM25+vector search at library scale** with multi-tenant filtering, vs. OpenWhispr's pure-vector single-user index.
- **Governed RAG chat**: scoped by recording/collection/tag/speaker, redaction-masked before hitting any LLM (fails closed), layered system prompts with non-overridable base rules, per-tenant narrowing-only settings cascade — a compliance-aware chat feature, not just "ask your notes."
- **Cross-file speaker intelligence at library scale** via OpenSearch kNN across an entire multi-user corpus, vs. OpenWhispr's local SQLite table of manually-named profiles.
- **Automated ingestion** (Watch Sources: S3/SMB/local folder polling with dedup) for team workflows, vs. OpenWhispr's manual per-recording flow.
- **Horizontal scalability**: 6+ specialized Celery workers, multi-GPU split/scale modes, Flower monitoring — built to serve many concurrent users, not one.

The honest framing for positioning: OpenWhispr is a well-built, genuinely capable **personal dictation and meeting-notes tool** with clever, size-conscious engineering (compiled-binary sidecars, on-demand model downloads, no PyTorch). It is not a competitor to OpenTranscribe as a **team/organizational transcription platform** — it's closer to a category adjacent to MacWhisper/Wispr Flow than to a self-hosted enterprise transcription system. The overlap that matters for messaging is narrow (both transcribe with Whisper-family models and both diarize), and on the one piece you've spent real, measurable effort on — diarization accuracy — you have benchmarked evidence they don't have anything close to.

---

## 9. Should OpenTranscribe move off PyTorch? (checked against your own prior optimization work)

This question came up directly out of this comparison, and it turns out your team already ran the experiment — `docs/upstream-patches/phase-6-2-lessons-learned.md` and `phase-6-3-spike-results.md` have hard numbers. Short answer: **mostly no, and the "yes" parts are narrower than "move off Python/PyTorch" suggests.**

- **Transcription isn't naive Python/PyTorch to begin with.** `faster-whisper`/WhisperX already run on **CTranslate2**, a compiled C++ inference engine — the same *category* of thing as OpenWhispr's whisper.cpp, just a different implementation. There's no "switch to compiled code" win available here because that switch already happened.
- **Diarization (pyannote) is where PyTorch actually runs**, and replacing it with plain ONNX Runtime on GPU was measured to be **4-8x slower**, not faster (`phase-6-2-lessons-learned.md`). Cause: ONNX Runtime 1.25's CUDA execution provider has no CUDA kernel for several ops pyannote's graph uses (`LSTM`, `If`, `Sin`, `Cos`), so those ops silently fall back to CPU mid-graph, forcing GPU↔CPU memory copies on every forward pass. A 2.2-hour benchmark that takes ~100s under eager PyTorch was still running after 28 minutes under ONNX Runtime CUDA EP before it was killed.
- **TensorRT is the one place a real win showed up** (`phase-6-3-spike-results.md`) — 2x faster than PyTorch eager, but only on the embedding sub-model, and only in a fixed-shape microbenchmark. At full-pipeline scale it hit a different wall: TensorRT compiles a new engine per input shape (~12s each), and the segmentation model sees 30+ distinct batch shapes per run, so first-run performance is *worse* until shape-profiling is added (documented, not yet implemented — real remaining work, not a dead end). It also costs +4.5GB of image size and engine plans are locked to one GPU architecture (an A6000 build won't run on a 4090 or H100).
- **The one clearly-open, cheap win**: ONNX Runtime's *CPU* execution provider is expected to be 2-3x faster than eager PyTorch on CPU (normal — ORT fuses kernels, no autograd tape). This would directly help `Dockerfile.lite`/CPU deployments and is flagged in your own docs as untested — "simple benchmark, no code changes."

**Why this differs from OpenWhispr's story**: OpenWhispr's "avoid Python for speed" pitch is solving a different problem — one meeting on one person's laptop, where per-call overhead matters. Once you're batching 16-32 items on a shared GPU, kernel execution already happens in compiled CUDA/cuDNN either way and Python's overhead is negligible against GPU compute time. Your real remaining levers are the three already scoped in your own docs: finish TensorRT shape-profiling (~5-10% E2E, GPU path), validate ORT CPU EP (lite mode), validate CoreML EP (Mac/MPS, also untested).

---

## 10. OpenWhispr Cloud — pricing and positioning

Checked `openwhispr.com/pricing` directly (2026-08-09):

| Tier | Price | Adds over previous tier |
|---|---|---|
| Free | $0 | Unlimited *local* dictation, 2,000 words/week cloud transcription, 5 hrs meeting recording/month, 100+ languages, custom dictionary, zero data retention, community support |
| Pro | $6.67/user/mo ($80/yr) | Unlimited cloud transcription, 20 hrs meetings/mo, cross-device sync, personal API access, MCP integration, mobile app, email support |
| Business | $16.67/user/mo ($200/yr) — "Most Popular" | Unlimited meeting recordings, **speaker labels**, **chat over your data**, priority support |
| Enterprise | Custom | Advanced team/admin tools, **SSO & SAML**, **SCIM provisioning**, **audit logs**, org-wide retention controls, custom DPAs, dedicated support |

**Why this matters for positioning**: speaker diarization and AI chat over transcripts are **Business-tier, $16.67/user/month** features in their cloud product. SSO/SAML/SCIM/audit logs — the whole slate of enterprise identity and compliance features — is their **top, custom-priced Enterprise tier**. Those are exactly the capabilities OpenTranscribe already ships for free in a self-hosted deployment (§8 above). The sharper comparison isn't "our feature list vs. their feature list" — it's that what you give away by default is their highest-paying customers' upsell ceiling. Local dictation itself stays free and unlimited across all their tiers (as it should for an MIT desktop app), but everything resembling "team/organizational transcription platform" — the category OpenTranscribe actually competes in — is gated behind their paid tiers, and still doesn't include benchmarked diarization accuracy or hybrid search at any price.

---

## Appendix: file/function citations for this report

- Transcription: `src/helpers/whisperServer.js` (spawn, args, HTTP POST, `--no-timestamps` rationale), `src/helpers/audioManager.js` (`MediaRecorder` batch path, `AudioWorkletProcessor` streaming path), `src/helpers/ffmpegUtils.js` (WAV conversion), `scripts/download-whisper-cpp.js`.
- Diarization: `src/helpers/diarization.js` (`diarize()`, `mergeWithTranscript()`, `capSpeakerClusters()`, `convertRawPcmToWav()`), `src/helpers/liveSpeakerIdentifier.js` (live clustering, profile matching), `src/helpers/database.js` (`speaker_profiles`/`speaker_mappings` schema, EMA update), `scripts/download-sherpa-onnx.js`.
- Packaging: `package.json` (`build`/`prebuild:*`/`download:*` scripts, `electron-builder.json` `files` allowlist), `src/helpers/modelManagerBridge.js` (runtime model downloads).
- Search/chat: `src/helpers/qdrantManager.js`, `scripts/download-qdrant.js`, `src/workers/onnxWorker.js`.
- License: `LICENSE` (MIT, OpenWhispr Team, 2024).
- Timestamp mechanism: `src/helpers/whisperServer.js:151` (`--no-timestamps`), `src/helpers/audioManager.js:1077` (`Date.now() - recordingStartTime` capture stamping), `src/helpers/diarization.js:471-472` (merge logic reading `seg.timestamp`).
- OpenTranscribe baseline: `docs/ARCHITECTURE.md`, `docs/combined-engine-design.md`, `docs/diarization-boundary-results/dataset-sweep.md` and `cloud-comparison.md`, `backend/app/services/asr/factory.py`, `backend/app/services/chat/`, `backend/app/services/search/`, `CHANGELOG.md [0.4.0] - 2026-03-22`.
- PyTorch/ONNX/TensorRT findings: `docs/upstream-patches/phase-6-2-lessons-learned.md`, `docs/upstream-patches/phase-6-3-spike-results.md`.
- OpenWhispr Cloud pricing: `openwhispr.com/pricing` (fetched 2026-08-09; pricing/tiers are external and may change — reverify before citing externally).
- Video support gap: `src/components/notes/UploadAudioView.tsx:1433` (accept list), `src/helpers/urlAudioDownloader.js:929-930` (`-x --audio-format`), `src/helpers/audioStorage.js` (`.webm`-only storage); OpenTranscribe contrast: `backend/app/services/storage_recovery_service.py`, `backend/app/services/media_download_service.py`, `frontend/src/components/VideoPlayer.svelte`, `PlyrMiniPlayer.svelte`.
- LLM provider inventory: `src/services/ai/inferenceProviders/*.ts` (OpenWhispr), `backend/app/services/llm_service.py:42` (`LLMProvider` enum, OpenTranscribe).
- Other gaps: MCP/API absence verified by repo-wide grep of `backend/app` and `docs/`; live-ASR absence verified via `backend/app/api/websockets.py`; custom vocabulary confirmed via `backend/app/api/endpoints/custom_vocabulary.py`; existing tracked issues checked via `gh issue list --repo attevon-llc/OpenTranscribe` (#365 Recall.ai meeting capture, #366 NVIDIA Parakeet/Canary).
