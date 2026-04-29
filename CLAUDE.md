# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⚠️ CRITICAL: Local Code vs Docker Hub Images

**When developing and testing, you MUST use locally built Docker images, NOT Docker Hub images.** The Docker Hub images contain older compiled code and will NOT include your local changes. This applies to BOTH frontend AND backend containers.

**Before testing ANY code changes:**
```bash
# Rebuild frontend from local code
docker build -t davidamacey/opentranscribe-frontend:latest -f frontend/Dockerfile.prod frontend/

# Rebuild backend from local code
docker build -t davidamacey/opentranscribe-backend:latest -f backend/Dockerfile.prod backend/

# Restart changed services
docker stop opentranscribe-frontend && docker rm opentranscribe-frontend
docker stop opentranscribe-backend && docker rm opentranscribe-backend
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.nginx.yml -f docker-compose.local.yml -f docker-compose.pki.yml up -d --no-deps --force-recreate frontend backend
```

**Why this matters:**
- `docker-compose.prod.yml` uses the Docker Hub image tag (`davidamacey/opentranscribe-*:latest`)
- `docker-compose.local.yml` sets `pull_policy: never` but does NOT mount local source code
- The `docker-compose.override.yml` (dev mode with Vite/hot-reload) is NOT loaded when using explicit `-f` flags
- **If you see 404 errors on API endpoints or missing UI features, the container is running old Docker Hub code**

## Project Architecture

OpenTranscribe is a containerized AI-powered transcription application with these core services:
- **Frontend**: Svelte/TypeScript SPA with Progressive Web App capabilities
- **Backend**: FastAPI with async support and OpenAPI documentation
- **Database**: PostgreSQL with SQLAlchemy ORM and Alembic migrations
- **Storage**: MinIO S3-compatible object storage
- **Search**: OpenSearch 3.4.0 (Apache Lucene 10) for full-text and vector search
- **Queue**: Celery with Redis for background AI processing
- **Monitoring**: Flower for task monitoring

### Key Technologies
- **AI Models**: WhisperX for transcription (100+ languages), PyAnnote for speaker diarization
- **Frontend**: Svelte, TypeScript, Vite, Plyr for media playback, i18n (7 UI languages)
- **Backend**: FastAPI, SQLAlchemy 2.0, Alembic, Celery
- **Infrastructure**: Docker Compose, NGINX for production

## Development Commands

### Primary Development Script
Use `./opentr.sh` for all development operations:

```bash
# Start development environment
./opentr.sh start dev

# Stop all services
./opentr.sh stop

# View logs (all or specific service)
./opentr.sh logs [backend|frontend|postgres|celery-worker]

# Reset environment (WARNING: deletes all data)
./opentr.sh reset dev

# Check service status
./opentr.sh status

# Access container shell
./opentr.sh shell [backend|frontend|postgres]

# Database backup/restore
./opentr.sh backup
./opentr.sh restore backups/backup_file.sql

# Multi-GPU scaling (optional - for high-throughput systems)
./opentr.sh start dev --gpu-scale
./opentr.sh reset dev --gpu-scale
```

### Multi-GPU Worker Scaling (Optional)

For systems with multiple GPUs, you can enable parallel GPU workers to significantly increase transcription throughput.

**Use Case**: You have multiple GPUs and want to maximize processing speed by running multiple transcription workers in parallel.

**Example Hardware Setup**:
- GPU 0: NVIDIA RTX A6000 (49GB) - Running LLM model
- GPU 1: RTX 3080 Ti (12GB) - Default single worker (disabled when scaling)
- GPU 2: NVIDIA RTX A6000 (49GB) - Scaled workers (4 parallel)

**Configuration** (in `.env`):
```bash
GPU_SCALE_ENABLED=true      # Enable multi-GPU scaling
GPU_SCALE_DEVICE_ID=2       # Which GPU to use (default: 2)
GPU_SCALE_WORKERS=4         # Number of parallel workers (default: 4)
```

**Usage**:
```bash
# Start with GPU scaling enabled
./opentr.sh start dev --gpu-scale

# Reset with GPU scaling enabled
./opentr.sh reset dev --gpu-scale

# View scaled worker logs
docker compose logs -f celery-worker-gpu-scaled
```

**How It Works**:
- When `--gpu-scale` flag is used, the system loads `docker-compose.gpu-scale.yml` overlay
- Default single GPU worker is disabled (`scale: 0`)
- A new single container is created with `concurrency=4` (configurable via `GPU_SCALE_WORKERS`)
- The container runs 4 parallel Celery workers within a single process
- All workers target the specified GPU device and process from the `gpu` queue
- Celery automatically distributes tasks across the worker pool

**Performance**: With 4 parallel workers on a high-end GPU like the A6000, you can process 4 videos simultaneously, significantly reducing total processing time for batches of media files.

**Scaling**: Simply change `GPU_SCALE_WORKERS` in your `.env` file to adjust the number of concurrent workers (e.g., 2, 4, 6, 8) based on your GPU's memory and processing capacity.

### GPU Split Mode (Optional — Advanced)

For systems with two dedicated GPUs, you can split transcription and diarization onto separate GPUs. This is different from `--gpu-scale`: instead of running multiple complete pipelines in parallel, it routes each stage of a single file's pipeline to a dedicated GPU.

**Use Case**: You have two GPUs and want to dedicate one entirely to Whisper (transcription) and the other entirely to PyAnnote (diarization). Useful when VRAM is tight and you don't want the two models competing.

**How It Works**:
- `--with-gpu-split` activates the `gpu-split` Docker Compose profile
- Starts `celery-worker-gpu-transcribe` (transcription only, `gpu-transcribe` queue)
- Starts `celery-worker-gpu-diarize` (diarization only, `gpu-diarize` queue)
- Each file flows: CPU preprocess → GPU A (Whisper) → GPU B (PyAnnote) → CPU finalize
- The task routing checks `ENGINE_GPU_SPLIT=true` (set automatically in each worker's environment)

**Configuration** (in `.env`):
```bash
GPU_TRANSCRIBE_DEVICE_ID=0   # CUDA device for transcription worker (WhisperX)
GPU_DIARIZE_DEVICE_ID=1      # CUDA device for diarization worker (PyAnnote)
ENGINE_SHARED_VOLUME_PATH=/tmp/transcription  # Must use shared volume mount for cross-container WAV handoff
```

**Usage**:
```bash
# Start with GPU split enabled
./opentr.sh start dev --with-gpu-split

# Reset with GPU split enabled
./opentr.sh reset dev --with-gpu-split

# View split worker logs
docker compose logs -f celery-worker-gpu-transcribe
docker compose logs -f celery-worker-gpu-diarize
```

**vs. `--gpu-scale`**:
| Feature | `--gpu-scale` | `--with-gpu-split` |
|---|---|---|
| What it does | Multiple complete pipelines on one GPU | One pipeline split across two GPUs |
| Best for | High file throughput (many files) | VRAM-constrained two-GPU setups |
| Queue | `gpu` | `gpu-transcribe` + `gpu-diarize` |
| Extra overlay | `docker-compose.gpu-scale.yml` | None (profile in main yml) |

### Authentication Method Deployment

OpenTranscribe supports multiple authentication methods. Use these commands to deploy with specific auth configurations:

**PKI Certificate Authentication (Production Only):**
```bash
# Production with PKI (test before push)
./opentr.sh start prod --build --with-pki

# Production with PKI (Docker Hub images)
./opentr.sh start prod --with-pki

# Access: https://localhost:5182
# Requires client certificate (.p12 files in scripts/pki/test-certs/clients/)

# Note: PKI requires nginx with mTLS and only works in production mode
# Dev mode uses Vite dev server which cannot handle client certificate verification
```

**LDAP/Active Directory Testing:**
```bash
# Development with LDAP test container
./opentr.sh start dev --with-ldap-test

# Production with LDAP test container (test before push)
./opentr.sh start prod --build --with-ldap-test

# LDAP server: localhost:3890
# Web UI: http://localhost:17170
# Admin: admin / admin_password
```

**Keycloak/OIDC Testing:**
```bash
# Development with Keycloak test container
./opentr.sh start dev --with-keycloak-test

# Production with Keycloak test container (test before push)
./opentr.sh start prod --build --with-keycloak-test

# Keycloak URL: http://localhost:8180
# Admin credentials: admin / admin
```

**Combined Authentication Methods:**
```bash
# LDAP + Keycloak testing (dev mode)
./opentr.sh start dev --with-ldap-test --with-keycloak-test

# All auth methods including PKI (production only)
./opentr.sh start prod --build --with-pki --with-ldap-test --with-keycloak-test
```

**Configuration Notes:**
- Configure auth methods via Admin UI: Settings → Authentication
- Database configuration takes precedence over `.env` variables
- PKI requires nginx with mTLS (automatically enabled with `--with-pki`)
- LDAP/Keycloak test containers are for development only
- Production deployments should connect to organization's AD/Keycloak servers

**Documentation:**
- PKI Setup: `docs/PKI_SETUP.md`
- LDAP/AD Setup: `docs/LDAP_AUTH.md`
- Keycloak Setup: `docs/KEYCLOAK_SETUP.md`

### Docker Build & Push (Production Images)

Build and push production Docker images to Docker Hub:

```bash
# Build and push both services (multi-arch: amd64 + arm64 via remote Mac Studio builder)
# ALWAYS use USE_REMOTE_BUILDER=true — without it ARM64 uses QEMU and takes 2-3 hours
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh

# Build specific service only
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh backend
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh frontend

# Quick iteration (skip security scan — use when testing changes back-to-back)
USE_REMOTE_BUILDER=true SKIP_SECURITY_SCAN=true ./scripts/docker-build-push.sh backend

# Auto-detect changes and build only what changed
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh auto

# Build for single platform (faster testing, no remote builder needed)
PLATFORMS=linux/amd64 ./scripts/docker-build-push.sh backend
```

See [scripts/README.md](scripts/README.md) for detailed documentation.

### Local Development Builds (No Docker Hub Push)

**CRITICAL: Production/nginx testing always requires locally built images.** The Docker Hub images contain older compiled code. When running with prod overlays (`docker-compose.prod.yml`, `docker-compose.nginx.yml`, `docker-compose.pki.yml`), the frontend serves pre-compiled static assets from the Docker image — NOT the local source code. You **must** rebuild locally after any frontend or backend code changes.

```bash
# Build backend image locally
docker build -t davidamacey/opentranscribe-backend:latest -f backend/Dockerfile.prod backend/

# Build frontend image locally
docker build -t davidamacey/opentranscribe-frontend:latest -f frontend/Dockerfile.prod frontend/

# Restart with local images (use docker-compose.local.yml to prevent pulling)
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.local.yml up -d --force-recreate
```

**When using PKI/nginx overlays**, rebuild and recreate only the frontend:
```bash
docker build -t davidamacey/opentranscribe-frontend:latest -f frontend/Dockerfile.prod frontend/
docker stop opentranscribe-frontend && docker rm opentranscribe-frontend
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.nginx.yml -f docker-compose.local.yml -f docker-compose.pki.yml up -d --no-deps --force-recreate frontend
```

**Important - Docker Build Caching:**
- Docker uses content hashing for COPY layers - if file content hasn't changed, the layer is cached
- If code changes aren't being picked up, touch a file or add a comment to invalidate the cache:
  ```bash
  echo "<!-- Build: $(date +%s) -->" >> frontend/src/components/SomeFile.svelte
  docker build -t davidamacey/opentranscribe-frontend:latest -f frontend/Dockerfile.prod frontend/
  ```
- **Avoid `--no-cache`** for routine builds - it rebuilds everything including `npm ci` which is slow
- Only use `--no-cache` when dependencies change or you suspect deep caching issues

**Important - Local vs Docker Hub Images:**
- `docker-compose.local.yml` sets `pull_policy: never` to prevent overwriting local images with Docker Hub versions
- `--force-recreate` ensures containers use the new image
- **Never use `./opentranscribe.sh update` with local builds** (it pulls from Docker Hub)
- When testing local code changes, always use the local compose overlay
- **The `docker-compose.override.yml` (dev Vite server) is NOT loaded when using explicit `-f` flags** — only the specified files are used

### Frontend Development
```bash
# From frontend/ directory
npm run dev          # Start dev server
npm run build        # Production build
npm run check        # Type checking
```

### Browser Automation (Claude Code)

System-wide Playwright browser automation for testing and debugging. Claude Code can use this to interact with the frontend, capture screenshots, see console errors, and automate UI testing.

**Location:** `~/bin/browser-tools/` (see `~/bin/browser-tools/README.md` for full docs)

**Setup (one-time):**
```bash
sudo npm install -g playwright
npx playwright install chromium
cd ~/bin/browser-tools && npm install
```

**Usage:**
```bash
# Basic - open URL, screenshot, check for errors
node ~/bin/browser-tools/browse.js http://localhost:5173

# With visible browser on XRDP (display :13)
node ~/bin/browser-tools/browse.js http://localhost:5173 --display=:13

# Login flow example
node ~/bin/browser-tools/browse.js http://localhost:5173 --display=:13 \
  'fill:#email:admin@example.com' \
  'fill:#password:password' \
  'click:button[type=submit]' \
  'wait:2000' \
  'screenshot:after-login'
```

**Actions:**
| Action | Description |
|--------|-------------|
| `fill:<selector>:<value>` | Fill input field |
| `click:<selector>` | Click element |
| `screenshot:<name>` | Save screenshot |
| `wait:<ms>` | Wait milliseconds |
| `waitfor:<selector>` | Wait for element |
| `eval:<javascript>` | Execute JS, print result |
| `title` | Print page title |
| `text` | Print page text |

**Options:**
- `--display=:13` - Show browser on XRDP display (user can watch)
- `--keep` - Keep browser open after actions
- `--timeout=30000` - Navigation timeout

**Screenshots saved to:** `~/bin/browser-tools/screenshots/`

**Test credentials:** `admin@example.com` / `password`

### E2E Testing (pytest + Playwright)

End-to-end tests that verify frontend and backend work together through real browser automation.

**Location:** `backend/tests/e2e/`

**Test Files:**
- `conftest.py` - Fixtures (login_page, authenticated_page, auth_helper, api_helper)
- `test_login.py` - Comprehensive login tests (~50 tests)
- `test_registration.py` - Comprehensive registration tests (~35 tests)
- `test_auth_flow.py` - Combined auth flow tests

**Running E2E Tests:**
```bash
# Activate venv first
source backend/venv/bin/activate

# Run all E2E tests (headless - fast)
pytest backend/tests/e2e/ -v

# Run with visible browser on XRDP (watch the tests)
DISPLAY=:11 pytest backend/tests/e2e/ -v --headed

# Run specific test file
pytest backend/tests/e2e/test_login.py -v

# Run specific test class
pytest backend/tests/e2e/test_login.py::TestLoginSuccess -v --headed

# Run with screenshots on failure
pytest backend/tests/e2e/ -v --screenshot only-on-failure
```

**Test Categories:**
| Category | Tests | Coverage |
|----------|-------|----------|
| Login | ~50 | Form validation, success/failure, security, session, UI |
| Registration | ~35 | All fields, username/email/password validation, duplicates |
| Auth Flow | ~15 | Login → use app → logout, session persistence |

**Requirements:**
- Dev environment running (`./opentr.sh start dev`)
- Frontend at `localhost:5173`
- Backend at `localhost:5174`
- pytest-playwright installed (`pip install pytest-playwright`)

**Fixtures Available:**
- `login_page` - Page navigated to login, ready for input
- `authenticated_page` - Already logged in as admin
- `auth_helper` - Helper for login/logout/register operations
- `api_helper` - Helper for backend API calls during tests

### Backend Development
```bash
# From backend/ directory (or via container)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
pytest tests/        # Run tests
alembic upgrade head # Apply migrations (production only)
```

### Python Virtual Environment

**IMPORTANT**: Always use the virtual environment at `backend/venv` for all Python operations outside of Docker containers.

```bash
# Activate the virtual environment
source backend/venv/bin/activate

# Install dependencies (if needed)
pip install -r backend/requirements.txt

# Run pre-commit hooks
pre-commit run --all-files

# Run linting tools
ruff check backend/
ruff format backend/

# Run type checking
mypy backend/app

# Run tests
pytest backend/tests/

# Deactivate when done
deactivate
```

**When to use the venv:**
- Running pre-commit hooks locally
- Running mypy, ruff, bandit, or other linting tools
- Installing Python packages for development
- Running pytest outside of Docker
- Any Python CLI tools (black, isort, etc.)

**Note**: The virtual environment should already be set up. If not, create it with:
```bash
cd backend
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install pre-commit mypy ruff bandit
```

## Database Management

OpenTranscribe uses **Alembic migrations** for all database schema changes. Migrations run automatically on backend startup, ensuring existing user data is preserved during upgrades.

### Adding Schema Changes (The Standard Process)

When you need to add columns, indexes, or constraints:

1. **Create an Alembic migration** in `backend/alembic/versions/`:
   ```python
   # Example: backend/alembic/versions/v070_add_pki_security_enhancements.py
   revision = "v070_add_pki_security_enhancements"
   down_revision = "v060_add_transcript_overlap"  # Previous migration

   def upgrade():
       # Use idempotent SQL (IF NOT EXISTS) to be safe
       op.execute("""
           DO $$
           BEGIN
               IF NOT EXISTS (
                   SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'user' AND column_name = 'new_column'
               ) THEN
                   ALTER TABLE "user" ADD COLUMN new_column VARCHAR(256);
               END IF;
           END $$;
       """)

   def downgrade():
       op.execute('ALTER TABLE "user" DROP COLUMN IF EXISTS new_column')
   ```

2. **Update SQLAlchemy models** in `backend/app/models/` to match

3. **Update Pydantic schemas** in `backend/app/schemas/` if needed

4. **Update migration detection** in `backend/app/db/migrations.py`:
   - Add detection logic for the new schema version
   - Update the latest version check

### Migration Files

| File | Purpose |
|------|---------|
| `backend/alembic/versions/*.py` | Alembic migrations (versioned, the schema authority) |
| `backend/app/db/migrations.py` | Auto-detection and startup runner |
| `database/init_db.sql` | Legacy reference only (no longer used for schema) |

### How Migrations Run

On backend startup, `migrations.py` automatically:
1. Detects current database schema version
2. Stamps untracked databases with the appropriate version
3. Runs any pending Alembic migrations to bring DB to current version

### Development Workflow

- **Testing schema changes**: Use `./opentr.sh reset dev` (deletes data, runs full migration chain)
- **Testing migrations**: Rebuild backend and restart (migrations run on startup)
- **Production upgrades**: Just restart the backend - migrations apply automatically

## Code Organization Patterns

### Backend Structure
- `app/api/endpoints/` - REST API routes organized by resource
- `app/models/` - SQLAlchemy ORM models
- `app/schemas/` - Pydantic validation schemas
- `app/services/` - Business logic and external integrations
- `app/tasks/` - Celery background tasks
- `app/core/` - Configuration and security

### Frontend Structure
- `src/components/` - Reusable Svelte components
- `src/components/settings/` - Settings-related components
- `src/routes/` - Page components
- `src/stores/` - Svelte stores for state management
- `src/lib/` - Utilities and services
- `src/lib/i18n/` - Internationalization system (7 languages)
- `src/lib/i18n/locales/` - Translation JSON files (en, es, fr, de, pt, zh, ja)

### Key Patterns
- **Authentication**: Hybrid multi-method with JWT tokens and refresh token rotation
- **File Processing**: Upload to MinIO → Celery task → AI processing → Database storage
- **Real-time Updates**: WebSockets for task progress notifications
- **Error Handling**: Structured error responses with proper HTTP status codes

### Authentication System

OpenTranscribe supports multiple authentication methods that can be enabled simultaneously (hybrid authentication).

**Available Authentication Methods:**
- **Local (Direct)**: Username/password with bcrypt hashing
- **LDAP/Active Directory**: Enterprise directory integration
- **OIDC/Keycloak**: OpenID Connect with external identity providers
- **PKI/X.509**: Certificate-based authentication for high-security environments

**Authentication Module Structure:**
```
backend/app/auth/
├── direct_auth.py       # Local password authentication
├── ldap_auth.py         # LDAP/Active Directory integration
├── keycloak_auth.py     # OIDC/Keycloak integration
├── pki_auth.py          # PKI/X.509 certificate authentication
├── mfa.py               # TOTP multi-factor authentication
├── password_policy.py   # Password strength enforcement
├── password_history.py  # Password reuse prevention
├── rate_limit.py        # Authentication rate limiting
├── lockout.py           # Account lockout management
├── session.py           # Session/token management
├── token_service.py     # JWT token operations
├── audit.py             # Authentication audit logging
└── constants.py         # Auth-related constants
```

**Authentication Database Models:**
- `UserMFA` - MFA/TOTP settings per user (`backend/app/models/user_mfa.py`)
- `PasswordHistory` - Password history tracking (`backend/app/models/password_history.py`)
- `RefreshToken` - Refresh token management (`backend/app/models/refresh_token.py`)

**Key Authentication Patterns:**
- **Hybrid Auth**: Multiple methods can be enabled simultaneously via `AUTH_TYPE` config
- **Token Flow**: JWT access tokens (short-lived) + refresh token rotation (long-lived)
- **External IdP Users**: PKI/Keycloak users bypass local MFA (handled by IdP)
- **Rate Limiting**: Per-IP and per-user rate limits to prevent brute force
- **Account Lockout**: Configurable lockout after failed attempts
- **Audit Logging**: All auth events logged for security compliance

**Configuration:**
- Set `AUTH_TYPE` in `.env` (local, ldap, keycloak, pki, or comma-separated for hybrid)
- Provider-specific settings documented in `docs/KEYCLOAK_SETUP.md` and `docs/PKI_SETUP.md`
- MFA can be enforced globally or per-user

## Development Guidelines

### Production Quality Standards
This is a professional production application. All code must meet production-quality standards:
- **No cheating or workarounds**: Every feature must work correctly end-to-end. No skipping validation, no placeholder implementations, no "good enough" shortcuts
- **No mocking in production code**: Mocks are only acceptable in test fixtures. Production code paths must use real implementations
- **Industry standards compliance**: All security features (MFA, authentication, encryption) must follow established RFCs and industry standards (e.g., RFC 6238 for TOTP, RFC 4226 for HOTP)
- **Real integration testing**: If a test requires external services (Redis, PostgreSQL, OpenSearch), run the test against real services or clearly document the dependency. Do not silently skip critical test paths
- **Authenticator app compatibility**: MFA/TOTP must be compatible with standard authenticator apps (Google Authenticator, Microsoft Authenticator, Authy, etc.)

### Code Quality
- Keep files under 200-300 lines
- Use Google-style docstrings for Python code
- Follow existing patterns before creating new ones
- Always check for TypeScript errors
- Ensure light/dark mode compliance for frontend changes

### Frontend Build Verification

A pre-commit hook automatically runs `svelte-check` and `vite build` when frontend source files are modified.

**Behavior:**
- Only triggers when `.svelte`, `.ts`, `.js`, `.css`, or `.html` files under `frontend/src/` are staged
- Runs `svelte-check --threshold warning` (fails on both errors and warnings)
- Runs `vite build` to verify the production build succeeds
- If Claude CLI is available, attempts automatic fixes on failure
- Falls back to showing error output if Claude is unavailable or cannot fix

**Manual usage:**
```bash
# Run the frontend check script directly
./scripts/frontend-check.sh

# Run without Claude auto-fix
./scripts/frontend-check.sh --no-claude

# Run only svelte-check (skip build, faster)
./scripts/frontend-check.sh --check-only

# Use Claude Code skill to fix frontend errors (inside a Claude Code session)
/fix-frontend
```

### Docker and Services
- Use `docker compose` (not `docker-compose`)
- **Docker Compose Structure** (base + override pattern):
  - `docker-compose.yml` - Base configuration (all environments)
  - `docker-compose.override.yml` - Development overrides (auto-loaded)
  - `docker-compose.prod.yml` - Production overrides
  - `docker-compose.offline.yml` - Offline/airgapped overrides
- **Development**: Just run `docker compose up` (auto-loads override)
- **Production**: Use `-f docker-compose.yml -f docker-compose.prod.yml`
- **Offline**: Use `-f docker-compose.yml -f docker-compose.offline.yml`
- Always check container logs after starting services
- Kill existing servers before testing changes
- Layer Docker files for optimal caching

### Testing and Deployment
- Write thorough tests for major functionality
- No mocking data for dev/prod (tests only)
- Always restart/reset services after making changes
- Use appropriate opentr.sh commands for testing changes

## Service Endpoints

### Development URLs
- Frontend: http://localhost:5173
- Backend API: http://localhost:5174/api
- API Docs: http://localhost:5174/docs
- MinIO Console: http://localhost:5179
- Flower Dashboard: http://localhost:5175/flower
- OpenSearch: http://localhost:5180 (v3.4.0 with Lucene 10)

### Important File Locations
- Environment config: `.env` (never overwrite without confirmation)
- Environment template: `.env.example` (freely editable - keep in sync when adding new env vars)
- Database migrations: `backend/alembic/versions/` (schema authority)
- Docker base config: `docker-compose.yml` (common to all environments)
- Docker dev config: `docker-compose.override.yml` (auto-loaded in dev)
- Docker prod config: `docker-compose.prod.yml` (production overrides)
- Docker offline config: `docker-compose.offline.yml` (airgapped overrides)
- Frontend build: `frontend/vite.config.ts`

## AI Processing Workflow

1. File upload to MinIO storage
2. Metadata extraction and database record creation
3. Celery task dispatch to worker with GPU support
4. WhisperX transcription with native word-level timestamps (100+ languages supported)
   - Configurable source language (auto-detect or specify)
   - Optional translation to English
   - All 100+ languages support word-level timestamps natively via faster-whisper cross-attention DTW
5. PyAnnote speaker diarization and voice fingerprinting
6. **LLM speaker identification suggestions** (optional - manual verification required)
7. **LLM-powered summarization** with BLUF format (optional - user-triggered)
   - Automatic context-aware processing (single or multi-section based on transcript length)
   - Intelligent chunking at speaker/topic boundaries for long content
   - Section-by-section analysis with final summary stitching
   - Configurable output language (12 languages supported)
8. Database storage and OpenSearch indexing
9. WebSocket notification to frontend

### Whisper Model Selection

OpenTranscribe defaults to `large-v3-turbo` for optimal speed. Users can override via `WHISPER_MODEL` environment variable.

**Model Comparison:**

| Model | Speed | VRAM | English | Multilingual | Translation | Best For |
|-------|-------|------|---------|--------------|-------------|----------|
| `large-v3-turbo` | **6x faster** | ~6GB | Excellent | Good* | **NO** | English, speed-critical |
| `large-v3` | Slow | ~10GB | Excellent | **Best** | Yes | Non-English, translation |
| `large-v2` | Slow | ~10GB | Excellent | Good | Yes | Legacy, translation |

*Turbo shows slightly reduced accuracy for Thai and Cantonese compared to large-v2

**Language-Specific Recommendations:**

| Use Case | Recommended Model | Why |
|----------|-------------------|-----|
| English podcasts/meetings | `large-v3-turbo` | 6x faster, excellent English accuracy |
| Spanish, French, German, Japanese | `large-v3-turbo` | Good accuracy, much faster |
| Thai, Cantonese, Vietnamese | `large-v3` | Turbo has reduced accuracy for these |
| Low-resource languages | `large-v3` | 10-20% better than other models |
| Translation to English | `large-v3` | **Turbo cannot translate** |
| Maximum accuracy (any language) | `large-v3` | Best overall accuracy |

**Critical Translation Warning:** `large-v3-turbo` was NOT trained for translation tasks. If users enable "Translate to English" in settings, they should use `large-v3` or `large-v2`.

**Configuration:**
```bash
# In .env file
WHISPER_MODEL=large-v3-turbo  # Default - 6x faster
WHISPER_MODEL=large-v3        # For translation or maximum accuracy
WHISPER_MODEL=large-v2        # Legacy fallback
```

### Hybrid Mode (CPU Transcription + GPU/MPS Diarization)

For low-VRAM GPUs and macOS, OpenTranscribe auto-activates **hybrid mode**: transcription runs on CPU while diarization remains on GPU/MPS. This requires only ~1.3 GB VRAM for PyAnnote.

**Auto-activation conditions:**
- macOS (MPS): always hybrid — faster-whisper MPS support is unreliable; PyAnnote MPS (fork) is production-ready
- CUDA GPU: activates when the minimum batch=2 VRAM peak for the configured model exceeds 80% of total GPU VRAM
  - large-v3-turbo / large-v3: min peak 3,893 MB → triggers on GPUs < ~4.9 GB
  - medium: min peak 3,829 MB → triggers on GPUs < ~4.8 GB
  - small: min peak 2,933 MB → triggers on GPUs < ~3.7 GB

**Key files:**
- `backend/app/utils/hardware_detection.py` — `should_use_hybrid_mode()`, `_min_peak_mb()`
- `backend/app/transcription/config.py` — `diarization_device` field, hybrid branch in `from_environment()`
- `backend/app/transcription/diarizer.py` — uses `config.diarization_device` (not `config.device`)

**Environment variables:**
```bash
WHISPER_HYBRID_MODE=auto          # auto (default) | true | false
WHISPER_HYBRID_CPU_MODEL=small    # CPU model in hybrid mode: small | medium | base
```

**Benchmark data:** `docs/whisper-vram-profile/README.md`

### LLM Features

The application now includes optional AI-powered features using Large Language Models:

**AI Summarization:**
- BLUF (Bottom Line Up Front) format summaries
- Speaker analysis with talk time and key contributions
- Action items extraction with priorities and assignments
- Key decisions and follow-up items identification
- Support for multiple LLM providers (vLLM, OpenAI, Ollama, Claude)
- Intelligent section-by-section processing for transcripts of any length
- Automatic context-aware chunking and summary stitching
- **Multilingual output**: Generate summaries in 12 languages (en, es, fr, de, pt, zh, ja, ko, it, ru, ar, hi)

**Speaker Identification:**
- LLM-powered speaker name suggestions based on conversation context
- Confidence scoring for identification accuracy
- Manual verification workflow (suggestions are not auto-applied)
- Cross-video speaker matching with embedding analysis
- Speaker merge UI for combining duplicate speakers

**Model Discovery:**
- Automatic discovery of available models for vLLM, Ollama, and Anthropic
- Works with OpenAI-compatible API endpoints
- Edit mode supports stored API keys (no need to re-enter)

**Configuration:**
- Set `LLM_PROVIDER` in .env file (vllm, openai, ollama, anthropic, openrouter)
- Configure provider-specific settings (API keys, endpoints, models)
- Features work independently - transcription works without LLM configuration

**Deployment Options:**
- **Cloud Providers**: Use `.env` configuration with external providers (OpenAI, Claude, OpenRouter, etc.)
- **Self-Hosted LLM**: Configure vLLM or Ollama endpoints in `.env` (deployed separately)
- **No LLM**: Leave LLM_PROVIDER empty for transcription-only mode

### Multilingual Transcription (New)

OpenTranscribe now supports transcription in 100+ languages with configurable settings:

**User-Configurable Language Settings:**
- **Source Language**: Auto-detect or specify language for improved accuracy
- **Translate to English**: Toggle to translate non-English audio to English
- **LLM Output Language**: Generate AI summaries in preferred language (12 supported)

**Language Features:**
- All 100+ languages support word-level timestamps natively via faster-whisper cross-attention DTW
- Settings stored per-user in the database

**Configuration:**
- User settings available in Settings → Transcription → Language Settings
- Per-file language override available at upload/reprocess time

### User-Level Settings

Users can configure their own transcription preferences:

**Transcription Settings:**
- **Speaker Behavior**: Always prompt, use defaults, or use saved custom values
- **Min/Max Speakers**: Configure default speaker detection range (1-50+)
- **Garbage Cleanup**: Enable/disable automatic cleanup of erroneous segments

**Recording Settings:**
- Audio recording quality and duration preferences
- Microphone device selection

### Universal Media URL Support

OpenTranscribe supports downloading and processing videos from 1800+ platforms via yt-dlp integration:

**Supported Platforms:**
- **Best Support (Recommended)**: YouTube, Dailymotion, TikTok - most reliable for public video downloads
- **Limited Support**: Vimeo, Twitter/X, Instagram, Facebook - may require authentication for many videos
- **Other Platforms**: Twitch, Reddit, SoundCloud, and 1800+ more sites supported by yt-dlp

**Platform Limitations:**
- Some platforms (Vimeo, Instagram, Facebook) restrict most videos to logged-in users
- Authentication-required videos cannot be downloaded without browser cookies
- Age-restricted, geo-restricted, or private videos are not accessible
- Premium/subscriber-only content requires active subscriptions

**User-Friendly Error Messages:**
- The system detects authentication-related errors and provides helpful guidance
- When a video fails, users receive platform-specific suggestions
- Recommends alternative platforms when authentication issues are detected

**How It Works:**
1. User enters a video URL from any supported platform
2. System validates the URL and extracts video metadata via yt-dlp
3. Video is downloaded in web-compatible format (H.264/MP4 preferred)
4. Downloaded video is uploaded to MinIO storage
5. Standard transcription pipeline processes the video
6. WebSocket notification updates the frontend

**Configuration:**
- No additional configuration required - yt-dlp is included in the backend container
- Anti-blocking measures included for YouTube (client rotation, proper headers)
- Maximum video duration: 4 hours
- Maximum file size: 15GB (same as direct upload limit)

## Model Caching System

OpenTranscribe uses a simple volume-based model caching system that automatically persists AI models between container restarts.

### Configuration
- Set `MODEL_CACHE_DIR` in `.env` to specify cache location (default: `./models`)
- Models are automatically downloaded on first use
- All models persist across container restarts and rebuilds

### Directory Structure
```
${MODEL_CACHE_DIR}/
├── huggingface/          # HuggingFace models cache
│   ├── hub/             # WhisperX models (~1.5GB)
│   └── transformers/    # PyAnnote transformer cache
├── torch/               # PyTorch models cache
│   └── pyannote/        # PyAnnote speaker models (~500MB)
├── nltk_data/           # NLTK data files
│   ├── tokenizers/      # punkt_tab tokenizer (~13MB)
│   └── taggers/         # POS taggers
├── sentence-transformers/ # Sentence transformers models
│   └── sentence-transformers_all-MiniLM-L6-v2/ # Semantic search model (~80MB)
└── opensearch-ml/       # OpenSearch neural search models
    ├── all-MiniLM-L6-v2/ # Default model for neural search (~80MB)
    │   └── sentence-transformers_all-MiniLM-L6-v2-1.0.1-torch_script.zip
    └── model_manifest.json # Download metadata
```

### Speaker Diarization Configuration

**MIN_SPEAKERS / MAX_SPEAKERS Parameters:**

PyAnnote's speaker diarization uses sklearn's `AgglomerativeClustering`, which has **NO hard maximum limit** on the number of speakers:
- Default: `MIN_SPEAKERS=1`, `MAX_SPEAKERS=20`
- Can be increased to 50+ for large conferences/events with many speakers
- No hard upper limit - only constrained by the number of audio samples
- Performance threshold at `max(100, 0.02 * n_samples)` where algorithm behavior changes for efficiency

**Use Cases:**
- Small meetings: 2-5 speakers (default works fine)
- Medium meetings: 5-15 speakers (default works fine)
- Large conferences: 15-50 speakers (increase MAX_SPEAKERS to 30-50)
- Very large events: 50+ speakers (increase MAX_SPEAKERS accordingly)

**Note**: Higher values may impact processing time but will not cause errors.

**Embedding batch size — pinned at 16 (Phase A, 2026-04-20):**

Speaker-embedding batch is hard-coded to 16 in `backend/app/transcription/diarizer.py` (`SpeakerDiarizer.EMBEDDING_BATCH_SIZE`). The fork's auto-scaler is bypassed via `PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE=16` set at pipeline load.

| Property | Value |
|---|---|
| Embedding batch size | **16** (fixed) |
| Diarization peak VRAM per pipeline | **~1 GB over process baseline** |
| Wall time @ 2.2 h fp32 | 103 s on RTX A6000 (vs. 100 s at bs=128 — 3 % upside not worth 7 GB) |
| DER vs. reference across bs ∈ {1..128} fp32 | 0 (speaker count + boundaries invariant) |

Why fixed rather than budget-aware: Phase A measured identical throughput from bs=16 to bs=128 at fp32, with no accuracy difference. Making the batch size constant simplifies the code, makes VRAM predictable for every deployment, and unlocks **multiple parallel diarization pipelines on a single GPU** — an A6000 can host ≈25 concurrent pipelines at 1 GB each.

Full raw data: `docs/diarization-vram-profile/README.md`. Run `python -m app.scripts.diarization_diag` to see the local GPU's headroom.

### Docker Volume Mappings
The system uses simple volume mappings to cache models to their natural locations:
```yaml
volumes:
  - ${MODEL_CACHE_DIR}/huggingface:/home/appuser/.cache/huggingface
  - ${MODEL_CACHE_DIR}/torch:/home/appuser/.cache/torch
  - ${MODEL_CACHE_DIR}/nltk_data:/home/appuser/.cache/nltk_data
  - ${MODEL_CACHE_DIR}/sentence-transformers:/home/appuser/.cache/sentence-transformers
  - ${MODEL_CACHE_DIR}/opensearch-ml:/home/appuser/.cache/opensearch-ml  # OpenSearch neural models

# OpenSearch container volume mapping (read-only)
  - ${MODEL_CACHE_DIR}/opensearch-ml:/ml-models:ro  # Mounted in OpenSearch container
```

### Key Benefits
- **No code complexity**: Models use their natural cache locations
- **Persistent storage**: Models saved between container restarts
- **User configurable**: Simple `.env` variable controls cache location
- **No re-downloads**: Models cached after first download (~2.5GB total)
- **Automatic setup**: OpenSearch neural models download automatically on first start
- **Offline capability**: Models persist for air-gapped deployments

### OpenSearch Neural Search Models

OpenTranscribe uses OpenSearch's native neural search for semantic/vector search capabilities:

**Model Management:**
- **Default Model**: `all-MiniLM-L6-v2` (384-dim, 80MB, English)
- **Auto-Download**: Default model downloads automatically on first start if missing
- **Offline Support**: Models persist in `${MODEL_CACHE_DIR}/opensearch-ml/` for air-gapped deployments
- **Fallback**: If model download fails, OpenSearch downloads from internet on-demand (temporary, not persisted)

**Download Methods:**

1. **Automatic (Recommended)**: Run `./opentr.sh start dev` - models download if missing
2. **Manual Pre-Download**: Run `bash scripts/download-models.sh models` before starting
3. **Offline Preparation**:
   ```bash
   # On internet-connected machine:
   DOWNLOAD_ALL_OPENSEARCH_MODELS=true bash scripts/download-models.sh models

   # Copy models/ directory to offline machine
   rsync -av models/ user@offline-machine:/opt/opentranscribe/models/
   ```

**Available Models** (see `backend/app/core/constants.py:OPENSEARCH_EMBEDDING_MODELS`):
- Fast tier (384d): `all-MiniLM-L6-v2` (default), `paraphrase-multilingual-MiniLM-L12-v2`
- Balanced tier (768d): `all-mpnet-base-v2`, `paraphrase-multilingual-mpnet-base-v2`
- Best tier: `all-distilroberta-v1` (768d), `distiluse-base-multilingual-cased-v1` (512d)

**Backend Startup Flow:**
1. Backend checks for local models in `/ml-models/` (OpenSearch container mount)
2. If default model missing, downloads to `~/.cache/opensearch-ml/` (backend container)
3. Volume mapping persists models to host `${MODEL_CACHE_DIR}/opensearch-ml/`
4. OpenSearch ML Commons registers model from local file (`file://`) for offline use
5. If local registration fails, falls back to remote HuggingFace registration (requires internet)

**Configuration** (`.env`):
```bash
OPENSEARCH_NEURAL_SEARCH_ENABLED=true  # Enable/disable neural search (default: true)
OPENSEARCH_NEURAL_MODEL=huggingface/sentence-transformers/all-MiniLM-L6-v2  # Model to use
```

## Security Features

### Non-Root Container User

OpenTranscribe backend containers run as a non-root user (`appuser`, UID 1000) following Docker security best practices.

**Benefits:**
- Follows principle of least privilege
- Reduces security risk from container escape vulnerabilities
- Compliant with security scanning tools (Trivy, Snyk, etc.)
- Prevents host root compromise in case of container breach

**Automatic Permission Management:**

The startup scripts (`./opentr.sh` and `./opentranscribe.sh`) automatically check and fix model cache permissions before starting containers. This ensures the non-root container user (UID 1000) can access the model cache without permission errors.

If you encounter permission issues, you can manually fix them:

```bash
# Fix permissions on existing model cache
./scripts/fix-model-permissions.sh
```

This script will change ownership of your model cache to UID:GID 1000:1000, making it accessible to the non-root container user.

**Technical Details:**
- Container user: `appuser` (UID 1000, GID 1000)
- User groups: `appuser`, `video` (for GPU access)
- Cache directories: `/home/appuser/.cache/huggingface`, `/home/appuser/.cache/torch`
- Multi-stage build for minimal attack surface
- Health checks for container orchestration

## Common Tasks

### Adding New API Endpoints
1. Create endpoint in `backend/app/api/endpoints/`
2. Add to router in `backend/app/api/router.py`
3. Create/update schemas in `backend/app/schemas/`
4. Update database models if needed
5. Test with `./opentr.sh restart-backend`

### Frontend Component Development
1. Create component in `src/components/`
2. Ensure light/dark mode support
3. Test responsive design
4. Update relevant routes/stores if needed
5. Test with `./opentr.sh restart-frontend`

### Database Changes
1. Create an Alembic migration in `backend/alembic/versions/`
2. Update SQLAlchemy models in `backend/app/models/`
3. Update Pydantic schemas in `backend/app/schemas/` if needed
4. Update `backend/app/db/migrations.py` detection logic for the new version
5. Test with `./opentr.sh reset dev` (runs full migration chain from scratch)
