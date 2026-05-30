# CLAUDE.md

Guidance for Claude Code working in this repository. See `docs/` for in-depth references; this file is the index.

## ⚠️ CRITICAL: Local Code vs Docker Hub Images

**Production/nginx/PKI overlays serve pre-built images from Docker Hub. Your local code changes are NOT included until you rebuild.** Symptom: 404s on new endpoints, missing UI features.

Why: `docker-compose.prod.yml` references `davidamacey/opentranscribe-*:latest`. `docker-compose.local.yml` sets `pull_policy: never` but does not bind-mount source. The dev `docker-compose.override.yml` (Vite hot-reload) is **not** auto-loaded when you pass explicit `-f` flags.

Rebuild + recreate (frontend and/or backend):
```bash
docker build -t davidamacey/opentranscribe-frontend:latest -f frontend/Dockerfile.prod frontend/
docker build -t davidamacey/opentranscribe-backend:latest  -f backend/Dockerfile.prod  backend/
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.nginx.yml \
               -f docker-compose.local.yml -f docker-compose.pki.yml \
               up -d --no-deps --force-recreate frontend backend
```

Caching: COPY layers are content-hashed. If a change isn't picked up, touch a file (`echo "<!-- $(date +%s) -->" >> path/to/file`) rather than `--no-cache` (which re-runs `npm ci`). Never run `./opentranscribe.sh update` against local builds — it pulls from Docker Hub.

## Project Overview

Containerized transcription app: **WhisperX** (transcription, 100+ languages) + **PyAnnote** (diarization, with our optimized fork) + optional LLM features.

Core services:
- **Frontend**: SvelteKit + TypeScript SPA (i18n: en, es, fr, de, pt, zh, ja, ru)
- **Backend**: FastAPI + SQLAlchemy 2.0 + Alembic
- **Database**: PostgreSQL · **Storage**: MinIO · **Search**: OpenSearch 3.4 (Lucene 10) · **Queue**: Celery + Redis · **Monitoring**: Flower

## Development Commands

### `./opentr.sh` — primary script

```bash
./opentr.sh start dev          # start dev environment
./opentr.sh stop               # stop all services
./opentr.sh reset dev          # WARNING: deletes data, runs full migration chain
./opentr.sh status             # service health
./opentr.sh logs [service]     # backend|frontend|postgres|celery-worker
./opentr.sh shell [service]    # exec into container
./opentr.sh restart-backend    # restart just backend
./opentr.sh restart-frontend   # restart just frontend
./opentr.sh backup             # database backup
./opentr.sh restore backups/<file>.sql
```

### Auth method overlays

Configure auth via Admin UI (Settings → Authentication); DB config takes precedence over `.env`. Pass test-container flags to spin up local IdPs:

```bash
./opentr.sh start dev --with-ldap-test       # LDAP at localhost:3890, UI :17170 (admin/admin_password)
./opentr.sh start dev --with-keycloak-test   # Keycloak at localhost:8180 (admin/admin)
./opentr.sh start prod --build --with-pki    # PKI/mTLS at https://localhost:5182 (prod-only — Vite can't do mTLS)
```
Combine flags as needed. PKI client certs: `scripts/pki/test-certs/clients/*.p12`.
Setup details: `docs/PKI_SETUP.md`, `docs/LDAP_AUTH.md`, `docs/KEYCLOAK_SETUP.md`.

### Multi-GPU worker scaling (optional)

Enable via `.env`: `GPU_SCALE_ENABLED=true`, `GPU_SCALE_DEVICE_ID=2`, `GPU_SCALE_WORKERS=4`. Run with `--gpu-scale`. Loads `docker-compose.gpu-scale.yml`, disables the default single GPU worker, runs N parallel Celery workers in one container against the chosen GPU. Tune `GPU_SCALE_WORKERS` to your VRAM.

### Docker build & push (production images)

**Always** use `USE_REMOTE_BUILDER=true` for multi-arch — without it ARM64 falls back to QEMU (2–3 hours vs 15–30 min via the Mac Studio builder).

```bash
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh                    # both services
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh backend|frontend   # one service
USE_REMOTE_BUILDER=true SKIP_SECURITY_SCAN=true ./scripts/docker-build-push.sh backend  # quick iteration
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh auto               # auto-detect changed
PLATFORMS=linux/amd64 ./scripts/docker-build-push.sh backend              # single-arch (no remote needed)
```
Full docs: `scripts/README.md`.

### Frontend / Backend / venv

```bash
# Frontend
cd frontend && npm run dev      # vite dev server
                  npm run build # production build
                  npm run check # svelte-check / type check

# Backend (in container is preferred; outside requires venv)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
pytest tests/
alembic upgrade head            # production only — dev runs migrations on startup automatically

# Host venv (for pre-commit, mypy, ruff, bandit, pytest outside Docker)
source backend/venv/bin/activate
```
The venv at `backend/venv/` already exists. If it doesn't: `cd backend && python3.11 -m venv venv && source venv/bin/activate && pip install -r requirements.txt pre-commit mypy ruff bandit`.

### Pre-commit / lint hooks

Configured: ruff (lint+format), mypy, prettier, svelte-check, vite build, bandit, shellcheck, Dockerfile lint. The frontend hook only fires when `frontend/src/**/*.{svelte,ts,js,css,html}` is staged. Run all: `pre-commit run --all-files`.

Manual frontend check: `./scripts/frontend-check.sh [--no-claude] [--check-only]`. Inside Claude Code: `/fix-frontend`.

## Testing

### E2E (pytest + Playwright)

Tests in `backend/tests/e2e/` (login, registration, auth flow). Requires dev environment running (`./opentr.sh start dev`).

```bash
source backend/venv/bin/activate
pytest backend/tests/e2e/ -v                                    # headless
DISPLAY=:11 pytest backend/tests/e2e/ -v --headed               # visible on XRDP
pytest backend/tests/e2e/test_login.py::TestLoginSuccess -v
```
Test fixtures (in `conftest.py`): `login_page`, `authenticated_page`, `auth_helper`, `api_helper`. Test creds: `admin@example.com` / `password`.

### Browser automation (interactive debugging)

System tool at `~/bin/browser-tools/browse.js` — opens URL, runs actions (`fill:`, `click:`, `screenshot:`, `wait:`, `eval:`), captures console errors. Full action list and setup in `~/bin/browser-tools/README.md`. On XRDP, pass `--display=:13`.

## Database Migrations

Alembic is the schema authority. Migrations run automatically on backend startup via `backend/app/db/migrations.py` (detects current version, stamps untracked DBs, applies pending migrations).

To add a schema change:
1. New revision file in `backend/alembic/versions/` — use idempotent SQL (`IF NOT EXISTS`) for safety.
2. Update SQLAlchemy models in `backend/app/models/`.
3. Update Pydantic schemas in `backend/app/schemas/` if exposed via API.
4. Add detection logic for the new version in `backend/app/db/migrations.py`.
5. Test with `./opentr.sh reset dev` (full chain from scratch) and a rebuilt-and-restart (migration on startup).

`database/init_db.sql` is legacy reference only — not used for schema.

## Authentication System

Hybrid: multiple methods can be enabled simultaneously via `AUTH_TYPE` (local, ldap, keycloak, pki, or comma-separated). Modules live in `backend/app/auth/`.

Patterns:
- JWT access tokens (short-lived) + refresh-token rotation (long-lived).
- External IdP users (PKI / Keycloak) bypass local MFA — handled by the IdP.
- TOTP MFA per RFC 6238 / 4226 (compatible with Google Authenticator, Authy, etc.).
- Per-IP and per-user rate limiting; configurable account lockout.
- All auth events written to audit log.

DB models: `UserMFA`, `PasswordHistory`, `RefreshToken`. Enforce MFA globally or per-user.

## AI Processing Workflow

1. Upload to MinIO → metadata extraction → DB record
2. Celery task dispatch (GPU queue)
3. WhisperX transcription (100+ languages, native word-level timestamps, optional translation to English)
4. PyAnnote diarization + voice fingerprinting
5. Optional: LLM speaker-ID suggestions (manual verification required)
6. Optional: LLM summarization (BLUF format, multi-section stitching, 12 output languages)
7. DB write + OpenSearch indexing
8. WebSocket notification to frontend

### Whisper model selection

Default: `large-v3-turbo` (6× faster, ~6GB VRAM). Override via `WHISPER_MODEL`.

| Model | Best for | VRAM | Translation |
|---|---|---|---|
| `large-v3-turbo` | English, most major languages, speed-critical | ~6 GB | **NO** — not trained for it |
| `large-v3` | Non-English (Thai/Cantonese/Vietnamese), translation, max accuracy | ~10 GB | Yes |
| `large-v2` | Legacy fallback | ~10 GB | Yes |

If users enable "Translate to English" they must use `large-v3` or `large-v2`.

### Hybrid mode (CPU transcription + GPU/MPS diarization)

Auto-activates for low-VRAM CUDA GPUs and always on macOS (faster-whisper MPS is unreliable; PyAnnote MPS via fork is solid). Diarization needs ~1.3 GB VRAM only.

CUDA trigger: minimum batch=2 model peak > 80% of total VRAM (turbo/v3: < ~4.9 GB GPU, medium: < ~4.8 GB, small: < ~3.7 GB).

```bash
WHISPER_HYBRID_MODE=auto       # auto (default) | true | false
WHISPER_HYBRID_CPU_MODEL=small # small | medium | base
```

Key code: `backend/app/utils/hardware_detection.py` (`should_use_hybrid_mode`), `backend/app/transcription/{config,diarizer}.py`. Benchmarks: `docs/whisper-vram-profile/README.md`.

### Diarization (PyAnnote — optimized fork)

Fork: `davidamacey/pyannote-audio@gpu-optimizations` (pip-installable). Embedding batch is **fixed at 16** in `SpeakerDiarizer.EMBEDDING_BATCH_SIZE` (forces fork's auto-scaler off via `PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE=16`).

- Diarization peak ≈ 1 GB VRAM over process baseline → an A6000 hosts ~25 concurrent pipelines.
- DER vs reference invariant across batch ∈ {1..128} fp32; fixing the batch trades 3% wall-time for predictable VRAM.
- `MIN_SPEAKERS=1`, `MAX_SPEAKERS=20` defaults; raise `MAX_SPEAKERS` to 30–50+ for conferences (no hard cap — sklearn `AgglomerativeClustering`).

Diagnostics: `python -m app.scripts.diarization_diag`. Raw data: `docs/diarization-vram-profile/README.md`. PR: pyannote-audio#1992.

#### Boundary correction (issue #193)

Two post-processing stages fix speaker mislabeling at turn boundaries. **All settings are DB-backed and live in the admin UI → Settings → Engine Configuration** (no restart); env (`ENGINE_BOUNDARY_*`) is fallback-only — no required `.env` vars.

- **Boundary smoothing** (default ON, pure-CPU): collapses 1–3 word "wrong-speaker islands" flanked by the same speaker with no real pause. Runs at the `finalize_segments()` chokepoint. -32% WSER on the reporter's clip, AMI-regression-safe. Code: `boundary_resolver.py` (`smooth_word_speakers`, `BoundarySmoothingConfig`), `utils/segment_postprocess.py`, called from `tasks/transcription/core.py`. Key: `boundary_smoothing_enabled`.
- **Acoustic backchannel re-check** (default OFF, experimental, GPU): re-embeds short disputed/overlap words and reassigns by voiceprint cosine — relabels existing words only, never invents speech. +~15% WSER on top of the smoother, ~1.9 s / 10-min file. Code: `acoustic_recheck` (`boundary_resolver.py`), `diarizer.embed_window`, wired in `engine/stages.py`; carried on `EngineConfig` to keep the engine DB-free. Keys: `boundary_acoustic_recheck_enabled`, `boundary_acoustic_cosine_margin` (0.05), `boundary_acoustic_max_word_dur` (1.0).
- Settings API: `api/endpoints/engine_settings.py`; UI: `frontend/src/components/settings/EngineSettings.svelte`. Metrics (WSER/island/DER): `utils/diarization_metrics.py`. Benchmark: `scripts/benchmark_boundary.py`. GPU-free regression: `tests/integration/test_boundary_regression.py` (fixtures: `tests/fixtures/boundary/`). Docs: `docs-site/docs/features/boundary-correction.md`, `docs-site/docs/developer-guide/diarization-boundary-correction.md`.

### LLM features (optional)

Set `LLM_PROVIDER` in `.env` (vllm, openai, ollama, anthropic, openrouter) plus provider-specific keys/endpoints. Empty = transcription-only. Self-hosted vLLM/Ollama deployed separately. Model auto-discovery via OpenAI-compatible endpoints; edit mode reuses stored API keys.

Summarization: BLUF, speaker analysis with talk time, action items + assignments, decisions, follow-ups, multi-section stitching for long transcripts, 12 output languages (en, es, fr, de, pt, zh, ja, ko, it, ru, ar, hi).

Speaker identification: LLM suggestions with confidence scoring (suggestions are never auto-applied — manual verification only). Cross-video matching via embeddings; merge UI for duplicates.

### User-level transcription settings

Per-user (Settings → Transcription):
- Source language (auto / specific) and translate-to-English toggle
- LLM output language
- Speaker behavior (always prompt / use defaults / use saved)
- Min/Max speakers
- Garbage-segment cleanup
- Recording quality / mic device

Per-file overrides available at upload/reprocess.

### Universal media URL ingestion (yt-dlp)

Supports 1800+ platforms. **Best**: YouTube, Dailymotion, TikTok. **Limited** (often need auth): Vimeo, Twitter/X, Instagram, Facebook. The system maps yt-dlp errors to user-friendly messages with platform-specific suggestions. Limits: 4-hour duration, 15 GB file size. No extra config — yt-dlp ships in the backend container with anti-block headers/client-rotation for YouTube.

## Model Caching

Configured via `MODEL_CACHE_DIR` in `.env` (default `./models`). Volumes mount each cache (`huggingface`, `torch`, `nltk_data`, `sentence-transformers`, `opensearch-ml`) into the container's `~/.cache/...`. `opensearch-ml` is also mounted read-only at `/ml-models` in the OpenSearch container.

Models persist across rebuilds (~2.5 GB total). Permissions auto-fixed by `./opentr.sh` startup; manual fix: `./scripts/fix-model-permissions.sh` (chowns to UID/GID 1000:1000 — the non-root container user).

### OpenSearch neural search

Enabled by default (`OPENSEARCH_NEURAL_SEARCH_ENABLED=true`). Default model: `all-MiniLM-L6-v2` (384d, 80 MB). Backend startup checks `/ml-models/`, downloads if missing, registers via local file URI; falls back to remote HF registration if local fails.

Pre-download for offline / airgapped:
```bash
DOWNLOAD_ALL_OPENSEARCH_MODELS=true bash scripts/download-models.sh models
```
Available model tiers in `backend/app/core/constants.py:OPENSEARCH_EMBEDDING_MODELS` (fast 384d, balanced 768d, multilingual variants).

> **Cosine score conversion (gotcha):** OpenSearch `cosinesimil` returns `(1 + cosine) / 2`, NOT raw cosine. All kNN score reads must do `raw_cosine = 2.0 * hit["_score"] - 1.0`. See memory file for the 8 fixed locations.

## Security

Backend containers run as non-root (`appuser`, UID 1000, group `video` for GPU). Multi-stage builds, health checks, Trivy scanned before release.

## Service Endpoints (development)

| Service | URL |
|---|---|
| Frontend | http://localhost:5173 |
| Backend API | http://localhost:5174/api |
| API docs (Swagger) | http://localhost:5174/docs |
| MinIO console | http://localhost:5179 |
| Flower | http://localhost:5175/flower |
| OpenSearch | http://localhost:5180 |

## Conventions

- **Docker compose layering**: base `docker-compose.yml` + auto-loaded `docker-compose.override.yml` (dev) OR explicit `-f` flags for prod / nginx / pki / offline / gpu-scale / local. Mixing dev + prod requires explicit flags (override is NOT auto-loaded then).
- Always `docker compose` (with space), never the legacy `docker-compose`.
- Conventional commits: `<type>(<scope>): <summary>`.
- `.env` is never overwritten without confirmation; `.env.example` is the editable template — keep new vars in sync.
- Keep code files under ~300 lines; Google-style Python docstrings; light/dark mode parity for any frontend change.
- No mocking in production code paths — mocks belong in test fixtures only.
- Real integration testing: if a test depends on Redis/Postgres/OpenSearch, run against the real service or document the dependency. Don't silently skip.
