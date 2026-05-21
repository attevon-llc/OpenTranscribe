# Changelog

All notable changes to OpenTranscribe will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **CPU-only install flag (`--cpu`)**: `setup-opentranscribe.sh`, `opentranscribe.sh`, and `opentr.sh` now accept a `--cpu` flag (and honor `OPENTRANSCRIBE_FORCE_CPU=1` for unattended installs) to explicitly opt out of the GPU compose overlay. Required on hosts where the NVIDIA Container Toolkit is detected by Docker but GPU passthrough is non-functional — e.g. WSL2 without a WSL-capable Windows NVIDIA driver, where auto-detection would otherwise enable the GPU overlay and cause celery-worker / celery-cpu-worker to fail at container start with `nvidia-container-cli: initialization error: WSL environment detected but no adapters were found`. The choice is persisted to `.env` as `FORCE_CPU_MODE=true`, so `./opentranscribe.sh start/restart/stop` continues to skip the GPU overlay without re-passing the flag. Default behaviour for GPU users is unchanged.
- **CPU-mode safe defaults and visibility**: builds on the `--cpu` flag with end-to-end CPU-mode awareness. The installer now writes `ENABLE_DIARIZATION=false` to `.env` whenever `DETECTED_DEVICE=cpu` (PyAnnote requires CUDA), prints a "CPU-Only Mode — Performance Notes" advisory in the install summary, and leaves the existing `select_whisper_model()` recommendation of `base` for CPU intact. The backend logs a single startup warning when a worker boots in CPU mode with a heavyweight Whisper model or diarization enabled. `GET /system/stats` now returns `device_mode`, `force_cpu_mode`, `whisper_model`, and `diarization_enabled` so the Settings → System Statistics panel renders a CPU-only advisory banner with the right "forced via flag" vs "no GPU detected — automatic fallback" subtitle. All 8 UI locales (en, es, fr, de, pt, ru, zh, ja) include the new strings. End-to-end testing plan documented at `docs/CPU_MODE_TESTING.md`.

## [0.4.1] - 2026-04-14

### Overview

Patch release fixing LDAP group filtering for Active Directory Distinguished Names (issue #188) and adding Keycloak-as-PKI-broker compliance for government/FedRAMP deployments.

### Fixed

- **LDAP group DN parsing** ([#188](https://github.com/davidamacey/OpenTranscribe/issues/188)): Group lists containing full Active Directory DNs (e.g. `CN=Whisper_Users,CN=Users,DC=domain,DC=local`) were silently broken because the code split on commas — which are structural delimiters inside DNs. Group lists now use **semicolons** as the multi-group separator. A single full DN with no semicolons is treated as one group correctly. Existing simple group names (no `=` characters) continue to work unchanged.
- **PKI admin DN parsing**: `PKI_ADMIN_DNS` suffered the same comma-split bug. Fixed to use semicolon-delimited parsing via the same shared helper.
- **Government cert display name**: Government X.509 certificates carry space-separated CNs in the form `LastName FirstName emailusername`. `extract_display_name_from_gov_dn()` now parses this 3-token format and renders it as `First Last`.

### Added

- **Keycloak-as-PKI-broker support**: When Keycloak acts as the X.509/PKI broker (government CAC/PIV deployments), cert claims injected into the OIDC token are now extracted and stored on the user record. Both short claim names (`cert_dn`, `cert_serial`) and Keycloak's `x509_cert_*` aliases are handled automatically.
- **PKI admin promotion via Keycloak**: Users authenticating through Keycloak with a cert DN listed in `PKI_ADMIN_DNS` are promoted to admin even if they lack the Keycloak realm role — matching the standalone PKI auth behaviour.
- **Documentation**: New "Government / FedRAMP: Keycloak as X.509 PKI Broker" section in `docs/KEYCLOAK_SETUP.md` covering authenticator setup, cert claim mapping table, DN format, and `PKI_ADMIN_DNS` configuration.

### Upgrade Notes

- **LDAP group list format change** — If you previously used comma-separated group names that happened to work (e.g. `GroupA,GroupB` where neither name contained `=`), update to semicolons: `GroupA;GroupB`. Full AD DNs **must** use semicolons: `CN=Group1,DC=domain,DC=local;CN=Group2,DC=domain,DC=local`.
- `PKI_ADMIN_DNS` also switches to semicolon delimiters if you have multiple DNs.
- No database migrations required.

## [0.4.0] - 2026-03-22

### Overview

Major release combining enterprise-grade authentication, native transcription pipeline, neural search, GPU optimizations, cloud ASR providers, comprehensive speaker intelligence, Progressive Web App support, a frontend security hardening sprint, and dozens of features built from processing 1,400+ real-world recordings over two months of development (281 commits). This release significantly improves security, performance, search capabilities, and mobile usability.

### Added

#### Enterprise Authentication System
- **Multi-Method Authentication**: Support for 4 simultaneous authentication methods:
  - Local authentication with bcrypt hashing
  - LDAP/Active Directory integration with auto-provisioning
  - OIDC/Keycloak with identity federation and social login
  - PKI/X.509 certificate authentication with OCSP/CRL revocation checking
- **Super Admin Configuration UI** - Comprehensive settings interface for managing authentication methods without restart
- **Multi-Factor Authentication (MFA)** - RFC 6238 compliant TOTP with Google Authenticator, Authy, Microsoft Authenticator compatibility
- **Password Policies** - FedRAMP IA-5 compliant password requirements with complexity, history, and expiration
- **Account Lockout** - NIST AC-7 compliant protection with configurable failed attempt thresholds and progressive lockout
- **Rate Limiting** - Per-IP and per-user rate limiting for authentication and API endpoints
- **Audit Logging** - Comprehensive authentication audit trail in structured JSON/CEF format with OpenSearch integration
- **Session Management** - JWT token-based sessions with refresh token rotation and concurrent session limits
- **Database-Driven Configuration** - All auth settings stored encrypted (AES-256-GCM) in database, accessible via admin UI

#### PyAnnote v4 Migration & Optimization
- **Automatic Migration System** - Admin UI for seamless migration from PyAnnote v3 to v4 with progress tracking
- **Speaker Overlap Detection** - Identifies and visualizes overlapping speakers with confidence scoring
- **Warm Model Caching** - Eliminates 40-60 second cold-start delays by pre-loading models on startup
- **Fast Speaker Assignment** - Efficient speaker assignment using WhisperX's built-in speaker mapping
- **Flexible Embedding Mode** - Per-file toggle between PyAnnote v3, v4, or auto-detection
- **Native Word-Level Timestamps** - Always-on word-level timestamps for all 100+ languages via cross-attention DTW (no separate alignment model needed)
- **Asynchronous Embedding Extraction** - Non-blocking speaker embedding processing

#### OpenSearch Native Neural Search
- **ML Commons Integration** - Native OpenSearch neural search using ML Commons plugin
- **Server-Side Embeddings** - Embedding generation moved from client to server for better performance
- **Hybrid Search** - Combines BM25 full-text with neural semantic search using RRF merging
- **Model Registry** - 6 embedding models organized by quality tier (smallest/fastest to largest/most accurate)
- **Offline/Airgapped Support** - Model downloading scripts for environments without internet access
- **Dynamic Model Management** - Add/remove embedding models via admin UI

#### Unified Transcription Pipeline
- **Native Word-Level Timestamps** - Word timestamps now provided natively by faster-whisper cross-attention DTW for all 100+ languages (previously only ~42 languages via wav2vec2 alignment)
- **Unified Pipeline** - Single streamlined transcription pipeline replaces the previous parallel_pipeline/whisperx_service split
- **User-Configurable VAD Settings** - Exposed Voice Activity Detection threshold and minimum silence duration as user-tunable settings
- **Word Timestamp Validation** - Post-processing validation and correction of word-level timestamps to prevent drift and ensure monotonicity

#### Performance Improvements
- **Default Model Change** - Switched from large-v2 to large-v3-turbo (6x faster transcription)
  - Note: large-v3-turbo cannot translate; use large-v3 for translation needs
- **Batch Size Optimization** - Intelligent batch sizing based on available VRAM
- **Neural Model Endpoints** - RESTful API for model lifecycle management
- **GPU Memory Leak Fixes** - Gated model preloading with `PRELOAD_GPU_MODELS` env var to prevent 15 GB CPU worker leak; forced CPU for speaker clustering under 500 speakers to prevent 44 GB prefork child leak
- **Vectorized Speaker Assignment** - NumPy matmul replaces O(n×m) linear scan, 13x speedup (80s → 6s for 4.7-hour files)
- **TF32 Acceleration** - Enabled at worker startup and after diarization for Ampere+ GPUs
- **GPU Pipeline Benchmarks** - 40.3x single-file realtime, 54.6x peak at concurrency=8, perfect linear scaling 1–12 workers on RTX A6000

#### Cloud ASR Providers
- **Multi-Provider Cloud ASR** - 8 cloud speech providers: Deepgram, AssemblyAI, OpenAI Whisper API, Google, AWS Transcribe, Azure Speech, Speechmatics, Gladia (#150)
- **pyannote.ai Integration** - Cloud diarization via pyannote.ai API (`/v1/diarize`)
- **Independent Diarization Provider Architecture** - `diarization_source` selector with four modes: ASR built-in, local (PyAnnote GPU), pyannote.ai cloud, or off — independent of transcription provider choice
- **API-Lite Deployment Mode** - CPU-only image (~2 GB vs 8.9 GB) for organizations without GPUs; cloud-transcribed files still get local speaker embedding extraction for cross-file matching
- **Custom Vocabulary** - Domain-specific hotwords (medical, legal, corporate, government) used as faster-whisper hotwords and cloud provider keyword boosting
- **Admin-Pinned ASR Model** - Admins control local Whisper model selection; model loaded once at startup, shared across all workers; per-user override removed
- **Per-Transcription Model Selection** - Users can override the admin-pinned model per upload (#153)

#### Speaker Intelligence
- **Speaker Pre-Clustering** - GPU-accelerated speaker clustering groups speakers across files based on voice similarity (#144)
- **Global Speaker Management Page** - Dedicated page for cross-file speaker profile management
- **Gender Classification** - Neural network gender prediction from voice characteristics using Apache 2.0 licensed model; results stored on profiles for cross-video consistency
- **Gender-Informed Cluster Validation** - Cross-gender cluster assignment requires higher similarity threshold; minority-gender members flagged for review
- **Speaker Profile Avatars** - Avatar images for speaker profiles
- **Jump-to-Timestamp Links** - Speaker editor includes links to timestamps in transcript (#147)
- **Speaker Metadata Parsing** - Cross-reference pipeline with metadata hints display for LLM-assisted speaker identification (#141)
- **Unassign and Blacklist** - Remove speaker assignments and blacklist erroneous profiles
- **Outlier Analysis** - Detect and flag outlier embeddings in speaker clusters
- **Play/Pause Toggle** - Inline audio playback in speaker cluster views
- **OpenSearch Cosine Score Fix** - OS `cosinesimil` returns `(1+cos)/2`; all 8 kNN score read locations now convert to raw cosine (`2.0 * score - 1.0`)
- **Profile Embedding Fix** - `add_speaker_to_profile_embedding` now delegates to `update_profile_embedding` for correct centroid averaging

#### Search Improvements
- **Hybrid Search Overhaul** - Fixed OpenSearch 3.4 `ArrayIndexOutOfBoundsException` crash when using `aggs` + `hybrid` + `collapse` + RRF pipeline (was silently falling back to BM25-only)
- **Score Gate Removed** - Replaced hard suppression with soft demotion (`_apply_semantic_demotion`); semantic results no longer dropped
- **Dynamic Over-Fetch** - Cap raised from 200 to 1000 via `SEARCH_MAX_OVERFETCH` env var for large indexes
- **BM25 Improvements** - Fuzziness AUTO, cross-fields, phrase slop; rank_constant 40→30
- **Stop/Cancel Reindex** - Cancel in-flight reindex operations from Admin UI (#5994)
- **Search Reliability** - Word-boundary regex for RRF collapse fallback; synthetic highlights for semantic results

#### Collaboration & Sharing
- **User Groups & Collection Sharing** - Create user groups and share collections with groups or individual users (#148)
- **Speaker Profile Sharing** - Share speaker profiles via collection sharing infrastructure
- **Config/Prompt Sharing** - Share LLM configs, prompts, media sources, and org contexts between users
- **Per-Collection AI Prompts** - Different AI summarization prompts for different collections (#146)
- **Bidirectional Prompt-Collection Links** - Prompts show linked collections on their cards

#### Upload & Media
- **TUS 1.0.0 Resumable Uploads** - Resumable chunked uploads with MinIO multipart storage; survives network interruptions (#10)
- **Collection & Tag Selection at Upload** - Select collections and tags during file upload (#145)
- **URL Download Quality Settings** - Configure video resolution, audio-only mode, and bitrate for yt-dlp downloads (#122)
- **File Retention / Auto-Deletion** - Admin-configurable file retention with automatic deletion (#134)

#### Export & Settings
- **Configurable TXT Export** - Persistent export preferences including speaker grouping options
- **Disable AI Summary** - Option to skip AI summarization per upload (#152)
- **Disable Speaker Diarization** - Option to skip diarization per upload (#151)
- **Stepper Reprocess UI** - Step-by-step reprocessing with stage picker for selective pipeline stages (#143)
- **Organization Context** - Inject domain knowledge into all LLM prompts for context-aware summaries (#142)

#### Infrastructure & Monitoring
- **Flower Monitoring Upgrade** - Industry-standard Celery/Flower integration with persistent task history, queue visibility, and worker status
- **Multi-GPU Stats with Stepper UI** - Real-time per-GPU stats display with stepper interface
- **Resumable Upload Sessions** - TUS protocol session management in database
- **Progressive Web App (PWA) & Mobile Overhaul** - Installable PWA, 2-column mobile grid, hamburger nav, full-screen modals, scroll locking, touch-optimized UI (#155)
- **Security Hardening** - CSP headers, private MinIO buckets, AES-256-GCM encryption, non-root containers, FIPS 140-3 readiness
- **Auto-Labeling** - AI suggests tags and collections from transcript content with fuzzy deduplication (#140)
- **Codebase Modularization** - 9 new shared backend modules, 6 new UI components, speaker task splits, dead code removal
- **Embedded Documentation** - New `opentranscribe-docs` container serving the Docusaurus documentation site; accessible at `/docs/` through the app's NGINX proxy (and `http://localhost:3030/docs/` directly); fully offline-capable for air-gapped deployments

#### Authentication Additions (v0.3.3 integrated)
- **Keycloak Federated Logout** - Session termination propagates to Keycloak OIDC end-session endpoint (#125)
- **Super Admin PKI + Local Password Fallback** - PKI-authenticated super admins can retain local password as fallback (#127)

#### Upload Modal Redesign
- **6-Step Stepper Flow** - Replaced the accordion-inside-modal upload UX with a linear stepper: Media → Tags → Collections → Speakers → Options → Submit. Conditional Extract step appears automatically for large video files
- **Unified Across All Upload Sources** - File, URL, and recording uploads now share steps 2-6, so URL downloads and recordings gain full access to tags/collections/speaker settings (previously file-only)
- **Remember Previous Values** - Upload modal pre-fills tags, collections, speaker settings, whisper model, and skip-summary from the last upload. One-click "Review with defaults" shortcut lets power users jump straight to submit
- **Clickable Stepper Navigation** - Users can click any previously-visited step to go back and edit. Dot + label is a single clickable button per step (Fitts's Law / Apple HIG 44×44pt touch-target compliance)
- **Decomposed Monolith** - The 4,603-line `FileUploader.svelte` split into a 1,294-line coordinator plus 9 focused components under `frontend/src/components/upload/` (each under ~470 lines). New `upload-shared.css` provides a unified chip/dropdown pattern reused across tags and collections
- **Conditional Extraction Step** - Large video files (>100MB by default) trigger an inline Extract step with radio-button choice (Extract Audio Only vs Upload Full Video). Extraction runs on final Submit, not at selection time, so users can still change their mind while stepping through tags/collections
- **Backdrop-Click No Longer Closes** - Modal only closes via X button or Escape key, preventing data loss from stray clicks on in-progress upload state

#### Skeleton Loaders on Major Pages
- **Structural Loading States** - Replaced generic `<Spinner size="large">` on home gallery, search results, file detail page, and speaker clusters/profiles/inbox with skeleton components that mirror the final layout. Perceived load time ~20% faster per Nielsen Norman research
- **Reusable Skeleton Components** - New `FileDetailSkeleton.svelte` (full 2-column layout with header/video/transcript), `ui/CardGridSkeleton.svelte` (parametric with media/profile/search variants), and `ui/ListRowSkeleton.svelte` (avatar + title + actions rows)
- **Gallery Click Feedback** - Clicking a file card now dims + scales it instantly (opacity 0.72, scale 0.985) with `pointer-events: none` to prevent double-clicks. Prefetch kicks off on `mousedown` ~50-100ms before the click handler runs

#### Collection & Share Modal Polish
- **Help Text and Empty States** - Create/Edit Collection modals gained intro banners explaining what collections are, field hints with `maxlength` indicators, and proper `aria-labelledby` wiring
- **Universal Content Analyzer Default** - New collections auto-select the system-default prompt (via `is_system_default` lookup), matching the behavior users typically want without requiring manual selection
- **Share Modal Intro and Permission Guide** - Share Collection modal now includes an introductory explanation, a collection name banner with folder icon, and a visible permission-level reference card showing Viewer/Editor labels inline with descriptions (previously only in tooltips). Empty state added for collections with no existing shares
- **Manage Collections Visual Fix** - Fixed nested-card glitch where the inner `.collections-panel` had its own surface background inside the outer modal container, producing a visible "card in a card" look

#### Unified Color System
- **2-Color Toolbar** - Gallery toolbar replaced a 7-color rainbow (blue, purple, green, amber, red, gray, purple) with a consistent 2-color system per Apple HIG: primary blue for the main action (Upload, Process), surface/gray for all secondary actions (Collections, Select, Organize), red for destructive only (Delete)
- **Purple Removed from UI** - All purple button and badge colors (`#8b5cf6`, `#7c3aed`, `#a855f7`) replaced across 9 components: gallery toolbar, speaker cluster Split button, shared-permission badge, AI suggestion indicators, AI tag/collection chips, LLM analysis badge, and search source-speaker badge. Speaker diarization palette and FedRAMP CUI classification banner retain purple as intentional domain-specific colors
- **AI Accent Color Variable** - New `--ai-accent-color: var(--primary-color)` in `theme.css` replaces scattered purple `#a855f7` fallbacks. AI-suggested tags, collections, and LLM analysis indicators now inherit the primary blue through the CSS cascade
- **Dark Mode Hover Direction Fixed** - `--primary-hover` changed from `#93c5fd` (lighter) to `#3b82f6` (darker). Hover should always darken per Apple HIG — the old lighter hover made buttons appear to deactivate on interaction. Same fix applied to `--link-hover`

### Security

#### Frontend Session Hardening
- **Flash of Authenticated Content (FOAC) fix** - `+layout.svelte` now gates all protected content behind `authReady && isAuthenticated && !isPublicPath`, showing a loading screen in route-mismatch states while async redirects are in flight. Previously, unauthenticated users hitting `/` briefly saw the gallery slot render before the redirect fired, leaking ~1-2 frames of protected UI and triggering `/files` API calls
- **Centralized User State Cleanup** - New `frontend/src/lib/session/clearUserState.ts` is the single source of truth for session teardown. Clears 17+ subsystems on every login/logout transition: toast, websocket, uploads, gallery filters, search results, sharing, LLM status, settings modal, transcript, groups, downloads, notifications, recording (with media track cleanup), thumbnail cache, media URL cache, speaker colors, plus user-scoped localStorage keys. Preferences (theme, locale, view mode, recording settings) are explicitly preserved. Replaces ad-hoc cleanup previously scattered across `auth.ts`
- **Session-Scoped Request Cancellation** - Session-scoped `AbortController` in `lib/axios.ts` attached to every request via interceptor (except `/auth/login`, `/auth/logout`, `/auth/token/refresh` which must always complete). `logout()` now calls `abortAllRequests()` before `clearUserState()`, closing the race window where a late API response could repopulate a cleared store with stale data from the previous session. New `isRequestCancelled()` helper exported for catch blocks to suppress error toasts on cancelled requests
- **bfcache Invalidation on Back Button** - `+layout.svelte` now listens for `pageshow` events with `event.persisted === true` and forces `window.location.reload()` to discard the restored DOM/JS snapshot. Prevents users from hitting the back button after logout and seeing the previously-protected page restored from memory on shared devices
- **Toast Cross-Session Leak Fixed** - `toastStore.clear()` is called from every login success path (local, Keycloak callback, PKI, MFA) and from `logout()` via `clearUserState()`. Previously, notifications from User A's session could persist into User B's login screen or the next user's session
- **Keycloak Redirect URL Validation** - `loginWithKeycloak()` now parses and validates the `authorization_url` returned by `/auth/keycloak/login` (requires `http:` or `https:` protocol) before calling `window.location.href`. Prevents open-redirect or `javascript:`/`data:` URL injection if upstream config drifts

#### XSS Hardening
- **DOMPurify-Backed HTML Sanitization** - New `lib/utils/sanitizeHtml.ts` provides `sanitizeHighlightHtml()` (whitelist allows `mark`, `span`, `br`, `ul`, `li`, `em`, `strong`, `div`, `p` with `class` and `data-match-index` attributes) and `sanitizeToPlainText()`. Added `dompurify` and `@types/dompurify` as dependencies
- **Defense-in-Depth Across 8 Render Sites** - Wrapped every `{@html}` directive that renders API-sourced or LLM-generated content with `sanitizeHighlightHtml()`: TopicsList, TranscriptDisplay, TranscriptModal, SearchTranscriptModal, SearchOccurrence, SearchResultCard, SummaryDisplay
- **Bypassable Regex Sanitizer Replaced** - `SearchOccurrence.svelte` and `SearchResultCard.svelte` previously used `html.replace(/<(?!\/?mark[\s>])[^>]*>/g, '')` which was bypassable via `</mark><script>alert(1)</script><mark>` payloads (the regex only matched opening tags). Now uses DOMPurify with a strict tag whitelist

#### Build & Configuration Hardening
- **Production Source Maps Disabled** - `vite.config.ts` now uses `sourcemap: mode !== 'production'`, ensuring `.js.map` files are only generated for dev/preview builds. Previously, production builds shipped source maps exposing variable names, API endpoint URIs, error messages, and full business logic to any visitor via DevTools or automated crawlers
- **Defense-in-Depth Home Page Guard** - `routes/+page.svelte` `onMount` now early-returns if `!get(isAuthenticated)`, preventing `fetchFiles()` and WebSocket subscriptions from running if the component is somehow mounted unauthenticated (belt-and-suspenders beyond the layout-level route guard)

### Changed

- **Default Whisper Model** - Changed from `large-v2` to `large-v3-turbo` for significantly faster transcription with maintained accuracy
  - New default: `WHISPER_MODEL=large-v3-turbo` (6x faster, excellent for English and most languages)
  - For translation to English: Use `WHISPER_MODEL=large-v3` (large-v3-turbo cannot translate)
  - For maximum accuracy: Use `WHISPER_MODEL=large-v3` (slightly better accuracy than turbo)
- **PyAnnote Embedding Dimension** - v4 uses 256-dim embeddings instead of 192-dim for better voice matching
- **Speaker Embedding Storage** - Database schema updated to support v3/v4 dual-mode during migration
- **Authentication Configuration** - Moved from environment variables to database for better security and manageability
- **Model Caching** - Improved caching strategy with warm-start support and automatic prefetching
- **Word-Level Timestamps** - Now native for all 100+ languages via cross-attention DTW (previously only ~42 languages supported via wav2vec2 alignment model)
- **Transcription Pipeline** - Consolidated into a single unified pipeline; removed separate parallel pipeline and WhisperX service layer

### Removed

- **wav2vec2 Alignment Model** - No longer needed; word-level timestamps are now native via faster-whisper cross-attention DTW
- **`whisperx_service.py`** - Removed separate WhisperX service abstraction (functionality merged into unified pipeline)
- **`parallel_pipeline.py`** - Removed parallel pipeline module (replaced by unified pipeline)
- **`pyannote_compat.py`** - Removed PyAnnote compatibility shim
- **`fast_speaker_assignment.py`** - Removed custom speaker assignment utility (using WhisperX built-in assignment)
- **`batched_alignment.py`** - Removed batched alignment utility (alignment no longer needed)
- **`ENABLE_ALIGNMENT` env var** - Deprecated and ignored (alignment is always-on natively)
- **`TRANSCRIPTION_ENGINE` env var** - Deprecated and ignored (single unified engine)

### Breaking Changes

- **Authentication Configuration**: Auth settings now configured via Super Admin UI (Settings → Authentication) instead of environment variables. Database configuration takes precedence if set.
- **PyAnnote Migration**: Existing installations may need to migrate speaker embeddings for optimal overlap detection (optional but recommended)
- **wav2vec2 Alignment Model Removed**: The separate wav2vec2 alignment model is no longer used. Word-level timestamps are now provided natively by faster-whisper cross-attention DTW. The `ENABLE_ALIGNMENT` and `TRANSCRIPTION_ENGINE` environment variables are deprecated and silently ignored.

### Fixed

- Speaker overlap detection accuracy improved
- Neural search relevance and ranking improved (hybrid search was silently falling back to BM25-only due to OpenSearch 3.4 crash)
- Authentication rate limiting prevents brute force attacks
- PKI certificate validation with OCSP/CRL revocation checking
- OpenSearch cosine similarity scores now correctly converted from OS range `(1+cos)/2` to raw cosine
- Speaker profile centroid embeddings now correctly averaged across all constituent embeddings
- GPU memory leaks fixed (CPU worker CUDA context initialization, prefork child VRAM leak)
- HuggingFace gated model authentication for PyAnnote diarization
- Login flicker and empty-state flash on navigation eliminated
- YouTube bot-bypass anti-blocking with 2026 yt-dlp best practices (Deno JS runtime, client rotation)
- Admin bypass and shared editor access across all API endpoints
- Alembic migration chain linearized after branch merges
- LDAP user bcrypt crash when verifying non-local passwords
- **WebSocket notification queue leak** - `clearAll()` now called on logout; previously persisted in localStorage across sessions, exposing User A's notification history to User B on shared devices
- **Upload queue persistence leak** - `localStorage['upload_queue']` is now cleared on logout via new `uploadsStore.reset()`; previously leaked file UUIDs, metadata, and processing status across sessions
- **Dropdown clipping in upload modal** - Removed nested `overflow-y: auto` on the stepper body that was clipping tag and collection dropdowns. Primary modal container now handles all scrolling with `z-index: 200` on the dropdown list
- **Double-card visual in Manage Collections** - `.collections-panel` previously had its own `surface-color` background + border inside the outer modal container, producing a visible "card in a card" look. Root set to `background: transparent` when rendered inside the modal
- **Debug console.logs removed** - `AuthenticationSettings.svelte` no longer logs full auth config on every load; `files/[id]/+page.svelte` no longer logs every 5 minutes on video URL refresh
- **Dead code removed** - Deleted unused `routes/Tasks.svelte.old` (868 lines) and the unused `AudioExtractionModal.svelte` (replaced by inline stepper step)
- **Avatar lazy-loading** - Profile and cluster avatars on the Speakers page now use `loading="lazy"` and `decoding="async"`, preventing synchronous load-block on page init
- **Dark mode hover direction** - `--primary-hover` was lighter than `--primary-color` in dark mode (`#93c5fd` vs `#60a5fa`), making buttons appear to deactivate on hover. Fixed to `#3b82f6` (darker) for consistent interaction feedback across both themes

### Upgrade Notes

#### Standard Upgrade (Non-Breaking)

```bash
# Pull latest images
docker compose pull

# Restart services (automatically runs migrations)
docker compose up -d
```

After upgrading, users should **hard-reload the frontend** (Ctrl+Shift+R / Cmd+Shift+R) to pick up the new service worker and clear any stale cached assets. The service worker will automatically cache the new build on next visit.

The system will automatically detect the authentication configuration mode and function correctly. To use new authentication features:

1. Log in as super admin
2. Navigate to Settings → Authentication
3. Enable desired authentication methods
4. Configure each method in its dedicated section

#### PyAnnote v4 Migration (Optional)

To take advantage of new speaker overlap detection and improved performance:

1. Navigate to Settings → Embeddings
2. Click "Migrate to PyAnnote v4"
3. Monitor progress with the real-time progress bar
4. No restart required

#### Model Selection for Your Language

- **English audio**: Keep default `large-v3-turbo` for fastest transcription
- **Non-English (no translation needed)**: Keep default `large-v3-turbo` for 6x faster speed
- **Translation to English**: Switch to `large-v3` (turbo cannot translate)
  - In Settings → Transcription → Model Selection, choose `large-v3`
- **Maximum accuracy needed**: Switch to `large-v3` for best overall accuracy
  - In Settings → Transcription → Model Selection, choose `large-v3`

#### wav2vec2 Model Cache Cleanup (Optional)

The wav2vec2 alignment model is no longer used. You can reclaim ~360MB of disk space by removing it from your model cache:

```bash
# Remove wav2vec2 alignment model cache (~360MB)
rm -rf ${MODEL_CACHE_DIR:-./models}/torch/hub/checkpoints/wav2vec2_*
```

No reprocessing of existing transcriptions is needed -- existing word-level timestamps are preserved.

#### Environment Variable Cleanup (Optional)

The following environment variables are deprecated and silently ignored. You may remove them from your `.env` file:

```bash
# These can be safely removed from .env:
# ENABLE_ALIGNMENT=true        (alignment is now always-on natively)
# TRANSCRIPTION_ENGINE=whisperx (single unified engine, setting ignored)
```

### Contributors

Special thanks to the community members whose code contributions and issue reports shaped this release:

**Code Contributors:**
- [@vfilon](https://github.com/vfilon) (Vitali Filon) — Implemented the entire LDAP/Active Directory authentication feature (PR #117): initial auth engine, username attribute support, auth_type handling, password change restrictions for non-local users, conditional settings UI, documentation, and migration detection logic (9 commits)
- [@imorrish](https://github.com/imorrish) (Ian Morrish) — Submitted PR #117, contributed the Postgres password reset guide to the troubleshooting docs (PR #1)

**Issue Reports Implemented:**
- [@imorrish](https://github.com/imorrish) — #129 scrollable speaker dropdown, #138 filename in AI summary template, #145 collection/tag selection at upload, #146 per-collection default AI prompt
- [@it-service-gemag](https://github.com/it-service-gemag) — #151 disable diarization per upload, #152 disable AI summary per upload, #153 per-transcription Whisper model selection
- [@Politiezone-MIDOW](https://github.com/Politiezone-MIDOW) — #134 file retention and auto-deletion system
- [@coltrall](https://github.com/coltrall) — #137 Docker daemon detection in installation script
- [@SQLServerIO](https://github.com/SQLServerIO) (Wes Brown) — #109 pagination for large transcripts (file detail page hang with thousands of segments)

---

## [0.3.3] - 2025-01-13

### Overview
Community contributions release featuring Russian language support, protected media authentication for corporate video portals, and various bug fixes and improvements.

Special thanks to [@vfilon](https://github.com/vfilon) for contributing all four PRs in this release!

### Added

#### Internationalization
- **Russian Language Support** - Added Russian (Русский) as the 8th supported UI language (#114)
- **Protected Media Translations** - Added translations for protected media feature to all 7 non-English languages

#### Protected Media Authentication (#115)
- **Plugin Architecture** - New extensible plugin system for authenticated media downloads from corporate/internal video portals
- **MediaCMS Provider** - Built-in support for MediaCMS installations requiring authentication
- **Frontend UI** - Username/password fields appear automatically when entering URLs from configured protected media hosts
- **Security** - Credentials are transmitted securely and never stored in the database

#### URL Utilities (#116)
- **Centralized URL Construction** - New `getFlowerUrl()`, `getAppBaseUrl()`, and `getVideoUrl()` utilities for consistent URL handling across dev and production environments

### Fixed

- **VRAM Monitoring** - Added validation for VRAM monitoring keys to prevent KeyError on non-CUDA devices (#113)
- **Loading Screen** - Fixed "app.loadingApplication" raw key displaying during initial page load by using hardcoded text before i18n initializes

### Changed

- **Flower Dashboard** - Refactored URL construction to use centralized utility function
- **Video Playback** - Updated video URL construction to work correctly behind nginx reverse proxy

### Upgrade Notes

Standard Docker Compose update:
```bash
docker compose pull
docker compose up -d
```

To use protected media authentication, configure allowed hosts in `.env`:
```bash
MEDIACMS_ALLOWED_HOSTS=media.example.com,mediacms.internal
```

---

## [0.3.2] - 2025-12-17

### Overview
Patch release fixing critical bugs in the one-liner installation script that prevented successful setup on fresh installations.

**Note:** This is a scripts-only release. No Docker container rebuild required.

### Fixed

#### Setup Script Fixes
- **Scripts Directory Creation** - Fixed curl error 23 ("Failure writing output to destination") when downloading SSL and permission scripts by creating the `scripts/` directory before download attempts
- **PyTorch 2.6+ Compatibility** - Applied `torch.load` patch to `download-models.py` for PyTorch 2.6+ compatibility, mirroring the fix already present in the backend (from Wes Brown's commit 8929cd6)
  - PyTorch 2.6 changed `weights_only` default to `True`, causing omegaconf deserialization errors during model downloads
  - The patch sets `weights_only=False` for trusted HuggingFace models

### Upgrade Notes

For existing installations, no action required - Docker containers already have the PyTorch fix.

For new installations, the one-liner setup script will now work correctly:
```bash
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

---

## [0.3.1] - 2025-12-16

### Overview
Patch release with enhanced setup scripts, HTTPS/SSL support improvements, and comprehensive documentation updates for v0.2.0 and v0.3.0 features.

### Added

#### Setup Script Enhancements
- **HTTPS/SSL Setup Command** - New `./opentranscribe.sh setup-ssl` interactive command for easy SSL configuration
- **Version Command** - New `./opentranscribe.sh version` to check current version and available updates
- **Update Commands** - New `update` (containers only) and `update-full` (containers + config files) commands
- **NGINX Auto-Detection** - Automatic NGINX overlay loading when `NGINX_SERVER_NAME` is configured
- **NGINX Health Check** - Added NGINX health monitoring to `./opentr.sh health`

#### Documentation
- **NGINX Setup Guide** - Comprehensive `docs-site/docs/configuration/nginx-setup.md` with homelab and Let's Encrypt instructions
- **Universal Media URL Docs** - Updated documentation to reflect 1800+ platform support via yt-dlp
- **Garbage Cleanup Docs** - Added documentation for auto-cleanup of erroneous transcription segments
- **System Statistics FAQ** - Added FAQ entry explaining how to view system resource usage
- **Large Transcript Pagination FAQ** - Added FAQ entry about automatic pagination for long transcripts

### Changed

- **Setup Script** - Downloads NGINX configuration files during initial setup
- **Management Script** - Displays HTTPS URLs when NGINX/SSL is configured
- **Documentation** - Updated all README and Docusaurus docs to cover v0.2.0 and v0.3.0 features

### Upgrade Notes

For existing installations, run the full update to get new scripts:
```bash
./opentranscribe.sh update-full
```

Or manually update scripts:
```bash
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/opentranscribe.sh -o opentranscribe.sh
chmod +x opentranscribe.sh
```

---

## [0.3.0] - 2025-12-15

### Overview
Major feature release integrating valuable contributions from the [@vfilon](https://github.com/vfilon) fork, along with critical UUID/ID standardization fixes and production infrastructure improvements.

### Added

#### Universal Media URL Support
- **1800+ Platform Support** - Expand beyond YouTube to support virtually any video platform via yt-dlp
- **Dynamic Source Detection** - Automatically detect source platform from yt-dlp metadata
- **User-Friendly Error Handling** - Clear messages for authentication-required platforms
- **Platform Guidance** - Helpful messages for common platforms (Vimeo, Instagram, TikTok, etc.)
- **Recommended Platforms** - YouTube, Dailymotion, Twitter/X highlighted as best supported

#### NGINX Reverse Proxy with SSL/TLS (Closes [#72](https://github.com/davidamacey/OpenTranscribe/issues/72))
- **Production-Ready SSL** - Full NGINX reverse proxy configuration for HTTPS deployments
- **docker-compose.nginx.yml** - Optional overlay for production environments
- **SSL Certificate Generation** - Script for self-signed certificates (`scripts/generate-ssl-cert.sh`)
- **WebSocket Proxy** - Full WebSocket support through NGINX
- **Large File Uploads** - 2GB upload support for large media files
- **Service Proxying** - Flower dashboard and MinIO console accessible through NGINX
- **Browser Microphone Recording** - Enabled on remote/network access via HTTPS

#### Infrastructure Improvements
- **GPU Overlay Separation** - `docker-compose.gpu.yml` for optional GPU support on cross-platform systems
- **Task Status Reconciliation** - Better handling of stuck tasks with multiple timestamp fallbacks
- **Auto-Refresh Analytics** - Analytics refresh when segment speaker changes
- **Ollama Context Window** - Configurable `num_ctx` parameter for Ollama LLM provider
- **Model-Aware Temperature** - Temperature handling based on model capabilities
- **Explicit Docker Image Names** - Cache efficiency with named images

#### Documentation
- **NGINX Setup Guide** - Comprehensive `docs/NGINX_SETUP.md` documentation
- **Fork Comparison** - `docs/FORK_COMPARISON_vfilon.md` with detailed analysis
- **Implementation Plan** - `docs/FORK_IMPLEMENTATION_PLAN.md` checklist
- **Test Videos** - `docs/testing/media_url_test_videos.md` with platform test URLs

### Changed

#### Backend
- **Service Rename** - `youtube_service.py` → `media_download_service.py` for platform-agnostic naming
- **URL Validation** - Generic HTTP/HTTPS URL pattern instead of YouTube-specific
- **Minio Version** - Updated minimum version to 7.2.18

#### Frontend
- **Media URL UI** - Renamed `youtubeUrl` → `mediaUrl` throughout FileUploader
- **Notification Text** - Changed "YouTube Processing" → "Video Processing" (all 7 languages)
- **Platform Info** - Added collapsible "Supported Platforms" section with limitations warning
- **WebSocket Token Encoding** - Added `encodeURIComponent()` for auth tokens

### Fixed

#### UUID/ID Standardization (60+ files)
- **Speaker Recommendations** - Fixed recommendations not showing for new videos
- **Profile Embedding Service** - Fixed returning UUID as `profile_id` when integer expected
- **Consistent ID Handling** - Backend uses integer IDs for DB, UUIDs for API responses
- **Frontend UUIDs** - All entity references now use UUID strings consistently
- **Comment System** - Fixed UUID handling in comments
- **Password Reset** - Fixed password reset flow
- **Transcript Segments** - Fixed segment update UUID handling

### Contributors

Special thanks to:
- **[@vfilon](https://github.com/vfilon)** - Original fork contributions (Universal Media URL concept, NGINX configuration, task reconciliation)

### Upgrade Notes

Users running self-hosted deployments should pull the latest images:
```bash
docker pull davidamacey/opentranscribe-frontend:v0.3.0
docker pull davidamacey/opentranscribe-backend:v0.3.0
```

For NGINX/SSL setup, see `docs/NGINX_SETUP.md`.

---

## [0.2.1] - 2025-12-13

### Overview
Security patch release addressing critical container vulnerabilities identified in security scans.

### Security

#### Container Base Image Updates
- **Frontend**: Upgraded `nginx:1.29.3-alpine3.22` → `nginx:1.29.4-alpine3.23`
- **Backend**: Upgraded `python:3.12-slim-bookworm` → `python:3.13-slim-trixie` (Debian 12 → Debian 13)

#### Resolved Critical CVEs (4 → 0)
- **CVE-2025-47917** (libmbedcrypto) - CRITICAL - Fixed in 3.6.4-2
- **CVE-2023-6879** (libaom3) - CRITICAL - Fixed in 3.12.1-1
- **CVE-2025-7458** (libsqlite3) - CRITICAL - Fixed in 3.46.1-7
- **CVE-2023-45853** (zlib) - CRITICAL - Fixed in 1.3.1

#### Frontend Security Fixes
- Fixed 3 HIGH severity libpng vulnerabilities
- Fixed 2 MEDIUM severity libpng vulnerabilities
- Fixed 1 MEDIUM severity busybox vulnerability
- Remaining: 3 tiff CVEs (no Alpine fix available)

#### Additional Improvements
- Added `HEALTHCHECK` instructions to both frontend and backend Dockerfiles
- Updated Python from 3.12 to 3.13
- Updated pip to latest version (25.3)

### Changed
- Backend now runs on Debian 13 "trixie" (released August 2025)
- Python site-packages path updated from 3.12 to 3.13

### Upgrade Notes
Users running self-hosted deployments should pull the latest images:
```bash
docker pull davidamacey/opentranscribe-frontend:v0.2.1
docker pull davidamacey/opentranscribe-backend:v0.2.1
```

---

## [0.2.0] - 2025-12-12

### Overview
Community-driven multilingual release! This version features significant contributions from the open source community, including 7 pull requests from [@SQLServerIO](https://github.com/SQLServerIO) (Wes Brown) and a critical multilingual feature request from [@LaboratorioInternacionalWeb](https://github.com/LaboratorioInternacionalWeb).

### Added

#### Multilingual Transcription Support
- **100+ Language Support** - Expanded from 50+ to 100+ languages via WhisperX
- **Configurable Source Language** - Auto-detect or manually specify source language for improved accuracy
- **Translation Toggle** - Choose to keep original language or translate to English (default: keep original)
- **Word-Level Alignment Indicators** - UI shows which languages (~42) support word-level timestamps
- **LLM Output Language** - Generate AI summaries in 12 languages (EN, ES, FR, DE, PT, ZH, JA, KO, IT, RU, AR, HI)

#### UI Internationalization (i18n)
- **7 UI Languages** - English, Spanish, French, German, Portuguese, Chinese, Japanese
- **Language Settings** - User-configurable UI language preference
- **Locale Store** - Persistent language preference with localStorage
- **Translation System** - Comprehensive i18n system across all frontend components

#### Speaker Management Enhancements
- **Speaker Merge UI** - Visual interface to combine duplicate speakers with segment preview
- **Segment Reassignment** - Automatic segment speaker reassignment during merge
- **Per-File Speaker Settings** - Configure min/max speakers at upload or reprocess time
- **User-Level Speaker Preferences** - Save default speaker detection settings (always prompt, use defaults, use custom)

#### LLM Integration Improvements
- **Anthropic Model Discovery** - Native /v1/models API for dynamic model listing
- **Model Auto-Discovery** - Extended to support vLLM, Ollama, and Anthropic providers
- **Edit Mode API Key Support** - Stored API keys work in edit mode (no need to re-enter)
- **Updated Default Models** - Anthropic: claude-opus-4-5-20251101, Ollama: llama3.2:latest
- **Improved Configuration UX** - Toast notifications replace inline errors, better API key toggle positioning

#### User Settings
- **Transcription Settings** - User-level transcription preferences stored in database
- **Garbage Cleanup Settings** - User-configurable automatic cleanup of erroneous segments
- **Automatic Database Migrations** - Migrations run automatically on startup

#### Admin & System
- **System Statistics** - CPU, memory, disk, and GPU usage visible to all authenticated users
- **Admin Password Reset** - Secure password reset with validation
- **Compact Action Buttons** - Icon-only action buttons with tooltips in admin UI

### Changed

- **Provider Consolidation** - `claude` provider deprecated in favor of `anthropic`
- **LLM Provider Enum** - Reordered with legacy CLAUDE at end
- **Error Display** - Converted inline errors to toast notifications in LLM config modal

### Fixed

- **Large Transcript Pagination** - Fixed page hanging with thousands of segments ([PR #110](https://github.com/davidamacey/OpenTranscribe/pull/110))
- **Garbage Segment Cleanup** - Automatic detection and removal of erroneous transcription segments ([PR #107](https://github.com/davidamacey/OpenTranscribe/pull/107))
- **UUID Admin Endpoints** - Fixed admin endpoints to use UUID instead of integer ID ([PR #106](https://github.com/davidamacey/OpenTranscribe/pull/106))
- **PyTorch 2.6+ Compatibility** - Updated for newer PyTorch versions ([PR #102](https://github.com/davidamacey/OpenTranscribe/pull/102))
- **vLLM Endpoint Configuration** - Fixed summaries not working with vLLM in OpenAI mode ([Issue #100](https://github.com/davidamacey/OpenTranscribe/issues/100))
- **API Key Whitespace** - Added .trim() to all API key validations
- **Race Conditions** - Fixed race conditions when editing existing LLM configurations
- **Speaker Dropdown Visibility** - Fixed flickering and visibility issues

### Code Quality

- **Reduced Cyclomatic Complexity** - Refactored 47 functions across 27 files
- **ESLint Integration** - Improved frontend linting and type safety
- **Removed Unused Code** - Cleaned up unused error variables and CSS classes

### Contributors

Special thanks to our community contributors:
- [@SQLServerIO](https://github.com/SQLServerIO) (Wes Brown) - 7 pull requests
- [@LaboratorioInternacionalWeb](https://github.com/LaboratorioInternacionalWeb) - Multilingual feature request

## [0.1.0] - 2025-11-05

### Overview
First official release of OpenTranscribe! This release marks the transition from internal development to public availability. What started as a weekend experiment in May 2025 has evolved into a full-featured, production-ready AI transcription platform over 6 months of dedicated development.

### Added

#### Core Transcription Features
- **WhisperX Integration** - High-accuracy speech recognition with faster-whisper backend
- **Word-Level Timestamps** - Precise timing for every word using cross-attention DTW
- **Multi-Language Support** - Transcribe in 50+ languages with automatic English translation
- **GPU Acceleration** - 70x realtime speed with large-v2 model on NVIDIA GPUs
- **CPU Fallback** - Complete CPU-only mode for systems without GPUs
- **Apple Silicon Support** - MPS acceleration for M1/M2/M3 Macs
- **Batch Processing** - Process multiple files concurrently with intelligent queue management

#### Speaker Diarization & Management
- **Automatic Speaker Detection** - PyAnnote.audio integration for speaker identification
- **Cross-Video Speaker Recognition** - AI-powered voice fingerprinting to match speakers across different media files
- **Speaker Profile System** - Global speaker profiles that persist across all transcriptions
- **Voice Similarity Analysis** - Advanced embedding-based speaker matching with confidence scores
- **LLM-Enhanced Speaker Identification** - Content-based speaker name suggestions using conversational context
- **Manual Verification Workflow** - Accept/reject AI suggestions to improve accuracy over time
- **Speaker Analytics** - Talk time distribution, cross-media appearances, and interaction patterns
- **Configurable Speaker Limits** - Support for 1-20 speakers by default, scalable to 50+ for large conferences
- **Auto-Profile Creation** - Automatic speaker profile creation when speakers are labeled
- **Retroactive Speaker Matching** - Cross-video matching with automatic label propagation

#### Media Support & Processing
- **Universal Format Support** - Audio (MP3, WAV, FLAC, M4A, OGG, AAC) and Video (MP4, MOV, AVI, MKV, WEBM)
- **YouTube Integration** - Direct URL processing with automatic video download
- **YouTube Playlist Support** - Extract and queue all videos from playlists for batch transcription
- **Large File Support** - Upload files up to 4GB (supports GoPro and high-quality video content)
- **Interactive Media Player** - Plyr-based player with click-to-seek transcript navigation
- **Audio Waveform Visualization** - Interactive waveform with precise timing and click-to-seek
- **Browser Microphone Recording** - Built-in microphone recording with real-time audio level monitoring (works over localhost or HTTPS)
- **Background Recording** - Record audio in the background while using other application features
- **Recording Controls** - Pause/resume recording with duration tracking and quality settings
- **Custom File Titles** - Edit display names for media files with real-time search index updates
- **Metadata Extraction** - Comprehensive file information using ExifTool
- **Subtitle Export** - Generate SRT/VTT files for accessibility
- **File Reprocessing** - Re-run AI analysis while preserving user comments and annotations
- **Auto-Recovery System** - Intelligent detection and recovery of stuck or failed file processing

#### Upload & File Management
- **Advanced Upload Manager** - Floating, draggable upload interface with real-time progress tracking
- **Concurrent Upload Processing** - Multiple file uploads with intelligent queue management
- **Drag-and-Drop Support** - Intuitive file upload interface with direct media file upload
- **Video File Size Detection** - Automatic detection of large video files with client-side audio extraction option to reduce upload size and processing time
- **Client-Side Audio Extraction** - Extract audio from video files in the browser before upload for faster processing and reduced bandwidth
- **Duplicate Detection** - Hash-based verification to prevent duplicate uploads
- **Automatic Recovery** - Retry logic for failed uploads with exponential backoff
- **Background Upload Processing** - Seamless integration with background task queue
- **YouTube URL Upload** - Direct video processing from YouTube URLs without manual download
- **YouTube Playlist Batch Upload** - Process entire YouTube playlists via URL with automatic queuing

#### AI-Powered Features
- **LLM Integration** - Support for 6+ providers (OpenAI, Anthropic Claude, vLLM, Ollama, OpenRouter, Custom)
- **AI-Powered Summaries** - Generate comprehensive summaries with customizable formats and structures
- **BLUF Format Summaries** - Bottom Line Up Front structured summaries with action items, key decisions, and follow-ups
- **Custom AI Prompts** - Create and manage unlimited AI prompts with ANY JSON structure
- **Flexible Schema Storage** - JSONB storage supporting multiple prompt types simultaneously
- **Intelligent Section Processing** - Automatic context-aware processing (single or multi-section) based on transcript length
- **Section-by-Section Analysis** - Handles transcripts of any length with intelligent chunking at speaker/topic boundaries
- **LLM Configuration Management** - User-specific LLM settings with encrypted API key storage
- **Provider Testing** - Test LLM connections and validate configurations before use
- **AI-Powered Topic Generation** - Automatic topic extraction from transcript content for intelligent tag suggestions
- **AI-Generated Collections** - Intelligent collection suggestions based on content analysis and topic clustering
- **Smart Tag Recommendations** - AI-powered tag suggestions based on transcript content, speakers, and themes
- **Real-Time Topic Extraction** - AI-powered topic extraction with granular progress notifications
- **Speaker Name Suggestions** - LLM-powered speaker identification based on conversation context
- **Local & Cloud Processing** - Support for both privacy-first local models and cloud AI providers

#### Search & Discovery
- **Hybrid Search** - Combine keyword and semantic search capabilities using OpenSearch 3.3.1
- **Full-Text Indexing** - Lightning-fast content search with Apache Lucene 10
- **9.5x Faster Vector Search** - Significantly improved semantic search performance
- **25% Faster Queries** - Enhanced full-text search with lower latency
- **75% Lower p90 Latency** - Improved aggregation performance
- **Advanced Filtering** - Filter by speaker, date, tags, duration, and more with searchable dropdowns
- **Smart Tagging** - Organize content with custom tags and categories
- **Collections System** - Group related media files into organized collections for better project management
- **Speaker Usage Counts** - Track which speakers appear most frequently across your media library
- **Inline Collection Editing** - Tag-style interface for managing file collections
- **Searchable Dropdowns** - Enhanced filter UI for better usability

#### Analytics & Insights
- **Advanced Content Analysis** - Comprehensive speaker analytics including talk time, interruptions, and turn-taking patterns
- **Speaker Performance Metrics** - Speaking pace (WPM), question frequency, and conversation flow analysis
- **Meeting Efficiency Analytics** - Silence ratio analysis and participation balance tracking
- **Real-Time Analytics Computation** - Server-side analytics with automatic refresh capabilities
- **Cross-Video Speaker Analytics** - Track speaker patterns and participation across multiple recordings

#### User Interface & Experience
- **Progressive Web App** - Installable app experience with offline capabilities
- **Responsive Design** - Optimized for desktop, tablet, and mobile devices
- **Interactive Waveform Player** - Click-to-seek audio visualization with precise timing
- **Floating Upload Manager** - Draggable upload interface with real-time progress
- **Smart Modal System** - Consistent modal design with improved accessibility
- **Timestamp-Based Comments** - Add user comments anchored to specific timestamps in videos and transcripts
- **Comment Navigation** - Click comments to jump to the corresponding moment in the media playback
- **Annotation System** - Rich annotation capabilities with timestamp markers throughout the transcript
- **Enhanced Data Formatting** - Server-side formatting service for consistent display of dates, durations, and file sizes
- **Error Categorization** - Intelligent error classification with user-friendly suggestions and retry guidance
- **Smart Status Management** - Comprehensive file and task status tracking with formatted display text
- **Auto-Refresh Systems** - Background data updates without manual page refreshing
- **Theme Support** - Seamless dark/light mode switching
- **Keyboard Shortcuts** - Efficient navigation and control via hotkeys
- **Full-Screen Transcript View** - Dedicated modal for reading and searching long transcripts
- **Smart Notification System** - Persistent notifications with unread count badges and progress updates
- **WebSocket Integration** - Real-time updates for transcription, summarization, and upload progress

#### Infrastructure & Performance
- **Docker Compose Architecture** - Base + override pattern for different environments
  - `docker-compose.yml` - Base configuration (all environments)
  - `docker-compose.override.yml` - Development overrides (auto-loaded)
  - `docker-compose.prod.yml` - Production overrides
  - `docker-compose.offline.yml` - Offline/airgapped overrides
  - `docker-compose.gpu-scale.yml` - Multi-GPU scaling configuration
- **Multi-GPU Worker Scaling** - Optional parallel processing on dedicated GPUs (4+ workers per GPU)
- **Specialized Worker Queues** - GPU (transcription), Download (YouTube), CPU (waveform), NLP (AI features), Utility (maintenance)
- **Parallel Waveform Processing** - CPU-based waveform generation runs simultaneously with GPU transcription
- **Non-Blocking Architecture** - LLM tasks don't delay next transcription (45-75s faster per 3-hour file)
- **Configurable Concurrency** - GPU(1-4), CPU(8), Download(3), NLP(4), Utility(2) workers for optimal resource utilization
- **Model Caching System** - Simple volume-based caching (~2.6GB total) with natural cache locations
- **PostgreSQL Database** - Reliable relational database with JSONB support for flexible schemas
- **MinIO Object Storage** - S3-compatible storage for media files
- **OpenSearch 3.3.1** - Full-text and vector search with Apache Lucene 10
- **Redis Message Broker** - High-performance task queue and caching
- **Celery Distributed Tasks** - Background AI processing with multiple specialized queues
- **Flower Monitoring** - Real-time task monitoring and management dashboard
- **NGINX Production Server** - Optimized reverse proxy for production deployments
- **Complete Offline Support** - Full airgapped/offline deployment capability

#### Security & Privacy
- **Non-Root Container User** - Backend containers run as non-root user (appuser, UID 1000)
- **Automatic Permission Management** - Startup scripts automatically fix model cache permissions
- **Principle of Least Privilege** - Reduced security risk from container escape vulnerabilities
- **Security Scanning Integration** - Trivy and Grype integration for vulnerability detection
- **Role-Based Access Control** - Admin/user permissions with file ownership validation
- **Encrypted API Key Storage** - User-specific LLM settings with secure key storage
- **Session Management** - Secure JWT-based authentication
- **Local Processing** - All data stays on your infrastructure (except optional cloud LLM calls)

#### Developer Experience
- **Comprehensive Utility Scripts** - `opentr.sh` and `opentranscribe.sh` for all operations
- **Hot Reload Support** - Development mode with automatic code reloading
- **Database Backup/Restore** - Easy data migration and disaster recovery
- **Service Health Checks** - Container orchestration with health monitoring
- **Docker Build Scripts** - Automated multi-platform builds with security scanning
- **Version Management** - Centralized VERSION file for consistent versioning
- **Code Quality Tooling** - ESLint, TypeScript strict mode, Black, Ruff
- **Comprehensive Documentation** - Docusaurus documentation site with screenshots and guides
- **TypeScript Integration** - Type-safe frontend development
- **API Documentation** - OpenAPI/Swagger automatic API docs

#### Documentation & Resources
- **Complete Documentation Site** - docs.opentranscribe.app with comprehensive guides
- **Visual Screenshots** - Step-by-step visual guides for all features
- **Installation Guides** - Multiple deployment options (Docker Hub, source, offline)
- **Configuration Reference** - Detailed environment variable documentation
- **Troubleshooting Guide** - Common issues and solutions
- **Developer Resources** - Contributing guidelines and architecture documentation
- **Blog** - Release announcements and development updates
- **One-Line Installer** - Quick setup script with hardware detection

### Changed
- **License** - Migrated from MIT to GNU Affero General Public License v3.0 (AGPL-3.0) to protect open source and ensure network copyleft
- **Version Numbering** - Starting at 0.1.0 with path to v1.0.0
- **Documentation Structure** - Migrated to dedicated Docusaurus site for better organization

### Technical Stack

#### Frontend
- Svelte 5.39.9 - Reactive UI framework
- TypeScript 5.9.3 - Type-safe development
- Vite 6.1.7 - Build tool and dev server
- Plyr 3.8.3 - Media player
- Axios 1.12.2 - HTTP client
- FFmpeg.wasm 0.12.15 - Browser-based media processing
- date-fns 4.1.0 - Date formatting
- imohash 1.0.3 - Fast file hashing

#### Backend
- Python 3.11+ - Programming language
- FastAPI - Modern async web framework
- SQLAlchemy 2.0 - ORM with type safety
- Alembic - Database migrations
- Celery - Distributed task queue
- Redis - Message broker and caching
- PostgreSQL - Relational database
- WhisperX - Speech recognition with native word-level timestamps
- PyAnnote.audio - Speaker diarization
- OpenSearch 3.3.1 - Search engine (Apache Lucene 10)
- MinIO - S3-compatible object storage
- Sentence Transformers - Semantic embeddings
- NLTK - Natural language processing
- ExifTool - Metadata extraction
- yt-dlp - YouTube download

#### AI/ML Stack
- faster-whisper - Optimized Whisper inference
- PyAnnote segmentation-3.0 - Speaker segmentation
- PyAnnote speaker-diarization-3.1 - Speaker identification
- faster-whisper cross-attention DTW - Native word-level timestamps
- Sentence Transformers all-MiniLM-L6-v2 - Semantic search (~80MB)
- Multiple LLM provider support (OpenAI, Claude, vLLM, Ollama, OpenRouter)

#### Infrastructure
- Docker & Docker Compose - Containerization
- NGINX - Reverse proxy
- Flower - Celery monitoring
- GitHub Actions - CI/CD

### Performance Benchmarks
- **Transcription Speed** - 70x realtime with large-v2 model on GPU
- **Vector Search** - 9.5x faster than previous generation
- **Query Performance** - 25% faster with 75% lower p90 latency
- **Multi-GPU Scaling** - 4 parallel workers can process 4 videos simultaneously
- **Model Cache Size** - ~2.6GB total for all AI models

### Deployment Options
- **Quick Install** - One-line installer with hardware detection
- **Docker Hub** - Pre-built images for instant deployment
- **Source Build** - Full source code with development environment
- **Offline/Airgapped** - Complete offline deployment support
- **Multi-Platform** - AMD64 and ARM64 support

### Breaking Changes
- None (first release)

### Migration Notes
- This is the first public release - no migration required
- For future releases, we will strive for backwards compatibility
- Breaking changes will be clearly announced in release notes

### Known Issues
- None critical at release time
- See GitHub Issues for community-reported items

### Contributors
- David Macey (@davidamacey) - Project Lead
- OpenTranscribe Community - Testing and feedback

### Links
- **Documentation**: https://docs.opentranscribe.app
- **GitHub Repository**: https://github.com/davidamacey/OpenTranscribe
- **Docker Hub Backend**: https://hub.docker.com/r/davidamacey/opentranscribe-backend
- **Docker Hub Frontend**: https://hub.docker.com/r/davidamacey/opentranscribe-frontend
- **Issues**: https://github.com/davidamacey/OpenTranscribe/issues
- **License**: https://github.com/davidamacey/OpenTranscribe/blob/master/LICENSE

---

## Future Roadmap

Looking ahead to v1.0.0, we plan to add:
- Real-time transcription for live streaming
- Enhanced speaker analytics and visualization
- Better speaker diarization models
- Google-style text search
- LLM powered RAG Chat with transcript text
- Other refinements along the way!

We welcome community feedback and contributions as we work towards the v1.0.0 release!

[0.1.0]: https://github.com/davidamacey/OpenTranscribe/releases/tag/v0.1.0
