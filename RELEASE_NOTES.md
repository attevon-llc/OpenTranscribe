<!--
  v0.4.0 release notes — DRAFT awaiting approval.

  Review this file, then run the `gh release create` command at the bottom
  to publish. Nothing is published until that command runs.

  Referenced by: docs/RELEASE_PLAN_v0.4.0.md (Step 4 — Publish GitHub Release).
  This file is temporary; archive or delete it after release.

  This file mirrors the v0.4.0 section of CHANGELOG.md — if you edit
  CHANGELOG.md after reading this, update this file too.
-->

# v0.4.0 — Enterprise Auth, Native Pipeline, Neural Search & Security Hardening

A major release combining enterprise-grade authentication, a native transcription pipeline, neural search, GPU optimizations, cloud ASR providers, comprehensive speaker intelligence, a Progressive Web App, user groups & sharing, and a final frontend hardening sprint — all built from processing **1,400+ real-world recordings** over two months of development. **281 commits** since v0.3.3.

---

## 🔐 Enterprise Authentication

Four authentication methods that can run simultaneously, configured through the admin UI without restarts:

- **Local** — Username/password with bcrypt, TOTP MFA (RFC 6238 — Google Authenticator, Authy, Microsoft Authenticator), FedRAMP IA-5 password policies (complexity, history, expiration), NIST AC-7 account lockout with progressive thresholds
- **LDAP/Active Directory** — Enterprise directory integration with auto-provisioning and username-attribute mapping
- **OIDC/Keycloak** — OpenID Connect with federated identity, social login, and federated logout propagation
- **PKI/X.509** — Certificate-based mTLS authentication with OCSP/CRL revocation checking and super-admin local password fallback

**Plus:** per-IP and per-user rate limiting, audit logging in structured JSON/CEF format with OpenSearch integration, JWT refresh token rotation with concurrent session limits, and database-driven configuration with AES-256-GCM encryption at rest — all manageable from a Super Admin UI without restarts.

## ⚡ Native Transcription Pipeline (2× Faster)

Replaced the legacy WhisperX pipeline with a native engine built on faster-whisper's `BatchedInferencePipeline` + PyAnnote v4. Cross-attention DTW provides word timestamps during transcription — no separate alignment pass, no wav2vec2 dependency, and native word timestamps for all **100+ languages** (previously only ~42 via wav2vec2).

**Benchmark (3.3-hour podcast, RTX A6000):** 706s → **332s** — 2.1× faster

- **Unified pipeline** replaces the previous `parallel_pipeline` / `whisperx_service` split
- **User-configurable VAD** — Voice Activity Detection threshold and silence duration exposed as tunable settings
- **Word timestamp validation** — post-processing ensures monotonicity and prevents drift
- **GPU pipeline benchmarks** — 40.3× single-file realtime, 54.6× peak at concurrency=8, perfect linear scaling 1–12 workers
- **TF32 acceleration** enabled at worker startup and after diarization (Ampere+ GPUs)

## 🎙️ PyAnnote v4 Migration & Speaker Intelligence

- **Automatic migration system** — Admin UI with real-time progress bar migrates speaker embeddings from v3 (512-dim) to v4 (256-dim) via atomic alias swap, zero downtime
- **Speaker overlap detection** — Identifies overlapping speakers with confidence scoring
- **Speaker pre-clustering** — GPU-accelerated cross-file speaker grouping (#144)
- **Global Speaker Management page** — Dedicated page for cross-file speaker profile management with avatars
- **Gender classification** — Apache 2.0 licensed neural network predicts gender from voice; stored on profiles for cross-video consistency
- **Gender-informed cluster validation** — Cross-gender cluster assignments require higher similarity thresholds; minority members flagged for review
- **Speaker metadata parsing** — Cross-reference pipeline with metadata hints display for LLM-assisted speaker identification (#141)
- **Jump-to-timestamp links** in the speaker editor (#147)
- **Unassign & blacklist** — Remove speaker assignments and blacklist erroneous profiles
- **Outlier analysis** — Detect and flag outlier embeddings in speaker clusters
- **Inline audio playback** — Play/pause toggle in speaker cluster views
- **OpenSearch cosine score fix** — All 8 kNN score read locations now correctly convert `(1+cos)/2` → raw cosine
- **Warm model caching** eliminates 40-60s cold-start delays by pre-loading models on startup

## 🔍 Hybrid Neural Search

Full-text BM25 combined with semantic vector search via **OpenSearch ML Commons**. Search for "budget discussion" and find segments about "financial planning" even when those exact words never appear.

- **ML Commons integration** — Native OpenSearch neural search, server-side embeddings
- **RRF hybrid merging** — BM25 + vector scores combined via Reciprocal Rank Fusion
- **6 embedding model tiers** — from 384-dim MiniLM (fast) to 768-dim mpnet (best quality)
- **Hybrid search crash fix** — Previously silent fallback to BM25-only on OpenSearch 3.4 due to `ArrayIndexOutOfBoundsException` when combining `aggs` + `hybrid` + `collapse` + RRF
- **Soft demotion instead of hard suppression** — Semantic results no longer dropped
- **Dynamic over-fetch** — Cap raised from 200 to 1,000 via `SEARCH_MAX_OVERFETCH` for large indexes
- **BM25 tuning** — Fuzziness AUTO, cross-fields, phrase slop, rank constant tuned 40→30
- **Stop/cancel reindex** — Admin UI can cancel in-flight reindex operations
- **Offline/airgapped model downloading** for air-gapped deployments
- **Dynamic model management** via admin UI

## ☁️ Cloud ASR Providers

For deployments without a GPU — 8 cloud speech providers plus cloud diarization (#150):

- **Providers**: Deepgram, AssemblyAI, OpenAI Whisper API, Google, AWS Transcribe, Azure Speech, Speechmatics, Gladia
- **pyannote.ai cloud diarization** integration
- **Independent diarization provider architecture** — `diarization_source` selector: ASR built-in, local PyAnnote GPU, pyannote.ai cloud, or off — independent of transcription provider choice
- **API-lite deployment mode** — 2 GB CPU-only image vs. 8.9 GB for the full GPU image. Cloud-transcribed files still get local speaker embedding extraction for cross-file matching
- **Custom vocabulary** — Domain-specific hotwords (medical, legal, corporate, government) used as faster-whisper hotwords and cloud provider keyword boosting
- **Admin-pinned ASR model** — Admins control local Whisper model selection; model loaded once at startup, shared across all workers
- **Per-transcription model override** — Users can override the admin-pinned model per upload (#153)

## 🤝 User Groups, Collection Sharing & Collaboration

- **User Groups & Collection Sharing** (#148) — Create user groups and share collections with groups or individual users; granular viewer/editor permissions
- **Speaker profile sharing** via the collection sharing infrastructure
- **Config/prompt sharing** — Share LLM configs, prompts, media sources, and organization contexts between users
- **Per-collection AI prompts** (#146) — Different summarization styles for different collection types
- **Bidirectional prompt-collection links** — Prompts show which collections use them
- **Organization context** (#142) — Inject domain knowledge into all LLM prompts for context-aware summaries

## 📤 Upload & Media

- **TUS 1.0.0 resumable uploads** (#10) — Chunked uploads with MinIO multipart storage that survive network interruptions
- **Collection & tag selection at upload** (#145) — Organize files during upload, not after
- **URL download quality settings** (#122) — Configure video resolution, audio-only mode, and bitrate for yt-dlp downloads
- **File retention & auto-deletion** (#134) — Admin-configurable file retention with automatic deletion and GDPR-compliant audit logging
- **Auto-labeling** (#140) — AI suggests tags and collections from transcript content with fuzzy deduplication
- **Disable AI summary per upload** (#152)
- **Disable speaker diarization per upload** (#151)
- **Selective reprocessing** (#143) — Stepper UI to re-run specific pipeline stages on existing files
- **YouTube bot-bypass** — 2026 yt-dlp best practices (Deno JS runtime, client rotation, proper headers) for 1,800+ supported platforms

## 🛡️ Frontend Hardening Sprint

A dedicated audit sprint shipped in this release. Full details below under "Security", but the highlights:

- **Flash of Authenticated Content (FOAC) fix** — Layout now gates protected content during async auth verification
- **Centralized user state cleanup** (`lib/session/clearUserState.ts`) — 17+ stores, caches, and localStorage keys cleared on every login/logout
- **Session-scoped `AbortController`** cancels in-flight requests on logout
- **bfcache invalidation** — Back button after logout forces reload to discard restored snapshots
- **DOMPurify sanitization** across 8 `{@html}` render sites; replaces a bypassable regex sanitizer
- **Production source maps disabled**
- **Keycloak redirect URL validation**

## 🎨 UX & Frontend Polish

- **Upload modal redesign** — Replaced the 4,603-line monolith with a 6-step linear stepper (Media → Tags → Collections → Speakers → Options → Submit) plus a conditional Extract step for large videos. All three upload sources (file/URL/recording) now share steps 2-6. "Remember previous values" and "Review with defaults" shortcuts for power users
- **Skeleton loaders** — Replace generic spinners on home gallery, search results, file detail, and speaker clusters/profiles/inbox (~20% faster perceived load per Nielsen Norman research)
- **Gallery click feedback** — Instant press state + mousedown prefetch (~50-100ms head start)
- **Gallery redesign** — Compact Apple-like grid cards, list view, sorting, multi-select bulk actions
- **Gallery state persistence** — Filters persist across file detail navigation; scroll position restored on back
- **Collection & Share modal polish** — Intro text, permission reference cards, empty states, backdrop-click data-loss protection
- **Manage Collections visual fix** — Eliminated the "card in a card" glitch
- **Settings redesign** — Tabbed navigation, per-user preferences, speaker behavior defaults
- **Queue Dashboard** — Unified tasks view (formerly File Status) with quick filters, DatePicker, and pagination
- **Stepper reprocess UI** — Step-by-step reprocessing with stage picker
- **Gallery action consolidation** — Action buttons moved to header with dropdown groups (#139)
- **Multi-select with auto-filter and title normalization**
- **Unified color system** — Gallery toolbar replaced a 7-color rainbow (blue, purple, green, amber, red, gray, purple) with a consistent 2-color system per Apple HIG: primary blue for main actions, surface/gray for secondary actions, red for destructive only. All purple removed from buttons and badges across 9 components. New `--ai-accent-color` CSS variable replaces hardcoded purple. Dark mode hover direction fixed (was lightening instead of darkening)

## 📱 Progressive Web App & Mobile

- **Installable PWA** (#155) — 15+ mobile fixes shipped as a comprehensive overhaul
- **2-column mobile grid**, hamburger navigation, full-screen modals, scroll locking, touch-optimized UI
- **iPad/iOS responsive layout fixes** — widened tablet breakpoints to 1200px for iPad landscape
- **Mobile settings navigation** redesigned with dropdown selector
- **Background page scroll lock** under all modals
- **44×44pt touch targets** throughout (Apple HIG compliance)

## ⚙️ Infrastructure, Monitoring & Performance

- **3-stage Celery pipeline** — Preprocess (CPU) → Transcribe+Diarize (GPU) → Postprocess (CPU) across separate queues so the GPU never idles waiting on CPU work
- **Flower monitoring upgrade** — Industry-standard Celery/Flower integration with persistent task history, queue visibility, worker status
- **Multi-GPU stats with stepper UI** — Real-time per-GPU stats display
- **GPU concurrent model sharing** — NVML profiling, PyAnnote embedding batch optimization (upstream contributions to PyAnnote and WhisperX)
- **273× faster WhisperX speaker assignment** — Replaced O(n×m) linear scan with interval tree + NumPy vectorized ops (10.2s → 0.037s for a 3-hour file). Contributed upstream.
- **Dual-model transcription architecture** — CPU lightweight + GPU primary for workload flexibility
- **Embedded documentation container** — New `opentranscribe-docs` Docusaurus site served at `/docs/` through NGINX proxy; fully offline-capable for air-gapped deployments
- **Progressive Web App service worker** with versioned cache purging
- **Codebase modularization** — 9 new shared backend modules, 6 new UI components, speaker task splits, dead code removal
- **Default Whisper model** changed from `large-v2` to `large-v3-turbo` (6× faster); use `large-v3` for translation or maximum accuracy
- **Intelligent batch sizing** based on available VRAM
- **Alembic-only database bootstrapping** — Linearized migration chain after branch merges
- **Configurable TXT export** — Persistent export preferences including speaker grouping

## 🔒 Security

A comprehensive security hardening pass alongside the feature work:

### Infrastructure Hardening
- **CSP headers**, private MinIO buckets, AES-256-GCM encryption at rest
- **Non-root containers** throughout the backend and frontend images
- **FIPS 140-3 readiness** documentation for government deployments
- **Apt/apk upgrade** on all runtime stages to pull latest base OS patches
- **Hadolint + Trivy + Grype + Dockle + SBOM** scan pipeline integrated into `docker-build-push.sh`

### Frontend Session Hardening
- **Flash of Authenticated Content (FOAC) fix** — `+layout.svelte` now gates all protected content behind `authReady && isAuthenticated && !isPublicPath`. Previously, unauthenticated users hitting `/` briefly saw the gallery slot render before the redirect, leaking ~1-2 frames of protected UI and triggering `/files` API calls
- **Centralized user state cleanup** — New `lib/session/clearUserState.ts` is the single source of truth for session teardown. Clears 17+ subsystems on every login/logout transition: toast, websocket, uploads, gallery filters, search results, sharing, LLM status, settings modal, transcript, groups, downloads, notifications, recording (with media track cleanup), thumbnail cache, media URL cache, speaker colors, plus user-scoped localStorage keys. Preferences (theme, locale, view mode, recording settings) are explicitly preserved
- **Session-scoped request cancellation** — `AbortController` in `lib/axios.ts` attached to every request via interceptor (except auth endpoints). `logout()` calls `abortAllRequests()` before `clearUserState()`, closing the race window where a late response could repopulate a cleared store
- **bfcache invalidation on back button** — Listens for `pageshow` events with `event.persisted === true` and forces `window.location.reload()`, preventing previously-protected pages from being restored from memory on shared devices
- **Toast cross-session leak fixed** — `toastStore.clear()` called from every login success path (local, Keycloak, PKI, MFA) and from `logout()`
- **Keycloak redirect URL validation** — `loginWithKeycloak()` parses and validates the authorization URL protocol (`http:`/`https:` only) before redirecting

### XSS Hardening
- **DOMPurify-backed HTML sanitization** — New `lib/utils/sanitizeHtml.ts` with strict tag whitelist. Added `dompurify` as a dependency
- **Defense-in-depth across 8 `{@html}` render sites** — TopicsList, TranscriptDisplay, TranscriptModal, SearchTranscriptModal, SearchOccurrence, SearchResultCard, SummaryDisplay
- **Bypassable regex sanitizer replaced** — The previous `SearchOccurrence` sanitizer `html.replace(/<(?!\/?mark[\s>])[^>]*>/g, '')` was bypassable via `</mark><script>...</script><mark>` payloads (regex only matched opening tags)

### Build & Config
- **Production source maps disabled** — `sourcemap: mode !== 'production'` prevents shipping variable names, API endpoints, and business logic to DevTools viewers
- **Defense-in-depth home page guard** — `routes/+page.svelte` early-returns if unauthenticated

## 🌍 Internationalization

- **8 UI languages**: English, Spanish, French, German, Portuguese, Chinese, Japanese, Russian
- **AI summary output in 12 languages**
- Full i18n compliance audit — added missing translations across all locales

## 🚢 How to Update

### Docker Compose

```bash
docker compose pull
docker compose up -d
```

After upgrading, **hard-reload the frontend** (Ctrl+Shift+R / Cmd+Shift+R) to pick up the new service worker and clear stale cached assets.

Alembic migrations run automatically on startup — no manual database changes. All existing data is preserved.

### Management Script

```bash
./opentr.sh stop && ./opentr.sh start prod
```

### To enable new authentication methods

1. Log in as super admin
2. Navigate to Settings → Authentication
3. Enable desired methods (LDAP, Keycloak, PKI)
4. Configure each in its dedicated section

### Optional: PyAnnote v4 migration

To enable speaker overlap detection and improved performance:

1. Navigate to Settings → Embeddings
2. Click "Migrate to PyAnnote v4"
3. Monitor progress with the real-time progress bar (no restart required)

### Optional: reclaim disk space

The wav2vec2 alignment model is no longer used (~360 MB):

```bash
rm -rf ${MODEL_CACHE_DIR:-./models}/torch/hub/checkpoints/wav2vec2_*
```

Existing word-level timestamps are preserved — no reprocessing needed.

### Optional: clean up deprecated env vars

```bash
# These can be safely removed from .env:
# ENABLE_ALIGNMENT=true        (alignment is now always-on natively)
# TRANSCRIPTION_ENGINE=whisperx (single unified engine, setting ignored)
```

## 📝 Breaking Changes

- **Authentication Configuration**: Auth settings now configured via Super Admin UI (Settings → Authentication) instead of environment variables. Database configuration takes precedence if set.
- **PyAnnote Migration**: Existing installations may optionally migrate speaker embeddings to v4 for overlap detection and improved voice matching.
- **wav2vec2 Alignment Model Removed**: Word-level timestamps are now native. `ENABLE_ALIGNMENT` and `TRANSCRIPTION_ENGINE` env vars are deprecated and silently ignored.
- **Removed Python modules**: `whisperx_service.py`, `parallel_pipeline.py`, `pyannote_compat.py`, `fast_speaker_assignment.py`, `batched_alignment.py` — functionality merged into the unified pipeline.

## 🐛 Selected Bug Fixes

- Hybrid search silently falling back to BM25-only due to OpenSearch 3.4 crash — **fixed**
- OpenSearch cosine similarity scores now correctly converted from `(1+cos)/2` to raw cosine
- Speaker profile centroid embeddings now correctly averaged across all constituent embeddings
- GPU memory leaks — CPU worker CUDA context initialization, prefork child VRAM leak, warm cache gating
- HuggingFace gated model authentication for PyAnnote diarization
- 18 N+1 query patterns and ORM hydration waste fixed across services and tasks
- Login flicker, empty-state flash, and navigation glitches eliminated
- YouTube 2026 bot-bypass (Deno JS runtime, client rotation)
- Admin bypass and shared editor access across all API endpoints
- Alembic migration chain linearized after branch merges
- LDAP user bcrypt crash when verifying non-local passwords
- WebSocket notification queue, upload queue, and previous-upload-values localStorage leaks on logout
- Dropdown clipping in upload modal
- Nested card visual glitch in Manage Collections modal
- Debug console.logs removed from production code
- Dead code removed (`Tasks.svelte.old`, unused `AudioExtractionModal.svelte`)
- Avatar lazy-loading on Speakers page
- Dark mode hover direction — `--primary-hover` was lighter than `--primary-color`, making buttons appear to deactivate on hover. Fixed to darken consistently across both themes

## 👥 Contributors

Special thanks to the community members whose code and feedback shaped this release:

**Code contributors:**
- **[@vfilon](https://github.com/vfilon)** (Vitali Filon) — Authored the entire LDAP/Active Directory authentication feature (PR #117, 9 commits): auth engine, username attribute support, `auth_type` handling, password change restrictions for non-local users, conditional settings UI, documentation, and migration detection logic. Foundation of the enterprise auth system.
- **[@imorrish](https://github.com/imorrish)** (Ian Morrish) — Submitted PR #117 upstream; contributed the Postgres password reset guide to the troubleshooting docs.

**Feature requests and bug reports that shipped in this release:**
- **[@imorrish](https://github.com/imorrish)** — Scrollable speaker dropdown (#129), filename in AI summary template (#138), collection/tag selection at upload (#145), per-collection default AI prompt (#146)
- **[@it-service-gemag](https://github.com/it-service-gemag)** — Disable diarization per upload (#151), disable AI summary per upload (#152), per-transcription Whisper model selection (#153)
- **[@Politiezone-MIDOW](https://github.com/Politiezone-MIDOW)** — File retention and auto-deletion system (#134)
- **[@coltrall](https://github.com/coltrall)** — Docker daemon detection fix in the installation script (#137)
- **[@SQLServerIO](https://github.com/SQLServerIO)** (Wes Brown) — Pagination for large transcripts, fixing file detail page hang with long recordings (#109)

Thank you to everyone who filed issues, tested pre-releases, and shared their use cases — your feedback directly drives what gets built.

## 📚 Full Details

- **Full changelog**: [CHANGELOG.md](https://github.com/davidamacey/OpenTranscribe/blob/master/CHANGELOG.md)
- **Blog post**: [The story behind v0.4.0](https://docs.opentranscribe.app/blog/v0.4.0-release)
- **Commits since v0.3.3**: [v0.3.3...v0.4.0](https://github.com/davidamacey/OpenTranscribe/compare/v0.3.3...v0.4.0) (281 commits)
- **Docker images**: `davidamacey/opentranscribe-backend:v0.4.0` and `davidamacey/opentranscribe-frontend:v0.4.0` on Docker Hub

---

<!--
  ═══════════════════════════════════════════════════════════════════════════
  PUBLISH COMMAND — run this only after you've reviewed the notes above.
  ═══════════════════════════════════════════════════════════════════════════

  Preconditions (already verified):
    ✓ Master pushed to origin (7f67f4e)
    ✓ v0.4.0 tag pushed to origin, points to 7f67f4e
    ✓ Docker Hub has fresh davidamacey/opentranscribe-{backend,frontend}:v0.4.0
    ✓ Security scans committed in 7f67f4e
    ✓ CHANGELOG.md and blog post updated
    ✓ docs-site/ builds cleanly

  Run from repo root:

      gh release create v0.4.0 \
        --title "v0.4.0 — Enterprise Auth, Native Pipeline, Neural Search & Security Hardening" \
        --latest \
        --notes-file RELEASE_NOTES.md

  Note: `--latest` flag demotes v0.3.3 from the "Latest release" badge.
  Note: NO `--draft` flag — per RELEASE_PROCESS.md, releases publish immediately.

  After running the command:
    1. Verify: gh release list | head -3
    2. Verify: https://github.com/davidamacey/OpenTranscribe/releases/tag/v0.4.0
    3. Archive this file: git rm RELEASE_NOTES.md && git commit -m "chore: archive v0.4.0 release notes after publish"
-->
