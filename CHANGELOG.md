# Changelog

All notable changes to OpenTranscribe will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-07-04

### Overview

This release lands four major feature areas plus a wave of hardening and dependency work. **Diarization boundary correction** (issue #193) adds a default-on word-boundary smoother and an experimental acoustic backchannel re-check that measurably reduce speaker mislabeling at turn seams. **Content redaction** introduces PII / profanity / toxicity detection with read-time masking across every display and export surface, served by a new dedicated `celery-redaction` CPU worker, with per-user opt-out and an admin enforcement floor. The **cloud ASR provider suite** is now production-verified end-to-end — AWS Transcribe, Speechmatics, AssemblyAI, Gladia, and pyannote.ai all flipped from experimental stubs to tested. The **media download architecture** completes its migration to presigned-URL streaming with SSE progress, async bulk export, and bounded, auto-expiring derived-asset caching. Plus CrisperWhisper model support, an Engine Configuration admin-UI cleanup, full 8-locale i18n parity, and a batch of Dependabot/CI updates.

This release also incorporates the substantial pipeline work that landed since v0.4.1: a refactored **combined transcription engine** with an optional **multi-GPU split**, **hybrid mode** (CPU transcription + GPU/MPS diarization) for small GPUs and Apple Silicon, a large **upload & pipeline performance overhaul** (presigned direct-to-MinIO uploads, content-hash dedup, shared-memory WAV handoff), end-to-end **pipeline timing instrumentation**, model-aware VRAM/batch tuning, orphan-sweeper resilience, and a slimmer backend image. **This release contains breaking changes** — see Upgrade Notes.

### Added

#### Open-core cloud seams & strict edition separation (PR #250)

- **Vendor-clean extension seams**: the commercial managed edition now layers onto generic, open extension points — a pluggable external token-verifier registry with JIT provisioning onto generic `external_id`/`external_org_id` columns, transcription pipeline hooks (quota reservation before dispatch, usage metering on completion — no-ops in community), a capabilities/entitlements resolver, per-tenant retention/upload-limit resolvers, and a frontend `$lib/cloud` seam whose community version is an inert stub. **No vendor noun appears anywhere in the open-source backend or frontend source** — enforced by a `seam-guard` CI gate (grep over `backend/app` + `frontend/src`) and an import-linter contract. The paid UI (hosted-auth wrapper, billing/usage/team panels, quota stores, cloud i18n packs) lives in the commercial repo and is overlaid at cloud-image build time only.
- **Multi-tenant isolation (inert in community)**: nullable `organization_id` scoping across media, collections, speakers, and search planes with membership-mirror authorization (`resolve_org_context`/`require_org_admin`), default-deny tenant gating threaded through every read surface — gallery, detail, search, subtitles/segments/waveform, stream/download URLs, analytics, comments, tags, topics, summaries — plus org-filtered speaker/voiceprint kNN and cross-org share blocking. Personal scope (`organization_id IS NULL`) behaves exactly as before; the community edition is unaffected.
- **GDPR erasure & abuse takedown (edition-neutral)**: real Art. 17 erasure cascading object storage, relational rows, and OpenSearch voiceprint (biometric) docs — org-scoped for org admins, account-wide only for the data subject or platform super-admins, with legal-hold files preserved (Art. 17(3)(e)) and the acting admin audited; admin quarantine/legal-hold with exclusion from every read surface (list, detail, search, autocomplete, collections) and release restoring the file's recorded prior status.
- **Takedown owner notice — DMCA §512(g) (issue #262)**: quarantining a file now sends its OWNER a persistent in-app notification (new WebSocket types `file_takedown` / `file_takedown_released`, 8-locale i18n) carrying the file title/filename, the admin-recorded reason, and counter-notice instructions pointing at the deployment's `ABUSE_CONTACT_EMAIL` (file UUID included for the counter-notice reference); releasing sends an access-restored notice with a working file link. The acting admin's identity is never disclosed, the file itself stays hidden (404) while quarantined, and a notification failure never blocks the takedown or release. Documented in `docs/abuse-and-takedown.md`.
- **Usage events spine**: an idempotent `usage_event` table + `record_event` service for metering/product analytics (empty in community).
- **JIT provisioning hardening**: linking an external identity to an existing account by email match requires the IdP to assert the address verified (fail-closed `email_verified` on `ExternalIdentity`); `super_admin` accounts are never JIT-linked by email; external IdPs grant at most `admin` and never demote.
- **Tenant/privacy hardening follow-ups (issue #262, migration v372)**: audit events now carry a nullable `organization_id` stamped at write time where the writer has tenant context (takedown/release, GDPR erasures, unredacted-view, prompt share/clone), and the org-admin audit read scopes on it — org-stamped events (including `user_id`-NULL failed logins) plus legacy un-stamped events attributed via member ids; other orgs' stamped events are never visible. Background imports capture the org at CREATION time instead of guessing from memberships: `watch_source.organization_id` (backfilled by v372) stamps every watch import, and playlist/URL placeholders receive the originating request's org through task kwargs (`resolve_owner_org_id` demoted to a documented last-resort for storage recovery). Remaining collection sub-surfaces (get/update/delete, share list/create/update/revoke, collection-media add/remove/list) are tenant-gated via `ctx.org_id`; group-targeted shares of an org collection now require every group member to belong to that org; org-context media adds reject cross-scope files. `SpeakerProfile` rows created via the API inherit the request's (or the speaker's) org. Collection member counts and the paginated collection-media list exclude quarantined files for non-admins. User-triggered re-diarization fires the before-dispatch access seam with a zero-hours reservation so a suspended/canceled cloud org can no longer burn GPU (402; community no-op, `CLOUD_SEAM_VERSION` unchanged).
- **Speaker-cluster tenant scope (issue #262, migration v373)**: cross-video speaker clusters are now tenant-scoped like the rest of the speaker plane. `speaker_cluster.organization_id` (NULL = personal) is stamped at creation from the member speakers' file org and mirrored onto the OpenSearch centroid doc, so org files join org clusters and personal files join personal clusters — previously org-file speakers could never join ANY cluster (isolation-safe but degraded to per-speaker singletons). `batch_recluster` now partitions the Phase-2 similarity graph per tenant scope, so a member's org and personal recordings of the same voice are never merged into one cluster. The one-off tenant backfill stamps existing cluster rows + docs from their member speakers' file orgs (all-same-org rule; legacy mixed-scope clusters stay NULL, are counted in the summary, and dissolve into per-scope clusters on the next re-cluster), replacing the earlier strip-all-org cluster repair. Community edition: org is NULL everywhere, one partition, no org field on any doc — behavior unchanged.

#### Backend observability & monitoring

- **Prometheus metrics**: new root-mounted `/metrics` endpoint (internal-network only; denied by nginx) exposing request-duration histograms by route template/method/status, request counters, in-flight gauge, **per-request DB-query-count histograms** (`db_queries_per_request` — surfaces N+1/duplicate queries per endpoint), DB query latency, cache hit/miss counters, priority-aware Celery queue-depth gauges, and product counters (`user_signups_total` by auth method, `files_uploaded_total` by source). No user IDs or raw paths in labels (cardinality-safe).
- **Structured access logs**: every request emits one access-log line carrying `user_id`, `org_id`, `request_id`, route template, status, `duration_ms`, and `db_query_count` — human-readable in `LOG_FORMAT=text` (default), single-line JSON (Loki/CloudWatch-ready) in `LOG_FORMAT=json`. Slow SQL statements (> `SLOW_QUERY_MS`, default 500 ms) log a parameter-free WARNING with the request ID.
- **Request-ID propagation into Celery**: tasks dispatched during a request now carry its `X-Request-ID`, so API requests and their background work correlate in logs; worker logging is configured via Celery's `setup_logging` signal (JSON-capable like the API).
- **Readiness probe** `GET /health/ready`: probes PostgreSQL/Redis (critical → 503) and OpenSearch/MinIO (reported, non-critical) for load balancers and orchestrators; `/health` is unchanged.
- **Optional Prometheus + Grafana overlay**: `./opentr.sh start dev --with-monitoring` starts Prometheus (:5186) and Grafana (:5185) with a provisioned datasource pair (Prometheus + read-only PostgreSQL) and two prebuilt dashboards — an ops dashboard (latency p50/p95/p99 by route, RPS, 5xx rate, DB queries/request, cache hit ratio, queue depth) and a product/usage dashboard (signups, uploads, DAU/WAU, transcription minutes). Fully optional; nothing changes when the flag is absent. Docs: `docs-site/docs/operations/monitoring.md`.

#### Fresh / isolated deployments & data-safety guardrails

- **`./opentr.sh start dev --fresh [name]`**: throwaway deployments in an isolated compose project with their own containers AND named volumes; the NAS/bind storage overlay is **never** loaded in fresh mode, so experiments physically cannot touch real data. Standard dev ports by default (with a guard if the main stack holds them) or `--port-offset N` for side-by-side; `--seed-benchmark` uploads sample media once healthy; `stop/status --fresh`, `fresh-list`, and a confirm-gated `fresh-destroy` manage lifecycle; `--dry-run` prints the compose plan without starting anything.
- **Explicit NAS-overlay directives**: the silent `.env` auto-load now announces itself with the resolved data paths; `--no-nas` suppresses it, `--nas` opts in explicitly.
- **Live-data guardrails**: every NAS-overlay start writes a `.opentranscribe-live-data` marker README into each bind-mounted data directory AND its parent (a tripwire for cleanup scripts and humans), and `./opentr.sh data-paths` prints exactly which host paths hold live data so they can be checked before any cleanup.

#### In-app scheduled database backups (no host cron)

- **Admin UI–configured backups** (Settings → System Management → Backups): enable/disable, cron schedule, destination, GFS retention (7 daily / 4 weekly / 12 monthly), optional gpg encryption (passphrase file), Run Now with last-result display. All settings are DB-backed (`backup.*` SystemSettings) — **no host cron, no new env vars, no new Python deps** (native minimal cron parser; no croniter).
- **Execution**: the existing celery-beat fires a lightweight `backup.check_schedule` every 5 minutes; when the DB-stored cron is due it dispatches `backup.run` → `pg_dump --format=custom` directly from the worker (the backend image now ships `postgresql-client`) to the mounted destination, then prunes by GFS. Schedule changes apply with no restarts.
- **Destination — mounted folder OR S3-compatible bucket**: the optional `docker-compose.backup.yml` overlay (`./opentr.sh start dev --with-backup`) maps `BACKUP_HOST_PATH` → `/backups` for a local destination; alternatively the destination can be an **S3-compatible bucket** (AWS S3, MinIO, Backblaze, etc.) so dumps land **off the host machine** — endpoint/region/bucket/prefix + access key, with the secret key encrypted at rest (AES-256-GCM, write-only, never returned) and a Test Connection button. GFS retention prunes over either backend. When the destination is unmounted/unreachable the feature degrades gracefully (UI warning, task records a status and never crashes).
- **Optional OpenSearch snapshots in the same run**: when enabled (Settings → Backups → "Include OpenSearch snapshot"), each scheduled backup also snapshots the search indices to the **same destination** beside the `.dump` files, pruned by the **same GFS retention**. The snapshot runs only after a successful database dump and its outcome never fails the dump (search indices are rebuildable from Postgres, so this is a convenience). Uses a filesystem snapshot repository allow-listed by the `--with-backup` overlay; degrades gracefully without it.
- **Recovery-key companion (#243)**: a database dump alone is **unrecoverable** without the `.env` master keys that wrote its AES-256-GCM ciphertext, so every successful run now makes the destination self-describing. With backup encryption ON, `opentranscribe-recovery.env.gpg` lands beside the dumps carrying `ENCRYPTION_KEY` / `JWT_SECRET_KEY` (and `MINIO_KMS_SECRET_KEY` when set) under the **same gpg passphrase** — the passphrase in your password manager then unlocks a complete restore. With encryption OFF, a no-secrets `RECOVERY-README.txt` (key names + SHA-256 fingerprints only) documents what to preserve separately, and admins get a **one-time warning notification** that the dumps alone are not restorable. One always-current companion per destination; a companion failure never fails the backup; key values never appear in logs or the recorded result.
- **Failure surfacing (#244)**: scheduled-backup outcomes are now surfaced proactively instead of only on the admin page. New Prometheus metrics — `backup_last_success_timestamp_seconds` (alert on staleness), `backup_last_status` (1/0), and `backup_runs_total{result}` — persisted by the worker and projected onto `/metrics` at scrape time (restart-proof, same sample-at-scrape pattern as queue depths). Failed runs (pg_dump error, missing mount, unreachable bucket) send a `backup_status` WebSocket notification to every admin with the error message; successes with non-fatal warnings notify too. **Retention-prune errors no longer fail a completed dump** — they're recorded as `prune_error` warnings (matching the OpenSearch-snapshot warn-only design), and the Backups panel now shows prune + recovery-companion status per run.
- **Media Mirror (#242)**: the backup system now covers the irreplaceable media originals, not just the database. A new **default-OFF** admin subsection (Settings → Backups → Media Mirror) schedules an **incremental copy of the MinIO media bucket** to a **separate destination** — a mounted folder (`BACKUP_MIRROR_HOST_PATH` → `/media-mirror`, via the same `docker-compose.backup.yml` overlay) or an **S3-compatible bucket** (AES-256-GCM encrypted write-only secret, Test Connection) for a true off-host copy. Objects are compared by size + ETag so only new/changed media transfers (write-once media makes nightly deltas tiny); regenerable data (temp preprocessed audio, derived/bulk caches) is excluded; per-object failures never abort a run; a configurable throttle caps I/O pressure; a Redis lock prevents overlapping runs; the run executes on the download worker (never the GPU queue). **The mirror never deletes at the destination** — a fat-fingered or malicious source-side delete cannot propagate (explicit tested invariant). Observability follows the #244 pattern: `media_mirror_last_success_timestamp_seconds`, `media_mirror_last_status`, `media_mirror_runs_total{result}`, per-outcome object counts, admin WebSocket notification on failure (success silent), last-run status + Run Now in the panel. Settings are DB-backed `backup.mirror_*` SystemSettings (cron schedule, no restarts). Closes the backup audit's High-severity media gap; restore-from-mirror documented in `docs-site/docs/operations/backup-restore.md`.

#### Storage recovery: in-place re-ingestion of orphaned MinIO objects

- **`python -m app.scripts.reingest_minio`** (run in the backend container; `--dry-run`, `--limit N`, `--user-email`, `--no-dispatch`, `--throttle N`): registers media objects that exist in MinIO but have no database row — each new `MediaFile` points at the **existing** object key in place (zero bytes copied or duplicated), gets a real imohash fingerprint, and is dispatched through the standard processing pipeline. Idempotent: re-runs skip already-referenced objects. Born from a data-loss incident where the database was destroyed but all original media survived in MinIO.
- **YouTube metadata recovery, rate-limited**: `recovery.youtube_metadata_fetch` harvests title/duration per surviving `youtube_<id>` thumbnail prefix via yt-dlp metadata-only requests (~1 every 5 s, resumable via a MinIO sidecar — never re-downloads videos), and `recovery.youtube_metadata_backfill` re-attaches titles by duration matching (±2 s, unique-match-only in both directions — ambiguous matches stay safely untitled).

#### Diarization boundary correction (issue #193)

- **Word-boundary smoothing (default ON, pure-CPU)**: a post-processing pass (`boundary_resolver.smooth_word_speakers`) that collapses 1–3 word "wrong-speaker islands" at turn seams, guarded by silent-gap and flanking-speaker checks. It relabels existing words only — never fabricating speech — and runs at the path-agnostic `finalize_segments()` chokepoint so every transcription path gets it identically. Measured −32% relative WSER and islands 82→15 on the reporter's hand-labeled clip; AMI-regression-safe.
- **Acoustic backchannel re-check (default OFF, experimental, GPU)**: re-embeds short disputed/overlap words with the diarizer's WeSpeaker model while audio is still in memory and reassigns them to the best-matching speaker centroid by voiceprint cosine — recovering absorbed backchannels ("yeah", "mm-hmm") the smoother can't. A further ~−15% WSER atop the smoother, ~1.9 s added per 10-minute file. Carried on `EngineConfig` so the engine stays DB-free.
- **Live admin tuning, no restart**: Settings → Engine Configuration gains a smoothing toggle, an acoustic re-check toggle, and two number inputs (cosine margin, max word duration), DB-backed with env fallback. The Engine Settings API gained float-value support.
- **CrisperWhisper model support**: selectable English-only Whisper model with precise word-level timestamps (`nyrahealth/CrisperWhisper`, ~10 GB VRAM). Short names and the PyTorch repo id resolve to the loadable CT2 build at load time (including per-file reprocess), with a verbatim-tokenizer normalization pass that restores spacing, repairs timestamps, and preserves word count.

#### Content redaction (PII / profanity / toxicity)

- **Read-time masking across all surfaces**: detects sensitive/offensive content and masks it with `[CATEGORY]` placeholders at every display and export surface. The full original transcript is always retained in the DB — masking is a read-time transform from cached spans (`services/redaction/spans.py:apply_redactions`). Detect-once / cache-forever (`transcript_segment.redactions` + `.toxicity`); enabling, categories, style, custom words and allowlist are all read-time (no recompute). Detectors: profanity/custom wordlist, Presidio regex + spaCy NER (optional GLiNER) PII, toxicity classifiers (English + multilingual XLM-R), and an optional LLM detector reusing `LLM_PROVIDER`.
- **Dedicated `celery-redaction` worker service**: a new independently-scalable CPU service (queue `redaction`) is the only worker that loads the PII/toxicity models. Runs at lower OS priority (`nice`) with capped intra-op threads; GPU-visible so it can opt into GPU when free VRAM allows, falling back to CPU. Added to the Flower queue list with a health check.
- **Per-user settings + admin policy floor**: per-user settings (opt-out by default) at `/user-settings/redaction` with a live example preview, style options, language support and lock indicators, plus an admin governance floor at `/admin/redaction-policy` that can force categories and mandate censored exports. New Svelte panels (`ContentRedactionSettings`, `RedactionPolicySettings`), a "Redacting…" status chip with WebSocket auto-update, and an owner/admin "show original" reveal via `?redact=false` (audited; forced categories never reveal).
- **Search-snippet redaction** and **redact-before-LLM**, so summaries and LLM features never see unredacted text.
- **Offline redaction model pre-download**: `scripts/download-models.py` gains `download_redaction_models()` (GLiNER PII + toxicity classifiers), gated by `DOWNLOAD_REDACTION_MODELS`; `Dockerfile.prod` installs the `en_core_web_sm` spaCy pipeline for Presidio.

#### Cloud ASR

- **AWS Transcribe — full production support**: dual-credential support (encrypted `access_key_id` column + secret `api_key`, both AES-256-GCM; falls back to the boto3 default chain when blank), BCP-47 language mapping, up to 30 speakers (previously capped at 10), and a "Multilingual (code-switching)" catalog entry. New ASR config UI fields ("AWS Access Key ID" / "AWS Secret Access Key", write-only).
- **Speechmatics, AssemblyAI, Gladia, pyannote.ai verified end-to-end** and flipped from experimental to tested (see Fixed for the underlying corrections).

#### Watch sources auto-import (issue #26)

- **Automatic ingestion from watched sources**: configure a **local mounted folder**, an **S3-compatible bucket** (AWS / MinIO / Backblaze / Wasabi), or an **SMB/CIFS network share**, and OpenTranscribe polls each source on its own interval (Celery Beat orchestrator + per-source scan), copies new media into app storage, and runs the full transcription/diarization/embedding pipeline automatically. Originals on remote sources are never moved or deleted; local sources can optionally delete-after-import. Settings → Watch Sources (per-user, with an admin "all sources" view).
- **Three-layer deduplication on the imohash fingerprint**: within a source (path), across sources (content), and cross-pipeline against existing `media_file` rows (manual upload / URL import / prior watch import) — duplicates are recorded with a skip reason and linked to the existing file instead of re-importing.
- **Multi-part recording stitching**: split recordings (`name_P001.ext`, `name_P002.ext`, …) from dropped VTC/podcast connections are auto-detected by configurable regex, grouped within a time window, and stitched with ffmpeg (stream-copy when codecs match, re-encode fallback otherwise). Incomplete groups wait a bounded number of scans for missing parts before stitching what arrived.
- **Multi-provider email notifications**: optional SMTP / Microsoft 365 (Graph OAuth2) / Exchange notifications on scan completion, with BLUF summary and per-file status. Encrypted credentials (AES-256-GCM), never returned in API responses.
- **Folder browser, connection testing, and per-source file history** in the UI; new `docker-compose.watch.yml` overlay and `./opentr.sh start dev --with-watch` (plus `--with-smb-test` for a local Samba test share).

#### Downloads & storage

- **Bounded derived-asset cache with retention + cleanup**: all regenerable derived assets (subtitle-embedded videos, extracted audio) moved under a `processed-videos/derived/` prefix governed by a single server-side MinIO lifecycle rule. Configurable retention (`DERIVED_CACHE_RETENTION_DAYS`, default 7) with DB-over-env so admin UI changes apply with no redeploy; new `cache_management_service`, admin API, and a "Media Cache" UI subsection (usage / retention / clear-now). A one-time startup pass reclaims legacy pre-prefix derived objects from upgraders.
- **Audio-only and original-media downloads**: the file-detail download button became a dropdown — video with subtitles, original video, and audio as MP3 / WAV / Original (lossless stream-copy via ffprobe-probed codec). `extract_audio()` with MinIO caching; `NoAudioTrackError` → HTTP 422.
- **Async bulk-export ZIP auto-expiry + browser E2E tests**: bulk-export ZIPs get a 24h MinIO lifecycle rule; new Playwright browser-driven E2E tests cover the download dropdown and gallery bulk export.

#### Platform

- **CPU-only install flag (`--cpu`)**: `setup-opentranscribe.sh`, `opentranscribe.sh`, and `opentr.sh` now accept a `--cpu` flag (and honor `OPENTRANSCRIBE_FORCE_CPU=1` for unattended installs) to explicitly opt out of the GPU compose overlay. Required on hosts where the NVIDIA Container Toolkit is detected by Docker but GPU passthrough is non-functional — e.g. WSL2 without a WSL-capable Windows NVIDIA driver, where auto-detection would otherwise enable the GPU overlay and cause celery-worker / celery-cpu-worker to fail at container start with `nvidia-container-cli: initialization error: WSL environment detected but no adapters were found`. The choice is persisted to `.env` as `FORCE_CPU_MODE=true`, so `./opentranscribe.sh start/restart/stop` continues to skip the GPU overlay without re-passing the flag. Default behaviour for GPU users is unchanged.
- **CPU-mode safe defaults and visibility**: builds on the `--cpu` flag with end-to-end CPU-mode awareness. The installer now writes `ENABLE_DIARIZATION=false` to `.env` whenever `DETECTED_DEVICE=cpu` (PyAnnote requires CUDA), prints a "CPU-Only Mode — Performance Notes" advisory in the install summary, and leaves the existing `select_whisper_model()` recommendation of `base` for CPU intact. The backend logs a single startup warning when a worker boots in CPU mode with a heavyweight Whisper model or diarization enabled. `GET /system/stats` now returns `device_mode`, `force_cpu_mode`, `whisper_model`, and `diarization_enabled` so the Settings → System Statistics panel renders a CPU-only advisory banner with the right "forced via flag" vs "no GPU detected — automatic fallback" subtitle. All 8 UI locales (en, es, fr, de, pt, ru, zh, ja) include the new strings. End-to-end testing plan documented at `docs/CPU_MODE_TESTING.md`.
- **Documentation**: new feature pages (boundary correction, content redaction), a developer guide for boundary correction, cloud-provider comparison + dataset-sweep results, and a consolidated `docs/market-research/` dossier.

#### Transcription engine & multi-GPU

- **Combined transcription engine**: a new `backend/app/transcription/engine/` orchestrator (`Engine` + `EngineConfig`, typed `JobSpec`/`JobResult`/`PreprocessResult`/`RawInferenceResult` dataclasses, a `MetricsCollector`, and `TranscriberBackend`/`DiarizerBackend` Protocol interfaces with a registry of faster-whisper / whisperx / cloud / pyannote backends). `pipeline.py` is now a thin shim delegating to `Engine.process()`, guarded by a byte-equal parity gate (`scripts/benchmark_engine_compare.py`) that asserts identical segments/language/overlap and embeddings within 1e-6 vs the legacy path.
- **Split-stage Celery pipeline + shared-volume WAV handoff**: engine `run_preprocess()` / `run_gpu_stage()` / `run_cpu_finalize()` stages; preprocess stages the 16 kHz WAV onto a shared `transcription-temp` volume so the GPU task mmap-loads it instead of re-downloading from MinIO, and waveform generation resamples that WAV (scipy `resample_poly`) instead of re-running FFmpeg.
- **Phase 4 multi-GPU split (`--with-gpu-split`)**: `ENGINE_GPU_SPLIT=true` routes ASR and diarization to separate `gpu-transcribe` / `gpu-diarize` Celery queues (new `celery-worker-gpu-transcribe` / `celery-worker-gpu-diarize` services under a `gpu-split` compose profile, activated via the new `./opentr.sh ... --with-gpu-split` flag).
- **DB-backed engine settings + admin Engine Configuration panel**: `EngineConfig.from_db_with_env_fallback()` reads `SystemSettings` `engine.*` keys (DB → env → default); new admin API `GET / POST(/update) / DELETE({key}) /api/admin/engine-settings` with db/env/default source badges and per-key reset, surfaced in a new Svelte Engine Configuration settings panel.
- **Engine metrics endpoint**: `GET /api/admin/engine-settings/metrics` returns per-worker Redis snapshots (GPU ready-queue depth, in-flight count, idle seconds, last-stage durations; 120 s TTL).

#### Hybrid mode & adaptive hardware

- **Hybrid mode — CPU transcription + GPU/MPS diarization**: auto-activates on macOS/MPS and on CUDA GPUs whose VRAM is too small for the configured model (batch=2 peak > 80% of total VRAM), unlocking 4–6 GB NVIDIA GPUs and Apple Silicon. Adds `should_use_hybrid_mode()` and a separate `diarization_device` on `TranscriptionConfig`. New env: `WHISPER_HYBRID_MODE` (auto|true|false), `WHISPER_HYBRID_CPU_MODEL` (default `small`).

#### Uploads & ingestion

- **Presigned direct-to-MinIO uploads + content-hash dedup**: opt-in `use_presigned=true` on `POST /api/files/prepare` returns a presigned PUT URL + task_id; the browser PUTs bytes directly (HTTP returns in ~100 ms, no multi-GB buffer in API heap) then calls the new `POST /api/files/complete`. Adds an `imohash_service` (constant-time fingerprint via three ranged MinIO reads) and a `MediaFile.imohash` column for dedup / artifact-cache keys / reprocess short-circuit; frontend SHA-256 moved to a web worker. The legacy multipart POST remains as a transparent fallback.
- **`celery-cloud-asr-worker` service**: the CPU worker is split into `celery-cpu-worker` (compute queues, concurrency 8) and a network-bound `celery-cloud-asr-worker` (queue `cloud-asr`, concurrency 16) so cloud-ASR jobs don't head-of-line-block local postprocess; added to every compose overlay.

#### Observability & resilience

- **End-to-end pipeline timing instrumentation**: `app.utils.benchmark_timing` captures 30+ wall-clock markers from HTTP ingress through async indexing into a durable `file_pipeline_timing` table, with admin endpoints `GET /api/admin/timing/{task_id}`, `GET /api/admin/timing`, and `GET /api/admin/timing-summary/recent`. Entirely gated on `ENABLE_BENCHMARK_TIMING` (zero overhead when off).
- **Orphan upload sweeper + retry-aware timing + error-path flush**: a new `cleanup.orphan_upload_sweeper` (beat every 15 min) reclaims PENDING `MediaFile` rows and MinIO objects orphaned by client disconnects (>30 min); failed pipelines now write a terminal `pipeline_error_end` marker and flush timing so they leave a durable row.
- **Scratch janitor**: an hourly `cleanup.scratch_janitor` purges per-file scratch dirs older than 1 h to keep crashed pipelines from filling the shared volume.

#### Operations

- **Encrypted database backups**: `./opentr.sh backup --encrypt` pipes `pg_dump` directly into GPG symmetric AES-256 (the plaintext dump never touches disk); `./opentr.sh restore` transparently detects and decrypts `.gpg`/`.asc` backups. Plain backups now print a reminder that dumps contain all user transcripts in plaintext.
- **Multi-GPU pipeline split overlay (`docker-compose.gpu-split.yml`)**: a new opt-in overlay that runs transcription and diarization on **separate GPUs** for higher throughput on a 2+ GPU host. `./opentr.sh start dev --with-gpu-split` (alias `--gpu-split`) activates the `gpu-transcribe` / `gpu-diarize` worker services (already defined in the base compose under the `gpu-split` profile) and appends the overlay, which grants each worker a dedicated GPU reservation (`GPU_TRANSCRIBE_DEVICE_ID` / `GPU_DIARIZE_DEVICE_ID`). Docker remaps each reserved card to container index 0, so both workers run on `CUDA_VISIBLE_DEVICES=0` (same pattern as `--gpu-scale`). Pairs with `ENGINE_GPU_SPLIT=true`.
- **Deployment-configuration operations guide**: new `docs-site/docs/operations/deployment-configuration.md` documents every deployment type and its exact `./opentr.sh` command, the healthcheck/`start_period`/`depends_on` first-init model, the `pipeline_scratch` cross-worker handoff contract, the three GPU modes (single / dual / split), the security posture (loopback infra ports, `no-new-privileges`, secret generation), and the NAS/NVMe storage overlay.

### Changed

- **Backend code-quality overhaul (maintainability, no behavior change)**: a sweep of the FastAPI backend with characterization tests as the regression net. **SQLAlchemy 2.0 typed models** — all 26 model files converted from legacy `Column()` to `Mapped[]`/`mapped_column()`, which let mypy see real column types and drop ~165 errors, and made 257 defensive `int(current_user.id)`-style casts provably redundant (removed); a `pg_dump` before/after diff proved zero schema change. **Blocking I/O off the event loop** — 17 `async` handlers that made synchronous MinIO/OpenSearch calls were either converted to `def` (FastAPI threadpools them) or wrapped in `run_in_threadpool`. **Endpoint dedup** — a message-parameterized `require_resource_owner` helper consolidated 17 copy-pasted ownership checks, a shared `paginate()` helper and an `ErrorHandler.internal_error()` replaced repeated boilerplate, all behavior-preserving and snapshot-gated. **Comprehensive endpoint test coverage** — characterization suites for all 39 previously-untested endpoint modules (~720 new tests across auth, files, speakers, settings, collaboration, and system/admin), with a byte-exact ownership-contract spec; coverage floor ratcheted 35 → 37 %. Celery task DB sessions standardized on `session_scope()`. **UUIDv7 generation** — all primary `uuid` columns now mint time-ordered RFC 9562 v7 identifiers (better index locality than random uuid4) via a small dependency-free generator; backward-compatible (existing rows coexist), with a defensive idempotent migration (`v368`) that converts any legacy `varchar(36)` uuid column to native `uuid` so older deployments upgrade without breaking.
- **In-place storage recovery / re-ingestion**: a new `python -m app.scripts.reingest_minio` registers media objects that exist in MinIO but have no database row — pointing each `MediaFile` at the existing object key (zero copy/duplication) and dispatching the standard pipeline — plus rate-limited yt-dlp metadata-only recovery for orphaned YouTube thumbnails. Built for disaster recovery where media survives but the database is lost.
- **Fresh / isolated deployments**: `./opentr.sh start dev --fresh <name>` runs a fully isolated stack (own compose project + volumes, NAS overlay never loaded) for safe experimentation; explicit `--nas`/`--no-nas` directives replace the silent auto-load; `.opentranscribe-live-data` marker files and a `data-paths` subcommand guard live bind-mounts against accidental cleanup.
- **Frontend modularity, quality & accessibility overhaul (issue #174)**: split the eight oversized Svelte components (`TranscriptDisplay`, `SettingsModal`, the file-detail / speakers / gallery routes, `Navbar`, `UserFileStatus`, `CollectionsPanel` — each 1,400–3,500 lines, ~19,000 lines combined) into focused single-responsibility children under new `transcript/`, `settings/`, `speakers/`, `gallery/`, `navbar/`, `fileStatus/`, `collections/`, and `fileDetail/` folders, with each route/parent kept as a thin coordinator and **no behavior, DOM, or visual change** (the eight shells dropped to ~10,400 lines, ~8,500 lines moved into focused children). Consolidated ~25 duplicated time formatters into `$lib/utils/formatting` (test-locked), extracted the client-side transcript export into a golden-tested `$lib/export` module, centralized `@keyframes` into `styles/animations.css` (with `prefers-reduced-motion`), deduplicated the collections create/edit modals, and added reusable, accessibility-correct UI primitives (`Tabs`, `Dropdown`, `Avatar`, `Badge`, `Chip`, `CopyButton`, `ExpandableSection`, `SearchableSelect`, `ConnectionStatusBanner`) plus a typed `clickOutside`/`apiError`/`focusTrap` toolkit. Stood up a **Vitest** unit/component harness (71 tests across 15 files) and wired **ESLint** (flat config) into pre-commit + CI; added a SvelteKit `+error.svelte` boundary, modal focus-trap / `aria-modal` / return-focus, icon `aria-label`s, and 17 per-folder `CLAUDE.md` docs. Verified per-commit by svelte-check (0 errors/0 warnings), `vite build`, the unit suite, and the live Playwright E2E suite.
- **Frontend type-safety, backend-leverage & resilience (issue #174, follow-on)**: enabled TypeScript `strict: true` and cut explicit `any` from 406→190 occurrences (catch blocks swept to `unknown` behind typed `getErrorMessage`/`getErrorStatus`/`getErrorCode` helpers); added an i18n key-parity checker (`npm run check:i18n`, all 8 locales) wired into CI. Pushed display shaping to the backend (thin-frontend): segments now carry an always-populated `resolved_speaker_name` and the API pre-computes `grouped_segments`, with the client retaining a fallback path so old payloads never break. Surfaced WebSocket reconnect state through a non-blocking status banner, and added an env-gated (`VITE_SENTRY_DSN`) error-reporting hook that is a lazy no-op by default (no dependency added to the home-label bundle).
- **Frontend dev-tooling & regression safety net (issue #174, follow-on)**: a bundle-size analyzer (`npm run build:analyze`, `rollup-plugin-visualizer`, gated so the default build is unaffected), dead-code detection (`knip`) and import-cycle detection (`madge`) wired as report-only CI steps, and removal of 3 orphaned modules they surfaced. Added **axe-core** accessibility assertions (baselined so only new serious/critical violations fail) and **Playwright visual-regression** screenshot baselines (light + dark, 4 primary surfaces) to the E2E suite, plus backend serializer unit tests for the new pre-shaped fields.
- **Unified upload finalization across both ingest paths**: the legacy multipart and presigned `/complete` routes now share one post-commit dispatch tail (`dispatch_upload_pipeline`: resolve per-file Whisper model → fire thumbnail → dispatch the transcription pipeline). This eliminated the hand-copied duplication that had let the two paths drift (the missing-thumbnail and missing-validation gaps). Extracted-audio uploads (client-side audio extraction) were also moved onto the presigned path with a legacy fallback, so all browser uploads share one consistent ingress. The dead `X-Extracted-Audio` header (never read by the backend; source metadata flows via the `extracted_from_video` body field) was removed.
- **Bulk subtitle export is now async + presigned**: `POST /api/files/bulk-export` (synchronous ZIP streamed through the API) is replaced by `POST /api/files/bulk-export/prepare` (returns a `job_id`) plus the SSE stream `GET /api/files/bulk-export-stream?job=<id>`. The ZIP is built on the `download` Celery worker, stored in MinIO, and delivered to the browser as a short-lived presigned URL — the API never proxies the archive bytes. This keeps bulk exports robust under concurrent users, backlog, and larger-than-expected batches, and reconnect-safe: a dropped EventSource still receives the result.
- **Media downloads moved off the API request path**: `POST /api/files/{uuid}/prepare-download` returns a ready presigned URL for passthrough/cache hits, else enqueues ffmpeg work; `GET /api/files/{uuid}/download-stream` is an SSE endpoint that pushes progress and the ready URL and re-checks the cache on reconnect. Media bytes now always stream directly from object storage (Range-capable), never through API container memory.
- **Presigned media URL lifetime raised to 6 hours** (`MEDIA_URL_EXPIRE_SECONDS` 300→21600) so a single URL outlives long viewing/labeling sessions of multi-hour files (previously 403'd mid-playback).
- **Engine Configuration admin UI trimmed to runtime-safe settings only**: removed `gpu_split` (deployment topology — hangs tasks without the `--profile gpu-split` workers), `precompute_vad` (unimplemented stub), and `shared_volume_path` (internal infra) from the admin API and panel. They remain env/deployment config. The panel now shows transcriber/diarizer backend plus the boundary controls.
- **Engine Configuration panel fully internationalized**: all infrastructure keys (titles, backend labels, Save/Reset/Saved, DB/Env/Default source badges) translated across all 8 locales at full key parity. The ASR provider dropdown now flags experimental/untested providers inline.
- **Single canonical `purge_media_file` for all delete paths**: every delete path (interactive single/force/bulk, N-day retention, orphan cleanup) now routes through one implementation that removes storage artifacts (original + thumbnail + derived cache), OpenSearch data (speakers v3/v4, transcript, chunks, summaries), the DB row, Redis state, and empty clusters — eliminating drift where retention deletes left the derived cache and orphaned data behind.
- **nginx**: dedicated no-buffering location for the SSE download/bulk-export streams (defined before `/api/`); the `/s3/` MinIO proxy brought to parity with `proxy_buffering off` + `proxy_max_temp_file_size 0` + extended timeouts for large presigned downloads.
- **Download spinner accuracy**: processed downloads now use `fetch()` so the button holds its "Processing…" state for the real ffmpeg duration, and backend errors surface as toasts; loading skeletons aligned with the search and profile layouts.
- **Dependencies**: `speechmatics-python` → `speechmatics-batch`; added `meeteval`, `presidio-analyzer`, `presidio-anonymizer`, `gliner`, `detoxify`; bumped `uvicorn`, `qrcode[pil]`, `onnx`, `yt-dlp`, `sentence-transformers`, `google-cloud-speech`, and `mypy`. Frontend dependency bumps (`@typescript-eslint`, `vite-plugin-pwa`, `devalue`; svelte pinned to avoid a 5.56.x parser regression) and `npm audit fix` to 0 vulnerabilities. CI action bumps (codeql, setup-python, cache, upload-pages-artifact, setup-buildx, anchore/scan). Dependabot reconfigured to weekly grouped updates (one frontend + one backend PR). July 2026 refresh: ~42 backend bumps (fastapi capped `<0.137` — 0.137+ breaks templated-route labeling in the observability middleware, tracked follow-up; presidio pins constrain numpy `<2.5` and cryptography `<47` transitively) and 12 frontend bumps (axios 1.18.1, dompurify 3.4.11, SvelteKit 2.69) with known-breaking majors held by policy (`@eslint/js` 10, `@types/node` 26, torch/torchaudio managed by hand, typescript-eslint trio pending the eslint 10 migration); CI + tooling aligned to the Python 3.13 runtime.
- **Model-aware Whisper batch sizing**: `_get_optimal_batch_size(model_name)` now caps batch at empirically validated thresholds per model and GPU class (from the Phase B VRAM study), replacing over-aggressive defaults (e.g. 32 on an A6000) that burned VRAM for no throughput gain (throughput plateaus at batch≈8).
- **GPU concurrency auto-detection recalibrated**: the `GPU_CONCURRENT_REQUESTS=auto` formula changed from `(vram−6000)//1000` (cap 4) to `(vram−7000)//4000` (cap 12), based on a measured ~7 GB warm baseline + ~4 GB/task — an RTX A6000 now runs up to 10 concurrent transcriptions (was capped at 4).
- **Diarization embedding batch pinned at 16**: the per-run VRAM-budget knobs were replaced by a fixed `EMBEDDING_BATCH_SIZE = 16` that forces the fork's auto-scaler off (`PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE=16`), giving a predictable ~1 GB peak so ~25 diarization pipelines fit on an A6000.
- **Eliminated duplicate upload I/O**: the source file was previously fetched from MinIO up to four times per video. Waveform now reads the preprocessed 16 kHz WAV (~10× smaller), metadata extraction runs `ffprobe` against the presigned URL (reads ~1 MB of container headers via `extract_media_metadata_from_url`) instead of re-downloading, and same-host workers hand off the WAV via a shared scratch volume (atomic rename + hard-link) with a MinIO fallback for multi-host.
- **Deferred thumbnail + full-document indexing off hot paths**: thumbnail generation (3–8 s FFmpeg) now dispatches to a task after the DB commit, and full-document transcript indexing moved onto the embedding worker, so completion fires sooner.
- **URL-ingest (yt-dlp) speed parity**: YouTube/URL ingestion now mints the task_id at entry, threads timing markers, computes imohash for dedup, defers the thumbnail FFmpeg to the queue, and runs preprocess sub-stages in parallel — matching the direct-upload fast path.
- **Upload critical-path compressions**: a single DB commit on intake (flush instead of double commit/refresh), streaming magic-byte validation (validate the first chunk before reading up to 50 GB), and a duplicate short-circuit on the legacy POST path before reading bytes.
- **Pandas removed from the diarization path**: `diarizer` / `speaker_assigner` / `reprocess` refactored onto a numpy-backed `DiarizeResult` dataclass.
- **Tunable infra knobs**: SQLAlchemy pool now configurable (`DB_POOL_SIZE` default 20, `DB_MAX_OVERFLOW` default 40, was hard-pinned 10/20); MinIO large uploads use 64 MiB multipart parts; OpenSearch refresh is suspended during large bulk loads (`SEARCH_LARGE_TRANSCRIPT_CHUNKS` default 500); download worker concurrency default raised 3→5.
- **Reference-counted frontend scroll-lock utility**: a new `src/lib/scrollLock.ts` (`lockScroll` / `unlockScroll` / `resetScrollLock`) replaces ad-hoc `document.body.style.overflow` toggling across modals and panels, fixing races where one modal closing unlocked the body while another was still open.
- **Datastore healthcheck grace periods (`start_period`)**: postgres, minio, and opensearch each gained a 60 s healthcheck `start_period` (retries 5→10/20) so a slow first-init on a large bind-mounted data dir (cluster create + WAL, bucket/IAM reconciliation, JVM boot + shard recovery) doesn't cross the retry budget, get marked unhealthy, and abort every `depends_on` service. The redis healthcheck was tightened (timeout 30s→5s, retries 50→10), and the GPU/CPU/embedding/model worker `start_period`s were raised 40s→120s to cover cold model preload + first-run HuggingFace download.
- **`celery-nlp-worker` now waits on `backend: service_healthy`** like every other worker (was `depends_on: [postgres, redis, minio, opensearch]` by start order only), so it can no longer race the schema before migrations have applied on first start.
- **`./opentr.sh start`/`reset` now block on health (`up -d --wait --wait-timeout 700`)**: a container that is created but never becomes healthy now surfaces as a non-zero exit with `ps` + recent logs, instead of the old optimistic "✅ Services are starting up." The success message changed to "✅ Services are up and healthy." `opentr.sh` also adopted `set -uo pipefail` (with the genuinely-optional `.env` vars pre-defaulted).
- **`./opentr.sh` worker lists completed**: `restore`, `restart-backend`, and the worker stop/start lists now include `celery-redaction` and `celery-cloud-asr-worker` (previously omitted, so those workers weren't stopped before a DB restore or restarted with the backend). The bench flow replaced blind `sleep`s with a deterministic backend-health poll.
- **`reset prod --build` forces no-pull at `up` time** (`--pull never`), matching `start prod --build`, so a locally-built image isn't clobbered by a Docker Hub pull when a `build:` context is also present (`pull_policy: never` isn't reliably honored in that case).

- **Backend overhaul — bugs caught by the new characterization test suite**: the ~720-test endpoint-coverage program (see Changed → "Backend code-quality overhaul") surfaced and fixed several latent defects. Malformed UUIDs on `DELETE /files/{uuid}` and on ten `speaker_clusters` routes flowed straight into a `uuid`-typed `WHERE` clause, producing an unhandled **500 plus a poisoned request transaction** instead of a clean 404 — now guarded. `list_speaker_profiles` wrapped its body in a bare `except` that **masked an intentional 403 as a 500** when filtering by another user's collection. The topics retroactive-auto-label status endpoint **500'd when Redis was unreachable** instead of degrading. Two routes were dead/unreachable and removed: `GET /api/llm/providers` (always 500 — called a nonexistent method) and `GET /api/files/analytics` (shadowed by the UUID-typed file route). A test-only defect was also fixed: API tests that dispatched Celery tasks were publishing into whichever Redis answered on the host's default port — `SKIP_CELERY` now covers the dispatch path.
- **Anonymous page loads triggered a spurious logout cascade**: since the httpOnly-cookie auth migration, the SPA's `initAuth` probed `/auth/me` on every page load; for anonymous visitors that guaranteed a 401 console error, fired a pointless `POST /auth/logout`, and — worst — `abortAllRequests()` cancelled the login page's own `getAuthMethods` fetch, so PKI/Keycloak/LDAP buttons could silently fall back to defaults. New `GET /api/auth/session` probe returns 200 for everyone (`authenticated` / `refreshable` flags), `initAuth` restores expired sessions silently via the refresh cookie instead of bouncing to login, and `fetchUserInfo` no longer has logout side effects.
- **Gallery hover-prefetch 404s for non-playable files**: hovering (or landing with the cursor over) a gallery card for a file in `error`/`processing` status prefetched a video stream URL that can't exist, logging a console 404 on every gallery visit. Prefetch now skips the stream URL unless the file is `completed`.
- **Flower healthcheck always unhealthy**: the flower service inherited the backend image's Docker HEALTHCHECK (API on :8080, which flower doesn't serve). A flower-specific compose healthcheck now probes its own unauthenticated `/flower/healthcheck`.
- **Dev-stack auth security limits vs the e2e suite**: the dev overlay (`docker-compose.override.yml`, never loaded in prod) now relaxes the per-IP auth rate limit and account-lockout threshold (`DEV_*`-tunable) so the 270+-test Playwright suite isn't throttled or lockout-poisoned; production keeps the strict `.env` defaults. E2E negative-login tests also switched to a nonexistent account so they can never lock the real admin account.
- **Thumbnails missing on presigned uploads + live gallery update**: video files uploaded via the presigned path (`/files/prepare` → direct MinIO PUT → `/files/complete`) never got a thumbnail because the dispatch lived only in the legacy multipart handler, so gallery cards stayed blank. `/files/complete` now dispatches thumbnail generation (extracted into a shared `dispatch_thumbnail_for_video` / `dispatch_upload_pipeline` used by **both** ingest paths), and `generate_thumbnail_task` emits a `file_updated` WebSocket event with a presigned `thumbnail_url` so the card swaps in the thumbnail **live during processing** instead of only on a full refresh.
- **Orphaned PENDING rows from failed presigned PUTs**: if the browser's direct-to-MinIO PUT never completed, `/files/complete` returned 400 but left a stuck PENDING row in the gallery. It now deletes the orphaned row (parity with the legacy path's failure cleanup).
- **Latent Redis pub/sub subscriber death (broke ALL realtime notifications)**: the WebSocket notification subscriber died on the first idle read timeout and never recovered, silently breaking transcription progress and all WebSocket updates. It now runs in a supervised reconnect loop with exponential backoff, treats idle `get_message` timeouts as benign, and uses `health_check_interval` + socket keepalive.
- **Video player presigned-URL refresh interval**: the file-detail and search-preview players refreshed the presigned playback URL on a hardcoded 5-minute timer regardless of the URL's real lifetime (`MEDIA_URL_EXPIRE_SECONDS`, 6h by default), needlessly re-fetching and re-setting the video `src` mid-playback. The players now use the URL's actual expiry returned by the backend.
- **Speechmatics diarization**: the deprecated `speechmatics-python` SDK returned transcripts with no speaker labels; migrated to `speechmatics-batch` (async `AsyncClient`, `submit_job`→`wait_for_completion`, parsing `results[].alternatives[0].speaker`). Speaker labels are now returned correctly.
- **AssemblyAI + Gladia end-to-end**: AssemblyAI switched to the required `speech_models` list and trimmed to working models; Gladia upload fixed to send a filename + content-type multipart part.
- **pyannote.ai transcription parsing**: word tokens are keyed `"text"` (the parser read `"word"`, returning empty words) — fixed, with API-body error surfacing added.
- **MinIO `delete_prefix`**: used the wrong `DeleteError` attribute (`.object_name`) that would raise `AttributeError` while logging a failed bulk delete — corrected to `.name`.
- **Derived-cache orphan leak**: `delete_media_file` now clears the file's derived cache and audio variants (not just video).
- **mypy 2.x strictness**: widened `upload_file_to_storage` to accept `bytes | bytearray`; annotated `.first()` results in `auto_label_service`.
- **Scratch volume ownership**: the `pipeline_scratch` named volume was root-owned while workers run as UID 1000, so `is_scratch_available()` returned False and every upload silently fell back to MinIO, defeating the shared-memory handoff. `./opentr.sh` now chowns it to 1000:1000 (and `rebuild-backend` also rebuilds `celery-cloud-asr-worker`).
- **Split-stage path leaks**: plugged a WAV cleanup leak and sanitized the `task_id` filename in the split-stage path; plumbed `asr_model` through `diarize_gpu_task`; tightened the Whisper→diarization handoff cleanup.
- **First-init datastore race left containers stuck "Created"**: on a fresh start against a large bind-mounted data dir, a slow datastore init crossed the healthcheck retry window, compose marked the datastore unhealthy, and every `depends_on` service was aborted before it ever started (symptoms: containers stuck `Created`, "relation does not exist" against a half-built schema). Fixed by the healthcheck `start_period` grace periods (see Changed) and by dropping the legacy `init_db.sql` mount from the NAS overlay — schema is built by Alembic/Python on backend startup, and the redundant init script only slowed the first boot that triggered the race.
- **Several broken deployment types repaired**:
  - **gpu-split**: the `gpu-transcribe` / `gpu-diarize` workers had no image/build in the dev or prod overlays, so `--with-gpu-split` couldn't start them. Added image/build/volumes (mirroring `celery-worker-gpu-scaled`) to `docker-compose.override.yml` and `docker-compose.prod.yml`, plus the new `docker-compose.gpu-split.yml` reservation overlay; `CUDA_VISIBLE_DEVICES` for both split workers fixed to `0` (the reserved card's in-container index).
  - **offline & bench**: both were missing the required `celery-redaction` service (redaction detection runs on every transcript), so those stacks would never process redaction. Added it to `docker-compose.offline.yml` (with HF cache + `HF_HUB_OFFLINE=1`) and `docker-compose.bench.yml`.
  - **lite**: the cloud-ASR worker was defined as a brand-new `celery-cloud-worker` service (duplicating ~30 hardcoded env vars that drifted from the base, plus referencing a bad `external` network) instead of overriding the base `celery-cloud-asr-worker`. Renamed to override the base service and inherit its connection/credential env, and removed the broken external-network block.
  - **pki-dev**: documented and fixed the compose chain (the dev override is required for the non-frontend/backend services and the shared network), resolved a host-port clash with Vite/docs (PKI plain-HTTP now publishes on `PKI_HTTP_PORT`, default 5187; mTLS stays on `PKI_HTTPS_PORT`/8443), and removed the stray private bridge network so it joins the stack's default network.
- **`pipeline_scratch` cross-worker handoff missing on several services**: the scaled GPU worker (override/prod/offline) and the GPU-split workers lacked the `pipeline_scratch:/scratch/opentranscribe` mount that the other transcription workers use to read the CPU-staged preprocessed WAV. Without it the worker can't see the handoff and silently falls back to re-downloading each file from MinIO. Mount added everywhere a transcription worker runs.
- **Aux overlay networks (ldap/keycloak/smb) hardcoded the project name**: the test-IdP overlays joined an `external` network literally named `transcribe-app_default`, so they failed to attach for any clone whose compose project name wasn't `transcribe-app`. Replaced with the project-agnostic `default` network named `${COMPOSE_PROJECT_NAME:-opentranscribe}_default`.
- **Setup-script LLM API keys silently discarded**: `setup-opentranscribe.sh` wrote LLM keys with `sed` patterns that targeted commented placeholder lines (`# OPENAI_API_KEY=...`); when the line wasn't in the expected commented form the substitution was a no-op and the key was lost. Rewritten to use an `_upsert_env` helper that sets the value whether the key is present, commented, or absent.

### Performance

- **Backend read-path query reduction (measured)**: the new `db_queries_per_request` instrumentation surfaced duplicate queries on hot paths, which were then eliminated — file detail **18 → 11** queries (−39%) and the segments endpoint **13 → 6** (−54%). The dominant win was the content-redaction admin policy load going from 8 sequential `get_setting` SELECTs to a single batched `get_settings_map` SELECT (it runs on every transcript read), plus `selectinload`/`joinedload` on the speaker-and-profile relationships. `EXPLAIN` confirmed every hot lookup is already indexed, so no new index was warranted.
- **In-process settings cache**: a TTL cache (`SETTINGS_CACHE_TTL`, default 30 s) fronts `SystemSettings` reads with bust-on-write across every writer; **Redis read-side caching** is enabled for the tag list (the one provably-safe, user-keyed surface) with a full invalidation audit that also closed previously-missing tag/speaker cache-busting on several mutation paths. Cache hit/miss is exported as `cache_operations_total`.
- **Settings reads batched app-wide**: `get_settings_map` (one SELECT for N keys) adopted in the redaction, backup, watch, user-settings, and engine config paths.
- **Backend Docker image slimmed ~820 MB** (9.68 GB → 8.86 GB): removed `triton` (~540 MB) and the `gcc`/`g++` toolchain tied to opt-in `torch.compile` (~150 MB) from the runtime stage, dropped `pytest` from runtime requirements, and removed the direct pandas dependency.
- **Removed the TensorRT pip dependency + `LD_LIBRARY_PATH` entry (−4.5 GB)**: the Phase 6.3 TensorRT execution-provider experiment never produced an end-to-end win (per-shape engine-rebuild storms on pyannote), so the image returned to its pre-spike size. The ONNX Runtime CUDA EP is retained.
- **ONNX Phase 6.2 — CPU execution-provider integration**: the one shipping ONNX win, giving 1.87–2.12× on the CPU-only tier (the CUDA / CoreML / TensorRT EPs regressed and were not shipped).

### Removed

- **Legacy byte-proxy media endpoints (breaking change)**: the deprecated `GET /api/files/{uuid}/video`, `/simple-video`, `/content`, `/download`, and `/download-with-token` endpoints have been removed. All media now streams directly from object storage via short-lived presigned MinIO URLs — playback uses `GET /api/files/{uuid}/stream-url` and downloads use `POST /api/files/{uuid}/prepare-download` (file-detail dropdown). Presigned URLs support HTTP range requests natively, so video seeking is unaffected. `GET /api/files/{uuid}/thumbnail` is retained as a resilient fallback for when presigned thumbnail minting fails. External API consumers that linked the removed routes should switch to the presigned-URL endpoints.
- **Engine Settings keys `gpu_split`, `precompute_vad`, `shared_volume_path`** removed from the admin API/panel (now env/deployment-only or unimplemented).
- **Per-GPU diarization VRAM-budget env vars** (`DIARIZATION_VRAM_BUDGET_MB`, `DIARIZATION_MIXED_PRECISION`, `DIARIZATION_ONNX_CPU`) removed, superseded by the fixed batch-16 policy.
- **`docs/performance-whitepaper/` untracked** (main.tex + main.pdf): WIP pending human review; remains on disk and in `.gitignore`.

### Security

- **Production placeholder-key guard hardened**: the backend's production startup check refused known weak defaults but did **not** recognize the `.env.example` placeholder (`CHANGE_ME_auto_generated_on_install`), so a hand-copied `.env` could boot production with publicly-known JWT/encryption keys. Both the `JWT_SECRET_KEY` and `ENCRYPTION_KEY` checks now also reject any `change_me` placeholder value.
- **Predictable secret fallback removed from the installer**: when neither `openssl` nor `python3` was available, `setup-opentranscribe.sh` derived all credentials — including the MinIO at-rest encryption key — from the current timestamp (`date +%s`), making them brute-forceable. The fallback now reads `/dev/urandom` (cryptographically secure, coreutils-only), and setup aborts rather than ever generating predictable secrets.
- **Offline installer now generates the MinIO encryption key**: `install-offline-package.sh` left `MINIO_KMS_SECRET_KEY` at its invalid placeholder (with `MINIO_KMS_AUTO_ENCRYPTION=on`), which prevents MinIO from starting; it now generates a real key (and a `FLOWER_PASSWORD`) like the main installer.
- **Windows installer no longer bakes secrets into the package at build time**: `build-windows-installer.sh` generated all credentials (Postgres/JWT/encryption/MinIO-KMS) when the package was *built*, so every installation of the same distributed installer shared identical secrets. The package now ships `.env` with placeholders plus a new `generate-secrets.ps1` (CNG `RandomNumberGenerator`, UTF-8 no-BOM, idempotent — only replaces placeholder values) that `run_opentranscribe.bat` invokes on first launch, giving each installation unique credentials. Verified end-to-end under PowerShell 7.
- **Startup secret-guard test coverage**: new `tests/test_production_secrets_guard.py` (12 tests) locks the production validation behavior — placeholder/weak JWT and encryption keys, missing Redis password, `DEBUG=true`, and the dev-mode exemption.
- **Magic-byte validation on presigned upload completion**: bytes uploaded via the presigned path go browser→MinIO directly (never through the API), so `/files/complete` now range-reads the object header and runs `validate_uploaded_file` before dispatching to the pipeline — rejecting disguised files (e.g. an executable renamed `.mp4`) with a 400 and deleting the object + row. This brings the presigned path to security parity with the legacy multipart handler. Fail-safe: only a confirmed bad signature rejects; a transient read error logs and proceeds.
- **Redaction worker hardening**: the `celery-redaction` service runs with `no-new-privileges`, capped intra-op threads, and lower OS priority.
- **AWS credential encryption**: the AWS Access Key ID is stored AES-256-GCM encrypted (never returned; the response exposes only `has_access_key_id`), matching the existing secret-key model.
- **Frontend dependency advisories cleared**: `npm audit fix` brought known advisories (axios, @sveltejs/kit, dompurify, et al.) to 0.
- **Frontend production hardening (issue #174)**: a `dependency-audit` CI job (`npm audit` + `pip-audit`); the theme bootstrap externalized from `app.html` to a static `/theme.js` and the **CSP tightened to drop `script-src 'unsafe-inline'`** — moved to SvelteKit `kit.csp` hash mode, with the now-redundant CSP `script-src` directives removed from `nginx.conf` / `nginx-pki.conf` (verified zero console CSP violations); server-side enforcement of the speaker-label length cap (`SpeakerUpdate` `max_length`); and a documented AWS production / hardening guide (`docs/deployment/AWS_PRODUCTION.md`) capturing the security audit (no bundle secrets, no prod source maps, DOMPurify on all `{@html}`, server-side authz) plus the ALB + AWS WAF + ACM TLS + Secrets-Manager reference architecture.
- **Infrastructure host ports bound to loopback**: postgres, redis, opensearch (and admin port), minio (API + console), and flower now publish their host ports as `127.0.0.1:<port>:<container>` instead of `0.0.0.0`. These services are reached internally over the compose network (`postgres:5432`, `minio:9000`, etc.); the host ports exist only for local tooling/tests and are no longer exposed to the LAN. The application frontend/nginx ports are unchanged.
- **`no-new-privileges` on the remaining auxiliary containers**: added `security_opt: [no-new-privileges:true]` to nginx, keycloak, step-ca, lldap (LDAP test), and the Samba test share (the core services already had it), preventing setuid privilege escalation inside those containers.
- **Installer `.env` locked to owner-only (0600)**: `install-offline-package.sh` previously left the generated `.env` world-readable (`644`) after a recursive `755`; it is now `chmod 600` *after* the recursive pass so the file holding all generated secrets isn't re-loosened.
- **`OPENSEARCH_ADMIN_PASSWORD` now generated by the installers**: both `setup-opentranscribe.sh` and `install-offline-package.sh` generate a complexity-compliant admin password (upper+lower+digit+special, ≥8) so enabling the OpenSearch security plugin doesn't fail its bootstrap password check. It is only consumed when `OPENSEARCH_SECURITY_ENABLED=true`.
- **API keys no longer echoed at the setup prompt**: `setup-opentranscribe.sh` reads LLM provider API keys with `read -s` (no terminal echo) instead of plain `read`, so keys don't appear on-screen or in the scrollback during interactive setup.

### Upgrade Notes

- **Breaking — removed media endpoints**: external consumers of `GET /api/files/{uuid}/video`, `/simple-video`, `/content`, `/download`, and `/download-with-token` must migrate to `GET /api/files/{uuid}/stream-url` (playback) and `POST /api/files/{uuid}/prepare-download` (downloads).
- **Breaking — bulk export endpoint**: `POST /api/files/bulk-export` (sync streamed ZIP) is replaced by `POST /api/files/bulk-export/prepare` + the SSE `GET /api/files/bulk-export-stream`.
- **Breaking — file fingerprints regenerated**: the server-side `imohash` fingerprint now uses the real `imohash` package (murmur3 over sampled windows + size) instead of the previous hand-rolled blake2b stand-in, so **every existing `media_file.imohash` value changes**. A one-time recompute runs automatically on first startup after upgrade (`asyncio` task gated by the `imohash_package_recompute_complete` system-settings flag, same pattern as the thumbnail/embedding migrations) and overwrites all rows via fast ranged reads — no manual action required. Cross-pipeline dedup (watch sources, re-upload detection) is unreliable for not-yet-recomputed rows until it finishes; an admin "Recompute File Fingerprints" button is available to re-trigger it.
- **New required service**: deployments must run the new `celery-redaction` worker — redaction detection runs once per transcript regardless of user settings. It is included in the standard compose overlays; no action is needed when using `./opentr.sh`.
- **Breaking (unreleased-master only) — v367 schema rewritten**: the cloud-seams migration was rewritten in place to be vendor-neutral (`external_id`/`external_org_id`; billing columns removed from core). No tagged release shipped the old shape; deployments tracking unreleased master (or commercial pins) are repaired automatically by the new `v371` migration, which renames the legacy columns idempotently on startup — no manual action required.
- **Database migrations** `v360_add_file_pipeline_timing`, `v361_add_media_file_imohash`, `v362_add_pipeline_timing_markers`, `v363_add_asr_access_key_id`, `v364_add_content_redaction`, `v365_add_prompt_shared_by`, `v366_add_watch_sources`, `v367_add_cloud_seams` (rewritten), `v368_uuid_native_type_guard`, `v369_superuser_role_invariant`, `v370_add_media_file_quarantine`, `v371_repair_cloud_seams_columns`, `v372_add_audit_organization_id`, and `v373_add_cluster_organization_id` apply automatically on backend startup (idempotent, additive). No manual `alembic` step is required in dev.
- **New env vars** are optional (sensible coded defaults): redaction tuning (`REDACTION_*`, `DOWNLOAD_REDACTION_MODELS`, `PRELOAD_REDACTION_MODELS`), derived-cache retention (`DERIVED_CACHE_RETENTION_DAYS`), hybrid mode (`WHISPER_HYBRID_MODE`, `WHISPER_HYBRID_CPU_MODEL`), engine/multi-GPU (`ENGINE_GPU_SPLIT`, `ENGINE_TRANSCRIBER_BACKEND`, `ENGINE_DIARIZER_BACKEND`, `GPU_TRANSCRIBE_DEVICE_ID`, `GPU_DIARIZE_DEVICE_ID`), DB pool (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`), `SEARCH_LARGE_TRANSCRIPT_CHUNKS`, `FFMPEG_THREADS`, and timing (`ENABLE_BENCHMARK_TIMING`). Boundary-correction and redaction behavior is primarily DB/admin-UI driven. `MEDIA_URL_EXPIRE_SECONDS` default changed 300→21600 (6h).
- **Optional multi-GPU split**: enable with `ENGINE_GPU_SPLIT=true` and launch via `./opentr.sh start dev --with-gpu-split` (runs dedicated `gpu-transcribe` / `gpu-diarize` workers). Without those workers, leave it off — tasks would otherwise wait on an unstaffed queue.
- **Hybrid mode** auto-activates on small-VRAM CUDA GPUs and on macOS (`WHISPER_HYBRID_MODE=auto`); force with `true`/`false`. No action needed for standard A6000-class GPUs.
- **Watch sources**: to watch a local folder, mount it via `WATCH_HOST_PATH` (the only watch env var; defaults to `./watch`) and start with `./opentr.sh start dev --with-watch` — every other watch setting (per-source connections/credentials/schedules and the global tuning knobs) is DB-backed and managed live from the admin UI with no restart. New backend dependencies `smbprotocol`, `msal`, and `watchdog` are added to `requirements.txt` (installed automatically on image build). New optional test container: `./opentr.sh start dev --with-smb-test`. **Email notifications are experimental** — delivery has not yet been verified against a live SMTP/M365/Exchange provider; test your configuration before relying on it.

## [0.4.1] - 2026-04-14

### Overview

Patch release fixing LDAP group filtering for Active Directory Distinguished Names (issue #188) and adding Keycloak-as-PKI-broker compliance for government/FedRAMP deployments.

### Fixed

- **Always-False `FileStatus` comparisons (issue #272)**: `str(FileStatus.X)` renders `"FileStatus.X"`, so two guards comparing against bare value strings never fired — the redaction don't-run-mid-reprocess guard, and the completed-only gate for on-demand analytics at three sites (**on-demand analytics never computed anywhere**; completed files missing analytics silently returned none). Both now compare enum members, with regression tests pinning the behavior.
- **Python lint targets aligned to the 3.12+ runtime**: ruff/black/mypy targets moved from py311/py39 to py312 with the resulting ~1,500-site pyupgrade codemod applied mechanically (`Optional[X]` → `X | None`, `timezone.utc` → `UTC`, PEP 695 generics, StrEnum adoptions audited case-by-case) — behavior-preserving, hand-audited, full suite green.

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
