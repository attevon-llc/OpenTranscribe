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

# Where v4 (256-dim) voiceprints are computed. Default true: they come from the
# diarizer's own centroids, or from the diar-native sidecar when a separate
# extraction is needed — both run the same WeSpeaker ResNet34-LM weights the
# in-process model does, so this is a deployment choice, not an accuracy one.
# Set false to force the in-process PyAnnote model (the escape hatch; costs a
# 40-60s model load and ~500MB VRAM per worker). v3 (512-dim) installs always use
# the in-process model — `pyannote/embedding` is a different network that the
# sidecar does not serve.
USE_NATIVE_SPEAKER_EMBEDDINGS=true

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
# Enable/Disable Neural Search (falls back to keyword-only when false)
OPENSEARCH_NEURAL_SEARCH_ENABLED=true

# OpenSearch Connection
OPENSEARCH_HOST=opensearch
OPENSEARCH_PORT=5180          # host-published port; containers talk to opensearch:9200
OPENSEARCH_USER=admin
OPENSEARCH_PASSWORD=your_secure_password

# Embedding model — must be one of the verified models the admin UI offers
OPENSEARCH_NEURAL_MODEL=huggingface/sentence-transformers/all-MiniLM-L6-v2

# JVM heap. Xms must equal Xmx; bootstrap.memory_lock pins it in RAM at startup.
OPENSEARCH_JAVA_OPTS=-Xms4g -Xmx4g
```

### Neural Search Memory Requirements

The embedding model is loaded **by OpenSearch itself and runs on CPU inside the JVM** — it
never touches the GPU, so what it costs is heap, not VRAM.

| Heap | What it runs |
|---|---|
| 1 GB | the default `all-MiniLM-L6-v2` (384-dim) **and** `paraphrase-multilingual-MiniLM-L12-v2` (measured: real cross-lingual inference, despite being 5× the default's size — size does not predict the floor) |
| 2 GB | every English model, including the 768-dim ones |
| 4 GB *(default)* | headroom for indexing bursts and the larger multilingual models |

Enabling multilingual search therefore needs **no heap change**: Settings → Search →
pick the multilingual model → **Download & deploy** → Apply (re-embeds every
transcript; measured ~3.2 documents/sec on the OpenSearch CPU node).

Measured floors and the "deployed but not working" failure mode:
[Performance Tuning](../operations/performance-tuning.md#opensearch-heap-what-it-is-actually-for).
The full list of selectable models is in the
[Admin Panel guide](../user-guide/admin-panel.md#embedding-model-selection).

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

### Index Topology

Shard and replica counts for the `transcript_chunks` index, applied **only when the index is
created** — OpenSearch cannot change a live index's shard count in place, and nothing in this
app deletes and recreates the index just to pick up a new value (that is the destructive
`recreate_index_for_dimension` path, reserved for an embedding-dimension change).

```bash
OPENSEARCH_CHUNKS_INDEX_SHARDS=1     # default -- correct for laptop/home-server (single node)
OPENSEARCH_CHUNKS_INDEX_REPLICAS=0   # default -- correct for laptop/home-server (single node)
```

:::warning A replica needs a second node to mean anything
`number_of_replicas` is a *copy count per shard*. On a single-node deployment (laptop, home
server, the bundled `opensearch` container) there is nowhere to place a replica shard, so
setting `OPENSEARCH_CHUNKS_INDEX_REPLICAS` above `0` leaves every replica **UNASSIGNED** and the
index health **yellow** forever -- it is not a safety margin on one node, only cost. Raise it
only on a multi-node domain (see the AWS profile below), where OpenSearch actually has a second
node to place the copy on.
:::

To change topology on an **existing** deployment: set the variable, then create a fresh index at
the new topology (a `--fresh` deployment, or a deliberate reindex-from-scratch) rather than
expecting the running index to pick it up.

### ML Commons Plugin

The OpenSearch ML Commons plugin enables vector embeddings and semantic search:
- **Status**: Automatically detected on OpenSearch startup
- **Configuration**: Database-driven via Admin UI
- **Fallback**: Full-text search if neural search disabled

### AWS OpenSearch Service (SigV4 auth + managed embeddings)

By default OpenSearch is reached with basic auth (username/password), which is what the
bundled OpenSearch container and most self-hosted clusters expect. A managed **Amazon
OpenSearch Service** domain with an IAM access policy instead requires SigV4-signed requests:

```bash
# Authentication mode
OPENSEARCH_AUTH=basic  # basic (default, unchanged) or sigv4

# SigV4 signing -- used only when OPENSEARCH_AUTH=sigv4
OPENSEARCH_AWS_REGION=     # empty falls back to AWS_REGION
OPENSEARCH_AWS_SERVICE=es  # es (managed domain, default) or aoss (OpenSearch Serverless)

# Embedding mode
OPENSEARCH_EMBEDDING_MODE=local  # local (default, unchanged) or managed
OPENSEARCH_NEURAL_MODEL_ID=      # pre-registered ML Commons model id -- used when OPENSEARCH_EMBEDDING_MODE=managed
```

`OPENSEARCH_AUTH=sigv4` signs every OpenSearch client with the AWS credential chain and forces
TLS. `OPENSEARCH_EMBEDDING_MODE=managed` adopts a model the domain already hosts
(`OPENSEARCH_NEURAL_MODEL_ID`) instead of mutating ML Commons cluster settings and registering a
model by URL -- operations a managed AWS domain does not permit and which otherwise make neural
search fail to initialize there.

### The AWS profile

The three seams above compose into one deployment profile: OpenSearch auth, where embeddings
come from, and where objects live. None of them require code changes -- each is an existing env
var -- but they are only tested and supported **together**, not as a pick-and-mix:

```bash
# OpenSearch: a managed Amazon OpenSearch Service domain
OPENSEARCH_HOST=<your-domain>.<region>.es.amazonaws.com
OPENSEARCH_PORT=443
OPENSEARCH_AUTH=sigv4
OPENSEARCH_AWS_REGION=            # empty falls back to AWS_REGION
OPENSEARCH_AWS_SERVICE=es         # aoss for OpenSearch Serverless

# Embeddings: adopt a model the domain already hosts (managed connector), never register one
OPENSEARCH_EMBEDDING_MODE=managed
OPENSEARCH_NEURAL_MODEL_ID=<pre-registered ML Commons model id>

# Object storage: native S3 instead of the bundled MinIO container
STORAGE_BACKEND=s3
S3_REGION=<same region as the domain, to avoid cross-region egress>
S3_USE_IAM_ROLE=true              # IRSA/ECS-task/instance-profile credentials, no static keys

# Index topology: worth a replica once there is a second node to place it on
OPENSEARCH_CHUNKS_INDEX_SHARDS=1
OPENSEARCH_CHUNKS_INDEX_REPLICAS=1
```

What each line implies:

- **`OPENSEARCH_AUTH=sigv4` + `OPENSEARCH_EMBEDDING_MODE=managed` go together.** A managed domain's
  IAM access policy accepts SigV4-signed requests only, and separately does not expose the
  cluster settings the `local` embedding path needs to register a model by `file://` or arbitrary
  URL -- so a managed domain that is reached with `sigv4` but left on `OPENSEARCH_EMBEDDING_MODE=local`
  fails to initialize neural search, not merely runs it inefficiently.
- **`STORAGE_BACKEND=s3` is independent of the OpenSearch two**, but the AWS profile sets all
  three together because a managed OpenSearch domain and a self-hosted MinIO container in the
  same deployment is an unusual, unmeasured combination -- nothing forbids it, nothing has
  exercised it.
- **Replica guidance.** `number_of_replicas` is a per-shard copy count and needs a second data
  node to place the copy on. A managed multi-node AWS domain (the normal shape once you are
  paying for SigV4 auth and a managed embedding connector) is exactly that: set
  `OPENSEARCH_CHUNKS_INDEX_REPLICAS=1` (or higher, per your domain's node count and the
  redundancy you want) for read availability across nodes and resilience to losing one. Do not
  set it above `0` on the single-node laptop/home-server profile -- see the topology warning
  above. Shards stay at the shipped default of `1` unless a corpus is large enough to need
  horizontal partitioning, which is a capacity decision for the operator's own index, not
  something this profile changes for you.
- This is a profile you assemble, not a flag `./opentr.sh` recognizes -- there is no
  `--aws` overlay. Set the variables in `.env` and start normally
  (`./opentr.sh start prod --build`); the seams themselves branch on the values above, not on a
  deployment-type flag.

## Cloud ASR Providers

Configure cloud-based speech recognition as an alternative to local GPU processing.

```bash
# ASR Provider Selection
ASR_PROVIDER=local  # local, deepgram, assemblyai, openai, google, azure, aws, speechmatics, gladia, pyannote

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

# pyannote.ai (STT orchestration — transcription + premium diarization in one API call)
PYANNOTE_API_KEY=
PYANNOTE_MODEL=parakeet  # or: whisper-large-v3-turbo

# Cloud ASR Options
CLOUD_ASR_CONCURRENCY=4            # Concurrency for cloud-asr worker
```

### Deployment Mode

```bash
DEPLOYMENT_MODE=full  # full (local GPU + optional cloud) or lite (cloud-only, no GPU, ~2GB image)
BACKEND_LITE_IMAGE=davidamacey/opentranscribe-backend-lite:latest
```

## LLM Integration

```bash
LLM_PROVIDER=  # vllm, openai, anthropic, ollama, openrouter, bedrock
VLLM_BASE_URL=http://localhost:8012/v1
VLLM_MODEL_NAME=mistralai/Mistral-7B-Instruct-v0.2
VLLM_API_KEY=
OPENAI_API_KEY=
OPENAI_MODEL_NAME=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL_NAME=claude-haiku-4-5
ANTHROPIC_BASE_URL=https://api.anthropic.com
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL_NAME=llama2:7b-chat
OPENROUTER_API_KEY=
OPENROUTER_MODEL_NAME=anthropic/claude-haiku-4.5
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1

# Amazon Bedrock — no API key: boto3 uses the standard AWS credential chain
BEDROCK_REGION=            # falls back to AWS_REGION / AWS_DEFAULT_REGION
BEDROCK_MODEL_NAME=anthropic.claude-haiku-4-5-20251001-v1:0
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
CLOUD_ASR_CONCURRENCY=4
```

## Flower Monitoring Dashboard

```bash
FLOWER_USER=admin
FLOWER_PASSWORD=auto_generated_on_install
FLOWER_URL_PREFIX=flower  # URL prefix (must match nginx proxy_pass path)
```

Flower provides industry-standard Celery task monitoring with persistent task history, queue visibility, and worker status. Access at `http://localhost:5175/flower` (or via NGINX at `/flower/`).

## Object Storage

OpenTranscribe stores uploaded media in an S3-compatible bucket. `STORAGE_BACKEND=minio` (the
bundled, self-hosted MinIO container) is the default and remains fully backward compatible; a
native AWS S3 backend is also available for cloud deployments.

```bash
# Storage Backend Selection
STORAGE_BACKEND=minio  # minio (default, self-hosted) or s3 (native AWS S3 / S3-compatible)
```

### MinIO (default)

```bash
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin
MINIO_HOST=localhost
MINIO_PORT=9000
MINIO_SECURE=false
MINIO_PUBLIC_URL=  # browser-facing origin for presigned URLs; empty keeps the default /s3 proxy path
```

### Native AWS S3 (`STORAGE_BACKEND=s3`)

```bash
STORAGE_BACKEND=s3
S3_REGION=us-east-1               # falls back to AWS_REGION
S3_ENDPOINT_URL=                  # set for an S3-compatible provider other than AWS; empty resolves to s3.<region>.amazonaws.com
S3_USE_IAM_ROLE=true              # default: AWS credential chain (env / EKS-IRSA web identity / ECS task role / EC2 instance metadata)
AWS_ACCESS_KEY_ID=                # only used when S3_USE_IAM_ROLE=false
AWS_SECRET_ACCESS_KEY=            # only used when S3_USE_IAM_ROLE=false
S3_CONFIGURE_BUCKET_CORS=false    # opt-in: apply a browser-upload CORS policy (boto3; minio-py has no CORS API)
```

`S3_USE_IAM_ROLE=true` (the default) needs no static keys -- credentials come from the standard
AWS provider chain with automatic rotation. Set `S3_USE_IAM_ROLE=false` to sign with static
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` instead. The same object storage client (`minio.Minio`)
drives both backends; switching `STORAGE_BACKEND` changes endpoint/credential/addressing
construction, not the call sites.

### Presigned URLs and large uploads (both backends)

```bash
STORAGE_PUBLIC_URL=               # backend-agnostic alias for MINIO_PUBLIC_URL; empty keeps the /s3 proxy path on MinIO and leaves native S3 URLs untouched
PRESIGNED_URL_MAX_SECONDS=21600   # 6h default -- a presigned URL cannot outlive the credentials that signed it (IAM-role STS sessions expire well inside 24h)
MULTIPART_THRESHOLD_MB=512        # objects at/above this size use browser-side multipart upload
```

:::note S3 vs MinIO single-PUT ceiling
MinIO accepts a single-PUT object up to 5 TiB. AWS S3 rejects a single PUT above 5 GiB
(`EntityTooLarge`), so on `STORAGE_BACKEND=s3` an upload above that size always goes through the
multipart path regardless of `MULTIPART_THRESHOLD_MB`.
:::

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

## Cloud ASR Providers

:::tip Configure these in the UI
Each user sets their own ASR provider and API key in **Settings → Transcription**,
stored encrypted in the database. The variables below are only the
**deployment-wide fallback** for users who have set nothing, and for a zero-touch
provisioned install. They were removed from `.env.example` for that reason.
:::

`ASR_PROVIDER` selects the default engine: `local` (the bundled WhisperX, needs a
GPU) or one of the cloud providers below.

| Provider | `ASR_PROVIDER` | Variables |
|---|---|---|
| Deepgram | `deepgram` | `DEEPGRAM_API_KEY`, `DEEPGRAM_MODEL` (default `nova-3`) |
| AssemblyAI | `assemblyai` | `ASSEMBLYAI_API_KEY`, `ASSEMBLYAI_MODEL` (`universal`) |
| OpenAI | `openai` | reuses `OPENAI_API_KEY`; `OPENAI_ASR_MODEL` (`gpt-4o-transcribe`) |
| Google Cloud Speech | `google` | `GOOGLE_CLOUD_CREDENTIALS` (path to the service-account JSON), `GOOGLE_ASR_MODEL` (`chirp-3`) |
| Azure Speech | `azure` | `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` (`eastus`), `AZURE_ASR_MODEL` (`whisper`) |
| Amazon Transcribe | `aws` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `AWS_ASR_MODEL`, `AWS_TRANSCRIBE_BUCKET` |
| Speechmatics | `speechmatics` | `SPEECHMATICS_API_KEY`, `SPEECHMATICS_MODEL` |
| Gladia | `gladia` | `GLADIA_API_KEY`, `GLADIA_MODEL` |

:::warning AWS variables are shared
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_REGION` are **not
ASR-specific**. The native S3 storage backend uses them when
`S3_USE_IAM_ROLE=false`, and `BEDROCK_REGION` falls back to `AWS_REGION`.
Changing them affects storage and Bedrock too. They remain in `.env.example` for
that reason.
:::

Amazon Transcribe additionally needs `AWS_TRANSCRIBE_BUCKET` to already exist —
Transcribe writes intermediate output there, and the bucket must be in
`AWS_REGION`.

Worker concurrency for cloud providers is `CLOUD_ASR_CONCURRENCY` (compose
default **16**), not a per-provider setting.

## LLM Providers

:::tip Configure these in the UI
Each user configures their own LLM provider, model and API key in
**Settings → LLM Provider**, encrypted at rest. `LLMService` resolves per-user
settings first and only falls back to the variables below when a user has none —
which is also the path background tasks take. Leave `LLM_PROVIDER` empty for
transcription-only mode with no AI features at all.
:::

| Provider | `LLM_PROVIDER` | Variables |
|---|---|---|
| vLLM (self-hosted) | `vllm` | `VLLM_BASE_URL`, `VLLM_MODEL_NAME`, `VLLM_API_KEY` |
| Ollama (self-hosted) | `ollama` | `OLLAMA_BASE_URL`, `OLLAMA_MODEL_NAME` |
| OpenAI | `openai` | `OPENAI_API_KEY`, `OPENAI_MODEL_NAME`, `OPENAI_BASE_URL` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL_NAME`, `ANTHROPIC_BASE_URL` |
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL_NAME`, `OPENROUTER_BASE_URL` |
| Amazon Bedrock | `bedrock` | `BEDROCK_REGION` only — no API key |
| Custom (OpenAI-compatible) | `custom` | user-config only; never resolved from env |

### Self-hosted models need the SSRF guard opened

`LLM_ALLOW_PRIVATE_ENDPOINTS` defaults to **`false`**, which makes the backend
refuse to call private, loopback, link-local or cloud-metadata addresses. That is
correct for a cloud deployment and **blocks a local vLLM or Ollama entirely** —
the symptom is an opaque `Health check blocked … Private IP address`.

```bash
LLM_ALLOW_PRIVATE_ENDPOINTS=true   # required for local vLLM / Ollama
```

:::danger Keep it false on multi-tenant deployments
With it on, any user can point a "test connection" at internal services or cloud
instance metadata. Only enable it where you control who can register.
:::

### Bedrock uses the AWS credential chain

There is deliberately no Bedrock API key. boto3 resolves credentials from the
standard chain (instance role, task role, shared profile, environment), so a
deployment on EC2/ECS/EKS provisions no secret at all. Required IAM actions:
`bedrock:InvokeModelWithResponseStream` (chat) and `bedrock:InvokeModel`
(summaries). `BEDROCK_REGION` falls back to `AWS_REGION` / `AWS_DEFAULT_REGION`.

### Context window

`max_tokens` is a **UI setting**, not an environment variable
(**Settings → LLM Provider → Max Tokens**). It still defaults to **8192**, but a
**Discover context window** probe (beside Test Connection) now measures the
model's real maximum instead of making you trust that default: for **vLLM** it
reads `max_model_len` off `GET /v1/models`, for **Ollama** it reads the model's
`context_length` off `POST /api/show`. Both are metadata-only calls — no
generation, no user content — and run only when you click the button, never on
a schedule. Every other provider (Anthropic, OpenRouter, Bedrock, `custom`)
reports as unsupported and your configured value stands unchanged. The probe
never guesses upward — a stale or wrong measurement fails closed to "unknown"
rather than raising your configured limit for you — so `max_tokens` still needs
to be raised by hand to match what the probe reports; leaving it at 8192 still
truncates long transcripts, the probe just makes that visible instead of
silent.

## Worker Concurrency and PostgreSQL Tuning

Advanced knobs for bulk-processing workloads. All are **optional** — the compose
defaults suit a 4–8 GB server with SSD storage, so a normal deployment sets none
of them. They were removed from `.env.example` to keep it to what an install
actually needs.

### Celery worker concurrency

| Variable | Default | Worker |
|---|---|---|
| `DOWNLOAD_CONCURRENCY` | 5 | parallel video/URL downloads — raise for bulk imports |
| `DOWNLOAD_MAX_TASKS` | 10 | restart the download worker after N tasks |
| `CPU_WORKER_CONCURRENCY` | 8 | preprocessing, postprocessing, waveforms |
| `CLOUD_ASR_CONCURRENCY` | **16** | concurrent cloud-provider transcriptions |
| `REDACTION_MAX_TASKS` | **200** | restart the redaction worker after N tasks |
| `REDACTION_WORKER_POOL` | `threads` | Celery pool for the redaction worker |
| `NLP_CONCURRENCY` | 4 | summarization, speaker ID, topic extraction |
| `NLP_MAX_TASKS` | 50 | restart the NLP worker after N tasks |
| `WORKER_DB_POOL_SIZE` | 2 | worker SQLAlchemy pool (workers fork their own engines) |
| `WORKER_DB_MAX_OVERFLOW` | 3 | worker pool overflow |

GPU worker settings are documented separately under **GPU Configuration** above —
note in particular that `GPU_MAX_TASKS` is **ignored** on the default `threads`
pool, because `--max-tasks-per-child` is a prefork-only feature.

### PostgreSQL

These override the values compose passes to the Postgres container.

| Variable | Default | Guidance |
|---|---|---|
| `PG_SHARED_BUFFERS` | `256MB` | ~25% of available RAM |
| `PG_EFFECTIVE_CACHE_SIZE` | `1GB` | ~75% of RAM, as an OS-cache estimate |
| `PG_WORK_MEM` | `16MB` | per sort/hash operation |
| `PG_MAINTENANCE_WORK_MEM` | `128MB` | `VACUUM`, `CREATE INDEX` |
| `PG_RANDOM_PAGE_COST` | `1.1` | `1.1` for SSD, `4.0` for spinning disk |
| `PG_EFFECTIVE_IO_CONCURRENCY` | `200` | `200` for SSD, `2` for HDD |
| `PG_MAX_CONNECTIONS` | `200` | maximum client connections |

### Auto-constructed values — do not set these

Some variables are **derived** and setting them by hand has no effect or breaks
the deployment:

- `DATABASE_URL` — built by the backend from the individual `POSTGRES_*` settings.
- `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` — built from `REDIS_HOST`,
  `REDIS_PORT` and `REDIS_PASSWORD`.
- `POSTGRES_HOST`, `MINIO_HOST`, `REDIS_HOST`, `OPENSEARCH_HOST` inside
  containers — compose hardcodes the service DNS names. The values in `.env` only
  affect host-side tools such as pytest.

## Search and Indexing Tuning

Optional knobs for the OpenSearch transcript index. Defaults are correct for a
laptop or single home server; none of these need setting for a normal install.

| Variable | Default | Effect |
|---|---|---|
| `SEARCH_CHUNK_TARGET_WORDS` | 200 | target words per transcript chunk |
| `SEARCH_CHUNK_OVERLAP_WORDS` | 40 | sliding-window overlap between chunks |
| `SEARCH_BULK_BATCH_SIZE` | 100 | chunks per OpenSearch bulk request |
| `SEARCH_NEURAL_BATCH_SIZE` | 5 | documents per embedding call |
| `SEARCH_REINDEX_REFRESH_INTERVAL` | 100 | flush a Lucene segment every N files |
| `SEARCH_LARGE_TRANSCRIPT_CHUNKS` | — | bulk loads this large disable refresh for the load |
| `REINDEX_PARALLEL_WORKERS` | — | parallel reindex workers |
| `SEARCH_COLLAPSE_MAX_CONCURRENT` | 20 | concurrent inner_hits searches; 0 = sequential |
| `SEARCH_MAX_OVERFETCH` | — | over-fetch ceiling before collapse |
| `SEARCH_HYBRID_MIN_SCORE` | — | minimum hybrid score to return a hit |
| `SEARCH_SEMANTIC_HIGH_CONFIDENCE` | 0.010 | semantic-confidence threshold |
| `SEARCH_SEMANTIC_SUPPRESS_RATIO` | 0.20 | suppression ratio for weak semantic hits |
| `OPENSEARCH_CHUNKS_INDEX_SHARDS` | 1 | applied **only at index creation** |
| `OPENSEARCH_CHUNKS_INDEX_REPLICAS` | 0 | see the warning below |

:::warning Changing chunk size requires a full reindex
Chunk boundaries are baked into the index at write time. Changing
`SEARCH_CHUNK_TARGET_WORDS` or `SEARCH_CHUNK_OVERLAP_WORDS` affects only
newly-indexed content until you reindex everything, which leaves a corpus chunked
two different ways in the meantime.
:::

:::warning Replicas on a single node
`OPENSEARCH_CHUNKS_INDEX_REPLICAS > 0` on a single-node cluster leaves every
replica shard permanently `UNASSIGNED` and the index status yellow — there is no
second node to place them on. Set it `>= 1` only on a multi-node domain.
:::

### Fusion strategy — measurement knobs, deliberately env-only

`SEARCH_FUSION_STRATEGY`, `SEARCH_RRF_RANK_CONSTANT`, `SEARCH_RRF_WINDOW_SIZE`,
`SEARCH_NORMALIZATION_TECHNIQUE`, `SEARCH_COMBINATION_TECHNIQUE` and
`SEARCH_COMBINATION_WEIGHTS` select how keyword and vector results are fused.

These are **not** DB-backed on purpose: they exist to run A/B measurements, and a
per-request argument is the supported way to use them. RRF remains the default
because a ten-arm sweep over 1,651 queries found no arm that won on both corpora.
See `backend/app/services/search/CLAUDE.md` before changing any of them.

### Per-variable reference — cloud ASR

Every variable below is the **deployment-wide fallback**. A user who configures a
provider in **Settings → Transcription** overrides all of it, and their API key is
stored encrypted rather than in a file.

| Variable | Valid values / limits | Default | Description |
|---|---|---|---|
| `ASR_PROVIDER` | `local` \| `deepgram` \| `assemblyai` \| `openai` \| `google` \| `azure` \| `aws` \| `speechmatics` \| `gladia` | `local` | Engine used when a user has chosen nothing. `local` uses the bundled WhisperX and requires a GPU. |
| `DEEPGRAM_API_KEY` | string | *(empty)* | Deepgram credential. Empty disables the provider. |
| `DEEPGRAM_MODEL` | `nova-3`, `nova-2`, `enhanced`, `base` | `nova-3` | Deepgram model id. Older accounts may not have `nova-3`. |
| `ASSEMBLYAI_API_KEY` | string | *(empty)* | AssemblyAI credential. |
| `ASSEMBLYAI_MODEL` | `universal`, `best`, `nano` | `universal` | Model tier. `nano` is cheapest, `best` most accurate. |
| `OPENAI_ASR_MODEL` | `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `whisper-1` | `gpt-4o-transcribe` | OpenAI speech model. Uses `OPENAI_API_KEY` — there is no separate ASR key. |
| `GOOGLE_CLOUD_CREDENTIALS` | absolute path | *(empty)* | **Path to a service-account JSON file**, not a key string. Must be readable inside the container. |
| `GOOGLE_ASR_MODEL` | `chirp-3`, `chirp-2`, `latest_long`, `latest_short` | `chirp-3` | Google Speech model. |
| `AZURE_SPEECH_KEY` | string | *(empty)* | Azure Speech subscription key. |
| `AZURE_SPEECH_REGION` | any Azure region id | `eastus` | **Must match the region the key was issued for**, or every request returns 401. |
| `AZURE_ASR_MODEL` | `whisper`, `conversation` | `whisper` | Azure recognition model. |
| `AWS_ASR_MODEL` | `standard`, `medical` | `standard` | Amazon Transcribe tier. |
| `AWS_TRANSCRIBE_BUCKET` | S3 bucket name | *(empty)* | Bucket Transcribe writes intermediate output to. **Must already exist and be in `AWS_REGION`.** |
| `SPEECHMATICS_API_KEY` | string | *(empty)* | Speechmatics credential. |
| `SPEECHMATICS_MODEL` | `standard`, `enhanced` | `standard` | Operating point. `enhanced` is slower and more accurate. |
| `GLADIA_API_KEY` | string | *(empty)* | Gladia credential. |
| `GLADIA_MODEL` | `standard`, `accurate` | `standard` | Gladia model tier. |

#### Example — Deepgram as the deployment default

```bash
# .env
ASR_PROVIDER=deepgram
DEEPGRAM_API_KEY=your-deepgram-key
DEEPGRAM_MODEL=nova-3
```

#### Example — Amazon Transcribe with an instance role

```bash
# .env — no static keys; the EC2/ECS role supplies credentials
ASR_PROVIDER=aws
AWS_REGION=us-east-1
AWS_TRANSCRIBE_BUCKET=my-transcribe-scratch   # must exist, same region
AWS_ASR_MODEL=standard
```

### Per-variable reference — LLM providers

**Where to set** column legend:

| Marker | Meaning |
|---|---|
| 🖥️ **UI** | Configurable in the admin/user UI. The UI value **wins** — you do not need to set it in `.env` at all. The env var is only a fallback for users who have configured nothing, and for background tasks (which have no user). |
| 📄 **env** | No UI equivalent exists. `.env` is the only way to set it. |

| Variable | Where to set | Valid values / limits | Default | Description |
|---|---|---|---|---|
| `LLM_PROVIDER` | 🖥️ UI | `vllm` \| `openai` \| `ollama` \| `anthropic` \| `bedrock` \| `openrouter` \| `custom` \| *(empty)* | *(empty)* | Fallback provider. **Empty = transcription-only**: no summaries, speaker suggestions or chat. `custom` is user-config only and is never resolved from env. |
| `LLM_ALLOW_PRIVATE_ENDPOINTS` | 📄 env | `true` \| `false` | `false` | SSRF guard. **Must be `true` for a local vLLM/Ollama**, or calls are refused with `Health check blocked … Private IP address`. Keep `false` anywhere untrusted users can register. |
| `VLLM_BASE_URL` | 🖥️ UI | URL ending `/v1` | `http://localhost:8012/v1` | vLLM OpenAI-compatible endpoint. This exact default is treated as *"not configured"*, so an untouched value is ignored rather than dialled. |
| `VLLM_MODEL_NAME` | 🖥️ UI | model name your server reports | *(empty)* | Must match what vLLM serves. `gpt-oss` is treated as a placeholder, not a real model. |
| `VLLM_API_KEY` | 🖥️ UI | string | *(empty)* | Only needed if vLLM was started with `--api-key`. Usually blank locally. |
| `OLLAMA_BASE_URL` | 🖥️ UI | URL | `http://localhost:11434` | ⚠️ Unlike vLLM this has **no** "not configured" sentinel — an untouched default is treated as real and hits the SSRF refusal unless `LLM_ALLOW_PRIVATE_ENDPOINTS=true`. |
| `OLLAMA_MODEL_NAME` | 🖥️ UI | any pulled Ollama tag | `llama2:7b-chat` | ⚠️ The coded default is **stale** (Llama 2, 2023). Use a current tag such as `llama3.1:8b`, and pull it first: `ollama pull llama3.1:8b`. |
| `OPENAI_API_KEY` | 🖥️ UI | `sk-…` | *(empty)* | OpenAI credential, shared with the OpenAI ASR provider. |
| `OPENAI_MODEL_NAME` | 🖥️ UI | any OpenAI chat model | `gpt-4o-mini` | Model used for summaries and speaker suggestions. |
| `OPENAI_BASE_URL` | 🖥️ UI | URL | `https://api.openai.com/v1` | Override for an OpenAI-compatible gateway. |
| `ANTHROPIC_API_KEY` | 🖥️ UI | `sk-ant-…` | *(empty)* | Anthropic credential. |
| `ANTHROPIC_MODEL_NAME` | 🖥️ UI | any Claude model id | `claude-haiku-4-5` | Anthropic model. |
| `ANTHROPIC_BASE_URL` | 🖥️ UI | URL | `https://api.anthropic.com` | Override for a proxy or gateway. |
| `OPENROUTER_API_KEY` | 🖥️ UI | `sk-or-…` | *(empty)* | OpenRouter credential. |
| `OPENROUTER_MODEL_NAME` | 🖥️ UI | `vendor/model` slug | `anthropic/claude-haiku-4.5` | Note the `vendor/model` form — a bare model name will not resolve. |
| `OPENROUTER_BASE_URL` | 🖥️ UI | URL | `https://openrouter.ai/api/v1` | OpenRouter endpoint. |
| `BEDROCK_REGION` | 📄 env | AWS region id | *(empty)* | Falls back to `AWS_REGION` / `AWS_DEFAULT_REGION`. **No API key exists** — boto3 uses the standard credential chain. The Bedrock *model* is chosen per user in the UI only, so there is no env var for it. |
| *max tokens / context window* | 🖥️ **UI only** | 512 – 2,000,000 | 8192 | **There is no env var.** Set it at Settings → LLM Provider → Max Tokens. A **Discover context window** probe (vLLM/Ollama only) can measure the model's real maximum for comparison, but never raises this value for you — leaving it at 8192 still silently truncates long transcripts. |

#### Example — local Ollama on the same host

```bash
# .env
LLM_PROVIDER=ollama
LLM_ALLOW_PRIVATE_ENDPOINTS=true      # REQUIRED, or every call is refused
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL_NAME=llama3.1:8b         # run: ollama pull llama3.1:8b
```

#### Example — cloud provider for a hosted deployment

```bash
# .env
LLM_PROVIDER=anthropic
LLM_ALLOW_PRIVATE_ENDPOINTS=false     # keep the SSRF guard on
ANTHROPIC_API_KEY=sk-ant-your-key
ANTHROPIC_MODEL_NAME=claude-haiku-4-5
```

:::note Setting these is optional
None of the above is required. A deployment with `LLM_PROVIDER` empty and
`ASR_PROVIDER=local` transcribes normally with no cloud account at all — which is
the default self-hosted configuration.
:::
