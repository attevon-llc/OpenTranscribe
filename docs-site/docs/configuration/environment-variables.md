---
sidebar_position: 1
---

# Environment Variables

Comprehensive reference for all OpenTranscribe environment variables.

## Quick Reference

Edit `.env` file in installation directory. See `.env.example` for full template.

## GPU Configuration

```bash
TORCH_DEVICE=auto  # or: cuda, mps, cpu
USE_GPU=auto  # or: true, false
GPU_DEVICE_ID=0  # Which GPU (0, 1, 2, etc.)
COMPUTE_TYPE=auto  # or: float16, float32, int8
BATCH_SIZE=auto  # or: 8, 16, 32
```

## Model & Caching

Configure AI models, caching behavior, and model discovery.

```bash
# Whisper Transcription Models
WHISPER_MODEL=large-v3-turbo  # or: large-v3, large-v2, medium, small, base, tiny

# PyAnnote Speaker Diarization
PYANNOTE_VERSION=auto  # or: v3, v4 (auto-detect installed version)
EMBEDDING_MODE=auto  # or: v3, v4 (embedding model version)
MIN_SPEAKERS=1
MAX_SPEAKERS=20

# Model Caching & Storage
MODEL_CACHE_DIR=./models
HUGGINGFACE_CACHE=${MODEL_CACHE_DIR}/huggingface
TORCH_CACHE=${MODEL_CACHE_DIR}/torch
HUGGINGFACE_TOKEN=hf_your_token_here

# Warm Cache (Pre-load Models on Startup)
WARM_CACHE_ENABLED=false
```

### Transcription Performance Options

```bash
# Whisper beam_size: lower = faster but slightly less accurate (default: 5)
# Set to 1 for greedy decoding (~25-40% faster, ~1-2% lower WER for English)
WHISPER_BEAM_SIZE=5

# Whisper compute_type: quantization for faster inference
# Default: auto-detected (float16 on CUDA). Options: float16, int8_float16, int8, float32
# int8_float16 gives ~15-25% speedup with negligible quality loss
WHISPER_COMPUTE_TYPE=float16
```

### Model Recommendations

| Use Case | Model | Notes |
|----------|-------|-------|
| English (primary) | `large-v3-turbo` | 6x faster, excellent English accuracy |
| Multilingual | `large-v3` | Best accuracy for 100+ languages |
| Translation to English | `large-v3` | Turbo cannot translate |
| Speed-critical | `large-v3-turbo` | Recommended for most use cases |
| Maximum accuracy | `large-v3` | Slower but best overall |

### Whisper Model VRAM Requirements

| Model | Batch Size 1 | Batch Size 8 | Batch Size 16 |
|-------|-------------|-------------|--------------|
| `tiny` | ~1GB | ~2GB | ~3GB |
| `base` | ~1GB | ~2GB | ~3GB |
| `small` | ~2GB | ~4GB | ~6GB |
| `medium` | ~5GB | ~10GB | ~15GB |
| `large-v3-turbo` | ~6GB | ~10GB | ~15GB |
| `large-v3` | ~10GB | ~20GB | ~30GB |
| `large-v2` | ~10GB | ~20GB | ~30GB |

## PyAnnote v4 Configuration

Configure speaker diarization and voice fingerprinting for speaker identification and tracking.

```bash
# Speaker Diarization Version
PYANNOTE_VERSION=auto  # or: v3, v4 (auto-detect installed version)

# Speaker Detection Ranges
MIN_SPEAKERS=1         # Minimum speakers to detect
MAX_SPEAKERS=20        # Maximum speakers to detect (no hard limit, can increase for large events)

# Embedding & Fingerprinting
EMBEDDING_MODE=auto    # or: v3, v4 (which embedding model to use)

# Model Caching & Warmup
WARM_CACHE_ENABLED=false  # Pre-load speaker models on startup for faster first transcription
MODEL_CACHE_DIR=./models
```

### Speaker Detection Use Cases

| Event Type | Speakers | Recommended MAX_SPEAKERS | Notes |
|-----------|----------|-------------------------|-------|
| Small meetings | 2-5 | 20 (default) | Works well with default |
| Medium meetings | 5-15 | 20 (default) | Works well with default |
| Large conferences | 15-30 | 30-40 | Increase MAX_SPEAKERS |
| Very large events | 30-50+ | 50-100 | No hard limit |

### Warm Cache Benefits

Enabling `WARM_CACHE_ENABLED=true` pre-loads PyAnnote models on startup:
- **First transcription**: 15-20 seconds faster (models already loaded)
- **Subsequent transcriptions**: No performance change
- **Trade-off**: ~500MB additional memory usage at startup
- **Recommended for**: High-throughput systems with continuous transcription

## OpenSearch Neural Search

Configure neural search capabilities for semantic search across transcriptions.

```bash
# Enable/Disable Neural Search
NEURAL_SEARCH_ENABLED=true

# ML Commons Plugin (Powers Neural Search)
OPENSEARCH_ML_COMMONS_ENABLED=true

# OpenSearch Connection
OPENSEARCH_URL=http://opensearch:9200
OPENSEARCH_USERNAME=admin
OPENSEARCH_PASSWORD=your_secure_password

# Vector Search Configuration
NEURAL_SEARCH_MODEL_ID=your_model_id  # ML Commons model ID for embeddings
NEURAL_SEARCH_BATCH_SIZE=32
```

### Neural Search VRAM Requirements

| Embedding Model | VRAM (GPU) | VRAM (CPU) | Batch Size |
|-----------------|-----------|-----------|-----------|
| sentence-transformers/all-MiniLM-L6-v2 | ~200MB | ~500MB | 32-64 |
| sentence-transformers/all-mpnet-base-v2 | ~400MB | ~1GB | 16-32 |
| bge-base-en-v1.5 | ~400MB | ~1GB | 16-32 |
| bge-large-en-v1.5 | ~1GB | ~2GB | 8-16 |
| BAAI/bge-large-zh-v1.5 | ~1GB | ~2GB | 8-16 |

### Search Performance Tuning

```bash
# Collapse optimization: max concurrent group searches (default: 20, 0 = sequential)
SEARCH_COLLAPSE_MAX_CONCURRENT=20

# Bulk batch size: chunks per OpenSearch bulk request (default: 100)
SEARCH_BULK_BATCH_SIZE=100

# Neural ingest batch size: documents per embedding call (default: 5)
SEARCH_NEURAL_BATCH_SIZE=5

# Reindex refresh interval: flush Lucene segments every N files (default: 100)
SEARCH_REINDEX_REFRESH_INTERVAL=100

# Hybrid search over-fetch cap: max candidates per sub-query before RRF merge (default: 200)
# Increase for large indexes where top-200 misses relevant results
SEARCH_MAX_OVERFETCH=200

# RRF rank constant: lower = more aggressive top-result boosting (default: 30)
SEARCH_RRF_RANK_CONSTANT=30
```

### ML Commons Plugin

The OpenSearch ML Commons plugin enables vector embeddings and semantic search:
- **Status**: Automatically detected on OpenSearch startup
- **Configuration**: Database-driven via Admin UI
- **Fallback**: Full-text search if neural search disabled

## Cloud ASR Providers

Configure cloud-based speech recognition as an alternative to local GPU processing.

```bash
# ASR Provider Selection
ASR_PROVIDER=local  # local, deepgram, assemblyai, openai, google, azure, aws, speechmatics, gladia

# Deepgram
DEEPGRAM_API_KEY=
DEEPGRAM_MODEL=nova-3

# AssemblyAI
ASSEMBLYAI_API_KEY=
ASSEMBLYAI_MODEL=universal

# OpenAI Whisper / GPT-4o Transcribe (uses OPENAI_API_KEY)
OPENAI_ASR_MODEL=gpt-4o-transcribe

# Google Cloud Speech
GOOGLE_CLOUD_CREDENTIALS=  # Path to service account JSON
GOOGLE_ASR_MODEL=chirp-3

# Azure Speech
AZURE_SPEECH_KEY=
AZURE_SPEECH_REGION=eastus
AZURE_ASR_MODEL=whisper

# Amazon Transcribe
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=us-east-1
AWS_ASR_MODEL=standard
AWS_TRANSCRIBE_BUCKET=  # S3 bucket for intermediate output

# Speechmatics
SPEECHMATICS_API_KEY=
SPEECHMATICS_MODEL=standard

# Gladia
GLADIA_API_KEY=
GLADIA_MODEL=standard

# Cloud ASR Options
CLOUD_ASR_EXTRACT_EMBEDDINGS=true  # Extract speaker embeddings locally for cross-file matching
CLOUD_ASR_WORKER_CONCURRENCY=4     # Concurrency for cloud-asr worker
```

### Deployment Mode

```bash
DEPLOYMENT_MODE=full  # full (local GPU + optional cloud) or lite (cloud-only, no GPU, ~2GB image)
BACKEND_LITE_IMAGE=davidamacey/opentranscribe-backend-lite:latest
```

## LLM Integration

```bash
LLM_PROVIDER=  # vllm, openai, anthropic, ollama, openrouter
VLLM_BASE_URL=http://localhost:8012/v1
VLLM_MODEL_NAME=mistralai/Mistral-7B-Instruct-v0.2
VLLM_API_KEY=
OPENAI_API_KEY=
OPENAI_MODEL_NAME=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL_NAME=claude-3-haiku-20240307
ANTHROPIC_BASE_URL=https://api.anthropic.com
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL_NAME=llama2:7b-chat
OPENROUTER_API_KEY=
OPENROUTER_MODEL_NAME=anthropic/claude-3-haiku
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

## GPU Concurrent Processing

```bash
# GPU Concurrent Model Sharing (multiple Celery threads share one model copy)
GPU_CONCURRENT_REQUESTS=1  # auto calculates from VRAM: (total - 9GB) / 2GB, max 4
GPU_WORKER_POOL=threads    # Default: threads. Use "prefork" only for legacy single-threaded setups

# Model preloading gate: set to true only on GPU workers to prevent CPU workers
# from initializing CUDA contexts and causing memory leaks (default: false)
PRELOAD_GPU_MODELS=false   # Set true in GPU worker compose service only

# GPU Worker Max Tasks (restart after N tasks for memory safety)
GPU_MAX_TASKS=100000       # Default: effectively never restart
GPU_DEFAULT_BATCH_SIZE=12  # Batch size for default GPU worker (auto-detected if unset)

# VRAM Profiling (temporary diagnostic tool)
ENABLE_VRAM_PROFILING=false  # Captures per-step GPU memory usage and timing data
```

## Multi-GPU Scaling

```bash
GPU_SCALE_ENABLED=false
GPU_SCALE_DEVICE_ID=2
GPU_SCALE_WORKERS=4
GPU_SCALE_DEFAULT_WORKER=1   # Scale default worker (0 to disable)
GPU_SCALE_MAX_TASKS=500       # Restart scaled worker after N tasks (memory safety)
```

## Worker Concurrency Tuning

```bash
# Download worker: parallel video/URL downloads
DOWNLOAD_CONCURRENCY=3   # Default: 3
DOWNLOAD_MAX_TASKS=10     # Restart after N tasks

# NLP worker: LLM summarization, speaker ID
NLP_CONCURRENCY=4         # Default: 4
NLP_MAX_TASKS=50           # Restart after N tasks

# Cloud ASR worker
CLOUD_ASR_WORKER_CONCURRENCY=4
```

## Flower Monitoring Dashboard

```bash
FLOWER_USER=admin
FLOWER_PASSWORD=auto_generated_on_install
FLOWER_URL_PREFIX=flower  # URL prefix (must match nginx proxy_pass path)
```

Flower provides industry-standard Celery task monitoring with persistent task history, queue visibility, and worker status. Access at `http://localhost:5175/flower` (or via NGINX at `/flower/`).

## Storage Encryption

```bash
# MinIO Server-Side Encryption at Rest (AES-256-GCM)
# Generate with: echo "opentranscribe-key:$(openssl rand -base64 32)"
MINIO_KMS_SECRET_KEY=auto_generated_on_install
MINIO_KMS_AUTO_ENCRYPTION=on  # Set to 'on' to enable

# API Key Encryption (for LLM keys stored in database)
ENCRYPTION_KEY=auto_generated_on_install  # NEVER change after first use
```

## Database

```bash
POSTGRES_HOST=postgres
POSTGRES_PORT=5176
POSTGRES_USER=postgres
POSTGRES_PASSWORD=auto_generated_on_install
POSTGRES_DB=opentranscribe
POSTGRES_SSLMODE=prefer  # disable/allow/prefer/require/verify-ca/verify-full
```

Database initialization is handled entirely by Alembic migrations on backend startup. No external SQL init file is needed.

## Ports

```bash
FRONTEND_PORT=5173
BACKEND_PORT=5174
FLOWER_PORT=5175
POSTGRES_PORT=5176
REDIS_PORT=5177
MINIO_PORT=5178
MINIO_CONSOLE_PORT=5179
OPENSEARCH_PORT=5180
OPENSEARCH_ADMIN_PORT=5181
DOCS_PORT=5183
```

## HTTPS/SSL Configuration

Enable HTTPS with NGINX reverse proxy for secure access and browser microphone recording from network devices.

```bash
# Set hostname to enable HTTPS (triggers NGINX reverse proxy)
NGINX_SERVER_NAME=opentranscribe.local

# Optional: Custom ports (defaults shown)
NGINX_HTTP_PORT=80
NGINX_HTTPS_PORT=443

# Optional: Custom certificate paths (defaults shown)
NGINX_CERT_FILE=./nginx/ssl/server.crt
NGINX_CERT_KEY=./nginx/ssl/server.key
```

**Quick setup:** Run `./opentranscribe.sh setup-ssl` to configure interactively.

See [NGINX Setup Guide](/docs/configuration/nginx-setup) for full documentation.

## Content Security Policy

OpenTranscribe's production NGINX configuration includes a Content Security Policy header to mitigate cross-site scripting (XSS) and other injection attacks ([#124](https://github.com/attevon-llc/OpenTranscribe/issues/124)). The CSP restricts script sources, style sources, connection targets, and frame ancestors. Key directives include:

- `default-src 'self'` -- baseline restriction to same-origin resources
- `script-src 'self' 'unsafe-inline'` -- inline scripts required by Svelte hydration (nonce-based CSP is a planned improvement)
- `connect-src 'self' ws: wss:` -- allows WebSocket connections for real-time updates
- `frame-ancestors 'self'` -- prevents clickjacking
- `object-src 'none'`, `base-uri 'self'`, `form-action 'self'` -- defense-in-depth directives

CSP is enforced in production via `frontend/nginx.conf`. Development mode (Vite dev server) does not apply CSP headers.

## File Retention

OpenTranscribe supports admin-configurable automatic file retention ([#134](https://github.com/attevon-llc/OpenTranscribe/issues/134)). Admins can set a retention period (delete files older than N days) to support GDPR compliance and storage management. File deletion is audit-logged and controlled exclusively by super admins via Settings → Admin → File Retention.

## URL Download Quality Settings

URL downloads (YouTube, TikTok, and 1800+ platforms via yt-dlp) support configurable quality settings ([#122](https://github.com/attevon-llc/OpenTranscribe/issues/122)):

```bash
# These are user-level settings stored in the database, configurable via Settings UI.
# Per-download overrides are also available in the URL upload tab.
# Default: "best" for both video and audio (current behavior).
```

Quality options include video resolution selection (best, 4K, 1440p, 1080p, 720p, 480p, 360p), audio-only mode for podcasts, and audio bitrate selection. The yt-dlp format string builder uses a fallback chain: if the requested quality is unavailable, it automatically downloads the next best option. This is designed for bandwidth-conscious users and storage optimization.

## Authentication Configuration

OpenTranscribe uses a **database-driven authentication system** with support for multiple simultaneous auth methods (hybrid authentication). See [Authentication Overview](../authentication/overview.md) for detailed configuration.

### Configuration Sources

Authentication is configured via **Super Admin UI** (Settings → Authentication) and stored in the database with **AES-256-GCM encryption**:

| Priority | Source | Notes |
|---|---|---|
| 1 (wins) | Database (`auth_config` table) | Set in Settings → Authentication; secrets AES-256-GCM encrypted; no restart needed |
| 2 | Environment variable | Bootstrap seed and fallback — **not** an override |
| 3 | Coded default | `backend/app/schemas/auth_config.py` |

### Multi-Method Authentication

Multiple authentication methods can be enabled simultaneously; each account records which one
owns it in `user.auth_type`. Which methods are *available* is decided per method by
`local_enabled`, `ldap_enabled`, `oidc_enabled` and `pki_enabled` — see
[the identity-source model](../authentication/overview.md#the-identity-source-model).

:::warning There is no `AUTH_TYPE` setting
Earlier versions of this page documented `AUTH_TYPE=local,ldap,keycloak` as an informational
indicator. No such setting exists and nothing ever read it. Remove it from your `.env`; it does
nothing.
:::

### LDAP/Active Directory Configuration

```bash
# LDAP/Active Directory (configured via Super Admin UI)
# These ENV variables are for legacy/development use only
LDAP_SERVER=ldaps://your-ad-server.domain.com
LDAP_PORT=636
LDAP_USE_SSL=true
LDAP_BIND_DN=CN=service-account,CN=Users,DC=domain,DC=com
LDAP_BIND_PASSWORD=your-service-account-password
LDAP_SEARCH_BASE=DC=domain,DC=com
LDAP_USERNAME_ATTR=sAMAccountName
```

### OpenID Connect Configuration

Works with any conforming provider. Set `OIDC_DISCOVERY_URL` for anything other than Keycloak;
it makes `OIDC_REALM` irrelevant. Full reference: [OIDC setup](../authentication/oidc.md).

```bash
# OpenID Connect (normally configured in Settings → Authentication → OIDC)
# These ENV variables are a bootstrap seed / fallback
OIDC_ENABLED=true
OIDC_SERVER_URL=https://idp.yourdomain.com
OIDC_DISCOVERY_URL=https://idp.yourdomain.com/.well-known/openid-configuration
OIDC_REALM=opentranscribe          # ignored when OIDC_DISCOVERY_URL is set
OIDC_CLIENT_ID=opentranscribe-app
OIDC_CLIENT_SECRET=your-client-secret
OIDC_CALLBACK_URL=https://yourdomain.com/login   # the FRONTEND login page
OIDC_ROLES_CLAIM=groups            # realm_access.roles | groups | roles
OIDC_ADMIN_ROLE=admin
```

:::note `KEYCLOAK_*` still works
Every one of these variables was previously named `KEYCLOAK_*`, and those names keep working
permanently — the legacy spelling even wins when both are set. The canonical spelling is
`OIDC_*`; the backend logs one deprecation line at startup naming any legacy variables it found.
:::

### PKI/X.509 Certificate Configuration

```bash
# PKI/X.509 Certificates (configured via Super Admin UI)
# These ENV variables are for legacy/development use only
PKI_CA_CERT_PATH=/path/to/ca.crt
PKI_ADMIN_DNS=CN=Admin User,O=Company,C=US
```

### Security Features

```bash
# Password Policy (FedRAMP IA-5)
PASSWORD_POLICY_ENABLED=true
PASSWORD_MIN_LENGTH=12
PASSWORD_REQUIRE_UPPERCASE=true
PASSWORD_REQUIRE_LOWERCASE=true
PASSWORD_REQUIRE_DIGIT=true
PASSWORD_REQUIRE_SPECIAL=true
PASSWORD_HISTORY_COUNT=24
PASSWORD_MAX_AGE_DAYS=60

# Account Lockout (NIST AC-7)
ACCOUNT_LOCKOUT_ENABLED=true
ACCOUNT_LOCKOUT_THRESHOLD=5
ACCOUNT_LOCKOUT_DURATION_MINUTES=15
ACCOUNT_LOCKOUT_PROGRESSIVE=true
ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES=1440

# Multi-Factor Authentication
MFA_ENABLED=true
MFA_ISSUER_NAME=OpenTranscribe
MFA_BACKUP_CODE_COUNT=10

# Rate Limiting
RATE_LIMIT_ENABLED=true
RATE_LIMIT_AUTH_PER_MINUTE=10
RATE_LIMIT_API_PER_MINUTE=100

# Audit Logging (FedRAMP AU-2/AU-3)
AUDIT_LOG_ENABLED=true
AUDIT_LOG_FORMAT=json  # or: cef
AUDIT_LOG_TO_OPENSEARCH=false

# Login Banner
LOGIN_BANNER_ENABLED=false
LOGIN_BANNER_TITLE=Security Notice
LOGIN_BANNER_TEXT=This is a restricted system...
```

## Next Steps

- [Authentication Overview](../authentication/overview.md)
- [GPU Setup](../installation/gpu-setup.md)
- [Multi-GPU Scaling](./multi-gpu-scaling.md)
- [LLM Integration](../features/llm-integration.md)
