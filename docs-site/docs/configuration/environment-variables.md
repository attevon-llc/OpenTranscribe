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

# PyAnnote Speaker Diarization — DIARIZATION_MODEL is fixed; there is no runtime version toggle
DIARIZATION_MODEL=pyannote/speaker-diarization-community-1
MIN_SPEAKERS=1
MAX_SPEAKERS=20

# Model Caching & Storage
MODEL_CACHE_DIR=./models
HUGGINGFACE_TOKEN=hf_your_token_here
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

## Speaker Diarization & Voiceprint Embeddings

Configure speaker diarization and voice fingerprinting for speaker identification and tracking.
There is no `PYANNOTE_VERSION` or `EMBEDDING_MODE` variable in the code — the diarization
pipeline is fixed to `DIARIZATION_MODEL=pyannote/speaker-diarization-community-1` (see
[Model & Caching](#model--caching) above). "v4" below refers to the `pyannote.audio` 4.x
model/API generation the app uses, not a selectable value. See
[HuggingFace Token Setup](../installation/huggingface-setup.md) for what to accept on
HuggingFace, and [Native Diarization Engine](#native-diarization-engine-diar-native) below for
which process actually runs the pipeline.

```bash
# Speaker Detection Ranges
MIN_SPEAKERS=1         # Minimum speakers to detect
MAX_SPEAKERS=20        # Maximum speakers to detect (no hard limit, can increase for large events)

# Where v4 (256-dim) voiceprints are computed. Default true: they come from the
# diarizer's own centroids, or from the diar-native sidecar when a separate
# extraction is needed — both run the same WeSpeaker ResNet34-LM weights the
# in-process model does, so this is a deployment choice, not an accuracy one.
# Set false to force the in-process PyAnnote model (the escape hatch; costs a
# 40-60s model load and ~500MB VRAM per worker). v3 (512-dim) installs always use
# the in-process model — `pyannote/embedding` is a different network that the
# sidecar does not serve.
USE_NATIVE_SPEAKER_EMBEDDINGS=true

MODEL_CACHE_DIR=./models
```

### Speaker Detection Use Cases

| Event Type | Speakers | Recommended MAX_SPEAKERS | Notes |
|-----------|----------|-------------------------|-------|
| Small meetings | 2-5 | 20 (default) | Works well with default |
| Medium meetings | 5-15 | 20 (default) | Works well with default |
| Large conferences | 15-30 | 30-40 | Increase MAX_SPEAKERS |
| Very large events | 30-50+ | 50-100 | No hard limit |

### Model preloading

There is no `WARM_CACHE_ENABLED` variable — that was never implemented. The real mechanism is
`PRELOAD_GPU_MODELS` (see [GPU Concurrent Processing](#gpu-concurrent-processing) below): set it
`true` on a GPU worker's compose service to load its model at container start instead of on the
first task.

## Native Diarization Engine (diar-native)

`ENGINE_DIARIZER_BACKEND` selects which diarizer serves `local` (on-box) diarization —
`native` (the coded default) routes to the `diar-native` sidecar, `pyannote` routes to the
in-process PyAnnote fork. This is a **different axis** from `ASR_PROVIDER`/diarization-source
above: it only decides which *engine* runs once diarization is already happening locally. See
[Speaker Diarization → Native Diarization Engine](../features/speaker-diarization.md#native-diarization-engine-new-in-v050)
for what the sidecar does and how its weights get provisioned; this section is only the
variable reference. Defaults below come from `.env.example`'s "GPU AND TRANSCRIPTION" block and
`docker-compose.diar-native.yml` — not invented here.

```bash
# Engine selection. SystemSettings `engine.diarizer_backend` (Settings -> Engine) wins over
# this; both are re-read on every diarization call, so neither needs a worker restart.
ENGINE_DIARIZER_BACKEND=native   # native (default) | pyannote

# Worker -> sidecar HTTP client (backend/app/transcription/diarizer_native.py)
DIAR_NATIVE_URL=http://diar-native:8701
DIAR_NATIVE_SHARED_DIR=/scratch/opentranscribe/diar
DIAR_NATIVE_TIMEOUT_S=1800
DIAR_NATIVE_GENDER=1             # ask the sidecar for gender in the same pass

# The diar-server process itself (docker-compose.diar-native.yml / -gpu.yml)
#DIAR_NATIVE_MODE=               # cuda | mps | cpu -- leave unset; the compose overlay decides
DIAR_NATIVE_MAX_INFLIGHT=2
DIAR_NATIVE_LAZY_SESSIONS=1
#DIAR_NATIVE_GPU=                # defaults to GPU_DEVICE_ID -- never a bare 0
#DIAR_NATIVE_LOG_LEVEL=info
#DIAR_NATIVE_LOG_FORMAT=text     # text | json

# Model export (backend/app/transcription/native_provision.py, FastAPI lifespan)
#DIAR_NATIVE_MODELS_DIR=./models/diar-native   # default: ${MODEL_CACHE_DIR}/diar-native
#DIAR_NATIVE_AUTO_PROVISION=true
#DIAR_NATIVE_MODEL_SET=fast      # fast (default) | small (laptop tier)
#DIAR_NATIVE_PROVISION_TIMEOUT_S=1800
```

### Where each variable is read, and what changing it needs

| Variable | Read by | Takes effect after |
|---|---|---|
| `ENGINE_DIARIZER_BACKEND` | `TranscriptionConfig._resolve_diarizer_backend()`, resolved fresh on every diarization call | nothing to restart — live immediately, and a DB `engine.diarizer_backend` setting overrides it anyway |
| `DIAR_NATIVE_URL`, `DIAR_NATIVE_SHARED_DIR`, `DIAR_NATIVE_TIMEOUT_S`, `DIAR_NATIVE_GENDER` | module-level constants in `diarizer_native.py`, read once when the **celery worker** process imports it | restarting/recreating the celery worker container(s) |
| `DIAR_NATIVE_MODE`, `DIAR_NATIVE_MAX_INFLIGHT`, `DIAR_NATIVE_LAZY_SESSIONS`, `DIAR_NATIVE_GPU`, `DIAR_NATIVE_LOG_LEVEL`, `DIAR_NATIVE_LOG_FORMAT` | the `diar-server` Rust binary, at its own process start | recreating the `diar-native` container only |
| `DIAR_NATIVE_MODELS_DIR` | the compose bind-mount source for both `backend` and `diar-native`, and `native_provision.ensure_native_models` | recreating **both** the `backend` and `diar-native` containers |
| `DIAR_NATIVE_AUTO_PROVISION`, `DIAR_NATIVE_MODEL_SET`, `DIAR_NATIVE_PROVISION_TIMEOUT_S` | `native_provision.py`, read once from the **backend's** FastAPI lifespan at startup | restarting the backend |

### Provisioning is automatic

The backend exports the ONNX/PLDA model set itself on first startup — nothing to run by hand
on a normal install. It needs `HUGGINGFACE_TOKEN` (above) from an account that has also
accepted the terms at
[pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
(the gate is per-account and auto-approved; a valid token whose account never accepted the
terms fails identically — HTTP 403). Measured cold: 483,882,939 bytes in 137 seconds on a warm
HuggingFace cache. It's idempotent behind a `diar-provision.json` marker in
`DIAR_NATIVE_MODELS_DIR`, so every startup after the first is a `stat` pass, and the
`diar-native` compose service waits on `depends_on: backend: condition: service_healthy` so it
never starts against an empty `/models` and crash-loops.

A failure here is never fatal to startup — diarization falls back to the in-process PyAnnote
engine, logged, and that is a supported configuration. `./opentranscribe.sh download-models
diar-native` runs the same export outside the backend's lifespan: use it to pre-provision
before first start, to force a re-export, or on a multi-replica deployment where
`DIAR_NATIVE_AUTO_PROVISION=false` hands the export to a dedicated job instead of racing
several backend replicas against the same files.

### CPU vs GPU

`docker-compose.diar-native.yml` is CPU-safe on its own — no GPU device reservation,
`DIAR_MODE` defaults to `cpu`. A second overlay, `docker-compose.diar-native-gpu.yml`, adds the
nvidia device reservation (pinned to `DIAR_NATIVE_GPU`, falling back to `GPU_DEVICE_ID`) and
flips `DIAR_MODE` to `cuda`. Both `opentr.sh` and `opentranscribe.sh` add that second overlay
automatically whenever they've already detected an nvidia runtime for the rest of the stack —
there is no separate flag to set. Idle GPU footprint is ~2.2 GB of warm ONNX Runtime arena once
the sidecar has served a request (measured on `diar-server` 0.3.1; pre-0.3.1 builds held
roughly double that, so re-measure with `nvidia-smi --query-compute-apps` rather than trusting
a fixed number here).

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

:::warning[A replica needs a second node to mean anything]
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
CLOUD_ASR_CONCURRENCY=16           # Concurrency for cloud-asr worker (default 16)
```

### Deployment Mode

```bash
DEPLOYMENT_MODE=full  # full (local GPU + optional cloud) or lite (cloud-only, no GPU, ~2GB image)
# BACKEND_LITE_IMAGE=davidamacey/opentranscribe-backend-lite:latest
# ^ leave commented out: docker-compose.lite.yml derives it from OT_IMAGE_TAG when unset,
# which is what lets `update --version` / `--rollback` pin/upgrade/roll back a lite install.
# Setting it (as this line used to ship, uncommented) overrides that pinning permanently.
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
BEDROCK_RETRY_MODE=adaptive  # botocore retry mode: adaptive | standard | legacy
BEDROCK_MAX_ATTEMPTS=8       # total attempts per call, including the first
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
DOWNLOAD_CONCURRENCY=5   # Default: 5
DOWNLOAD_MAX_TASKS=10     # Restart after N tasks

# NLP worker: LLM summarization, speaker ID
NLP_CONCURRENCY=4         # Default: 4
NLP_MAX_TASKS=50           # Restart after N tasks

# Cloud ASR worker
CLOUD_ASR_CONCURRENCY=16   # Default: 16
```

### Speaker attribute (gender) detection memory

Gender detection runs a wav2vec2 model on the CPU worker. Its memory grows with the length of
the clip it is given. Each detection costs about 0.7 GB for the model plus about 0.4 GB of
working memory at the default clip cap, and every CPU worker process shares one container
memory limit. Two settings bound it:

```bash
# Longest clip (seconds, taken from the middle of a speaking turn) the model is given
SPEAKER_ATTRIBUTE_MAX_CLIP_SECONDS=20   # Default: 20 (minimum 2)
# Detections allowed at once per worker host/container; extra ones re-queue themselves
SPEAKER_ATTRIBUTE_MAX_CONCURRENCY=2     # Default: 2 (0 = unbounded)
```

A CPU worker also unloads the model after each detection, so idle worker processes don't each
keep their own copy.

## Task Recovery

A periodic health check (every 10 minutes) and a startup recovery pass reclaim work whose worker
died. They never fail, or dispatch a second copy of, a transcription that is only **waiting** for
a worker — for example while GPU workers are scaled to zero, paused, or behind a long backlog.

Each transcription run carries two markers in Redis:

- a **queued** marker, set when the pipeline is published and whenever a stage hands the run back
  to the queue;
- a **lease** (heartbeat), refreshed by a worker while it is executing a stage, which also records
  which broker message holds it.

A run is treated as dead only when it has neither — its worker stopped heartbeating, or its
message is gone from the queue. When recovery does retry a file, it cancels the old run, and a
stage that picks up a run which has since been replaced exits without doing any work. If Redis
cannot be read, recovery leaves transcriptions alone.

### When a worker dies mid-transcription

A worker killed while it holds a stage (out of memory, SIGKILL at the end of a container's stop
grace period, node loss, container restart) used to leave that stage's message in the broker's
*unacked* set until the 6 h visibility timeout, and recovery then marked the file **failed**. Now
an **orphan reaper** runs every minute:

- If the stage's message is still in the broker, it is put back at the **head** of its queue (so
  the file keeps its place ahead of newer submissions) and removed from the unacked set, so it is
  never redelivered as a duplicate later. The file stays in processing throughout.
- If the message is gone too (a cold shutdown's cancel drops it on Redis), a replacement run is
  dispatched at retry priority.

A worker loss is noticed within one lease TTL plus one sweep — about 2.5 minutes by default.
Infrastructure requeues are counted per file, separately from the admin retry limit, and a file
that keeps killing workers fails with *"interrupted ... after several automatic retries"* once
`TRANSCRIPTION_MAX_INFRA_REQUEUES` is spent.

A stage of a file that is being cancelled is never put back; its delivery is dropped and the
cancellation backstop resolves the file. Every other late-acknowledged task (utility, CPU and
enrichment tasks, and countdown tasks a worker was holding until their time) has no lease, so
the reaper puts it back once no live worker has reported holding it for
`BROKER_ORPHAN_UNTRACKED_STALE_SECONDS` after delivery or after its ETA. A task the
worker-loss replay sweep already tracks is left to that sweep.

```bash
TRANSCRIPTION_HEARTBEAT_INTERVAL_SECONDS=15   # Default: 15 — lease refresh
TRANSCRIPTION_HEARTBEAT_TTL_SECONDS=90        # Default: 90 — detection latency for a dead worker
BROKER_ORPHAN_SWEEP_INTERVAL_SECONDS=60       # Default: 60 — how often the reaper runs
BROKER_ORPHAN_STALE_SECONDS=120               # Default: 120 — grace for a delivery not yet started
BROKER_ORPHAN_UNTRACKED_STALE_SECONDS=600     # Default: 600 — grace for a non-transcription delivery
BROKER_ORPHAN_MAX_REQUEUES=5                  # Default: 5 — worker losses per non-transcription message
TRANSCRIPTION_MAX_INFRA_REQUEUES=5            # Default: 5 — worker losses per file before it fails
TRANSCRIPTION_INFRA_REQUEUE_ALERT_THRESHOLD=3 # Default: 3 — for the poison-file alert gauge
```

**Keep `CELERY_VISIBILITY_TIMEOUT` at 6 h.** Redis has no per-message lease, so that one value
applies to every late-acknowledged task however long it legitimately runs; lowering it re-runs
live work. Recovery no longer depends on it.

### Failures: permanent vs transient

Every processing failure is sorted into one of two classes:

- **Permanent** — the input is unusable: corrupt or undecodable media, no audio track, no
  speech, an empty or too-short file, an unsupported format, DRM/encrypted content. The file
  fails at once with that reason and is never retried automatically.
- **Transient** — the infrastructure failed (out of memory, a lost connection, a timeout, a
  worker that died) or the cause is unknown. The file is put back in the queue within minutes,
  with exponential backoff and jitter, **ahead of files submitted after it**, until the admin's
  *max retries* setting (Settings → Transcription) is used up. Only then does it fail, with a
  reason that says it was interrupted and retried.

Celery's own shutdown settings: `CELERY_WORKER_SOFT_SHUTDOWN_TIMEOUT` (default 30) and
`CELERY_WORKER_SOFT_SHUTDOWN_ON_IDLE` (default true) apply to a **cold** shutdown (SIGQUIT) only;
a plain SIGTERM is a warm shutdown that waits for running tasks without limit, so give worker
containers a stop grace period longer than your longest stage. Prefer the warm shutdown: on the
Redis broker a cold shutdown's cancel acknowledges (drops) the running stage's message, and the
file then waits for the reaper to re-dispatch it.

```bash
# Longest a transcription may wait in the queue before its message is treated as lost
TRANSCRIPTION_QUEUE_MAX_WAIT_SECONDS=604800   # Default: 7 days


# Longest a transcription may RUN, measured from when a worker started it (not from upload)
TASK_MAX_DURATION_TRANSCRIPTION_SECONDS=3600  # Default: 3600
# Budget for other task types, which record their start when they begin running
TASK_MAX_DURATION_DEFAULT_SECONDS=1800        # Default: 1800

# How long a task must go without an update before the health check considers it
TASK_RECOVERY_STALENESS_SECONDS=300           # Default: 300
# ...and before startup recovery considers it orphaned
TASK_RECOVERY_ORPHANED_HOURS=1                # Default: 1
```

An invalid value (non-numeric, or below 1) logs a warning and falls back to the default. The
values are read when a worker starts, so restart the workers after changing them.

### Tasks whose worker dies

When a worker is killed mid-task (out of memory, SIGKILL, node loss), Celery can't hand the
task to another worker. Most tasks are acknowledged as soon as a worker receives them, so the
message is gone. A task that acknowledges late waits in the broker until the visibility
timeout (6 h). So the tasks that are safe to run twice (speaker attributes, speaker
clustering, analytics, waveform, thumbnail, playback rendition, search indexing, file facts,
summary, topics, LLM speaker identification) record themselves while they run and send a
heartbeat. A sweep every two minutes re-sends any whose heartbeat has lapsed, under the same
task id, and gives up (marking the task failed) after a set number of re-sends, so a task
that kills its worker every time can't loop. Transcription is never re-sent this way.

```bash
# Heartbeat refresh interval, and how long a worker may go silent before its tasks are re-sent
TASK_HEARTBEAT_INTERVAL_SECONDS=30   # Default: 30
TASK_HEARTBEAT_TTL_SECONDS=120       # Default: 120 (must exceed the interval)
# How many times one task is re-sent after losing its worker before it is failed
TASK_REPLAY_MAX_ATTEMPTS=2           # Default: 2 (0 = never re-send)
# Records older than this are dropped instead of re-sent
TASK_REPLAY_MAX_AGE_SECONDS=86400    # Default: 86400
```

## Flower Monitoring Dashboard

```bash
FLOWER_USER=admin
FLOWER_PASSWORD=auto_generated_on_install
FLOWER_URL_PREFIX=flower  # URL prefix (must match nginx proxy_pass path)
```

Flower provides industry-standard Celery task monitoring with persistent task history, queue visibility, and worker status. Access at `http://localhost:5175/flower` (or via NGINX at `/flower/`).

## Worker Task Metrics

```bash
WORKER_METRICS_PORT=          # Default: unset (off). A TCP port, e.g. 9808, to turn it on
```

Each Celery worker can serve per-task Prometheus metrics at `http://<worker>:<port>/metrics`:

| Metric | Labels | Meaning |
|---|---|---|
| `celery_task_total` | `task`, `outcome` | Tasks that ended in this worker. `outcome` is `success`, `failure`, `retry` or `revoked` |
| `celery_task_runtime_seconds` | `task` | Histogram of run time, start to end, whatever the outcome (buckets 0.5 s to 2 h) |

- `task` is the registered task name (for example `transcription.gpu_transcribe`). A name the
  worker has not registered is counted as `other`, so the label set is bounded by the task list.
  No file, user or task ids are ever used as labels.
- A task whose worker died and that was put back on its queue is not counted: it has not ended.
  A task that failed because its worker process died is counted as `failure`.
- Unset, empty or invalid means off: nothing is recorded and no port is opened. `.env` is shared by
  every service, but only `celery ... worker` processes read the variable, so the API, beat and
  Flower are unaffected. Each worker container has its own network namespace, so the same port
  works for all of them; the port is not published to the host, so scrape it from the compose
  network (for example `celery-cpu-worker:9808`).
- **Prefork workers.** Tasks run in forked child processes, which cannot share one port. A worker
  with this variable set runs `prometheus_client` in multiprocess mode: every process writes its
  samples to a file in `PROMETHEUS_MULTIPROC_DIR` and the worker's main process serves them all,
  including the counts of children already recycled by `--max-tasks-per-child`. The directory is a
  fresh temporary one by default, emptied at startup and removed at shutdown; set
  `PROMETHEUS_MULTIPROC_DIR` only to choose where it lives (one directory per worker). Threads and
  solo pools (the GPU and redaction workers) use the same mechanism with a single process.
- Because multiprocess mode applies to the whole worker process, other collectors a worker updates
  (for example `db_query_duration_seconds`) are served on the same port.

## Object Storage

OpenTranscribe stores uploaded media in an S3-compatible bucket. `STORAGE_BACKEND=minio` (the
bundled, self-hosted MinIO container) is the default and remains fully backward compatible; a
native AWS S3 backend is also available for cloud deployments.

```bash
# Storage Backend Selection
STORAGE_BACKEND=minio  # minio (default, self-hosted) or s3 (native AWS S3 / S3-compatible)
```

### Buckets (both backends)

```bash
MEDIA_BUCKET_NAME=opentranscribe    # uploaded originals — the source of truth
CACHE_BUCKET_NAME=processed-videos  # regenerable derived assets and bulk-export ZIPs
```

Two buckets, on purpose: everything in `CACHE_BUCKET_NAME` (subtitle-embedded videos and
extracted audio under `derived/`, bulk-export ZIPs under `bulk/`) is a duplicate re-created on
demand, so a lifecycle rule can expire that bucket wholesale without a rule that could ever
match an original. Both buckets are created on first use if missing.

Set `CACHE_BUCKET_NAME` whenever a bucket named `processed-videos` is not available to the
configured credentials — S3 bucket names are one global namespace, so on native S3 the default
generally is not yours. Getting this wrong is not a boot failure: the backend starts, and bulk
subtitle export, subtitle-embedded video download and the admin media-cache screens fail at
request time instead.

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

On S3, quarantining a file does not by itself revoke media URLs already issued for it: they stay
valid for up to `MEDIA_URL_EXPIRE_SECONDS`. See
[Production Deployment](../operations/production-deployment.md#native-aws-s3-backend-alternative-to-minio)
for the bucket policy that enforces revocation and the TTL trade-off.

### Presigned URLs and large uploads (both backends)

```bash
STORAGE_PUBLIC_URL=               # backend-agnostic alias for MINIO_PUBLIC_URL; empty keeps the /s3 proxy path on MinIO and leaves native S3 URLs untouched
PRESIGNED_URL_MAX_SECONDS=21600   # 6h default -- a presigned URL cannot outlive the credentials that signed it (IAM-role STS sessions expire well inside 24h)
MULTIPART_THRESHOLD_MB=512        # objects at/above this size use browser-side multipart upload
API_MEDIATED_UPLOAD_ENABLED=true  # false = refuse POST /api/files (file streamed through the API); uploads go browser -> storage only
```

:::note[Disabling the API-mediated upload]
Uploads normally go straight from the browser to object storage over presigned URLs;
`POST /api/files` is only a fallback that streams the whole file through the API process.
With `API_MEDIATED_UPLOAD_ENABLED=false` that route answers 404 before reading the body, the
browser is told (via `/api/system/capabilities`) never to fall back to it, and a failed
presigned attempt is retried on the presigned path instead. Multi-GB uploads are unaffected:
they already use the presigned multipart path. With it enabled (the default), the route's
body is capped at `MAX_UPLOAD_BYTES` while it streams.
:::

:::note[S3 vs MinIO single-PUT ceiling]
MinIO accepts a single-PUT object up to 5 TiB. AWS S3 rejects a single PUT above 5 GiB
(`EntityTooLarge`), so on `STORAGE_BACKEND=s3` an upload above that size always goes through the
multipart path regardless of `MULTIPART_THRESHOLD_MB`.
:::

### GDPR erasure journal

```bash
ERASURE_JOURNAL_BACKEND=file                    # file | object_storage
ERASURE_JOURNAL_OBJECT_PREFIX=gdpr/erasure-journal/  # object_storage only; key prefix in the media bucket
```

Every GDPR erasure request is also written outside the database, so restoring an older dump
cannot silently undo it (the reconciliation sweep re-opens anything the database lost). With
`file` (the default) that journal is `DATA_DIR/gdpr/erasure-journal.jsonl` on the data volume.
Set `object_storage` when containers have **no durable writable volume** — a read-only root
filesystem, or pods whose local disk does not outlive them. Each entry then becomes one object
under the prefix in `MEDIA_BUCKET_NAME` (surrogate keys only, no personal data). Do not point
`DATA_DIR` at an ephemeral volume instead: the journal would vanish with the pod, which is the
failure it exists to prevent. Keep the prefix out of any bucket lifecycle expiration rule.

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

### API connection pool and threadpool

```bash
DB_POOL_SIZE=20          # API process SQLAlchemy pool
DB_MAX_OVERFLOW=40       # extra connections above the pool under load
API_THREADPOOL_SIZE=0    # 0 = match DB_POOL_SIZE + DB_MAX_OVERFLOW (never below 40)
```

The API runs synchronous handlers, synchronous dependencies and `run_in_threadpool` calls on
one thread pool. A request holds its pooled database connection from authentication until the
response is sent, including while it waits for a thread for its next step. With `C` connections
(`DB_POOL_SIZE + DB_MAX_OVERFLOW`) and `T` threads, a burst of `C + T` or more concurrent requests
can leave every connection with a request waiting for a thread and every thread with a request
waiting for a connection, until the pool timeout (30 s) fails the waiters.

The default therefore sizes the thread pool to the pool capacity, so one API process handles up
to twice its pool capacity of concurrent requests (120 with the defaults) without reaching that
state. To serve more, raise the pool (and `PG_MAX_CONNECTIONS`, which must stay above the sum of
every service's pool) or run more API processes; setting `API_THREADPOOL_SIZE` below the pool
capacity logs a warning at startup.

Hooks registered by a deployment (for example an upload-limits resolver) run on this thread
pool, never on the event loop, but they run while the request holds a connection: keep them
fast, and put a timeout on any network call they make.

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

OpenTranscribe ships a Content Security Policy to mitigate cross-site scripting (XSS) and other injection attacks ([#124](https://github.com/attevon-llc/OpenTranscribe/issues/124)). It is generated by SvelteKit from `kit.csp` in `frontend/svelte.config.js` and emitted as a `<meta http-equiv="content-security-policy">` in the built `index.html`, in hash mode, so the inline SPA bootstrap is hashed and `script-src` needs no `'unsafe-inline'`. Key directives include:

- `default-src 'self'` -- baseline restriction to same-origin resources
- `script-src 'self' 'wasm-unsafe-eval'` -- plus the bootstrap's hash; `wasm-unsafe-eval` is for the FFmpeg.wasm worker
- `connect-src 'self'` -- same-origin API calls and the real-time notifications WebSocket. Under CSP Level 3, `'self'` matches `ws:`/`wss:` on the page's own host, which is all the socket needs (its URL is built from the page's location). Bare `ws:`/`wss:` sources are deliberately absent: they would allow a socket to **any** host ([#1028](https://github.com/attevon-llc/OpenTranscribe/issues/1028)), and `npm run build` fails if one reappears.
- `object-src 'none'`, `base-uri 'self'`, `form-action 'self'` -- defense-in-depth directives

`frame-ancestors` is not valid in a `<meta>` policy; `X-Frame-Options: SAMEORIGIN` from the frontend nginx covers clickjacking. The optional reverse-proxy overlay (`nginx/site.conf.template`) additionally sends a CSP **header**; the browser enforces both, so a source must be allowed by each. The Vite dev server (`./opentr.sh start dev`) sends the same policy as a response header instead of a `<meta>` tag. Its hot-reload socket is same-host, so `'self'` covers it too.

:::note Deployers who rewrite the CSP
If you rewrite `connect-src` with an nginx `sub_filter` (or similar), the string to match is now `connect-src 'self'` rather than `connect-src 'self' ws: wss:`. Add an explicit scheme+host (for example `wss://realtime.example.com`), never a bare scheme.
:::

## CORS Origins

`CORS_ORIGINS` lists the browser origins allowed to make **credentialed cross-origin** API calls. Every shipped deployment serves the SPA and the API from the same origin, which needs no CORS entry, so leave it unset unless your frontend is served from a different origin.

| `ENVIRONMENT` | `CORS_ORIGINS` unset resolves to |
|---|---|
| hardened (`production`, unset, or anything not listed below) | `[]` -- no cross-origin access |
| `development`, `dev`, `testing`, `test`, `local` | `http://localhost:5173`, `http://127.0.0.1:5173` (the Vite dev server) |

An explicit value always wins, as a comma-separated list (`CORS_ORIGINS=https://app.example.com,https://admin.example.com`) or a JSON list (`CORS_ORIGINS=["https://app.example.com"]`). A wildcard (`*`) is refused at startup in a hardened deployment. The backend logs the resolved list at startup as `CORS allowed origins: ...`.

Before [#1029](https://github.com/attevon-llc/OpenTranscribe/issues/1029), a production deployment that never set the variable allowed the Vite dev origins, so any page served on `localhost:5173` on a user's machine could make credentialed requests. Behind the shipped nginx configs, the WebSocket same-origin check does not depend on this list. The Vite dev server's proxy rewrites `Host`, so in development the notifications socket is admitted through the list instead, which is why the relaxed default keeps the Vite origins. A dev stack on a non-default frontend port (for example `--fresh --port-offset`) needs that origin in `CORS_ORIGINS`.

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

:::warning[There is no `AUTH_TYPE` setting]
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

:::note[`KEYCLOAK_*` still works]
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
# Password Policy (FedRAMP IA-5 / NIST SP 800-63B-4) - see Password Policy page
PASSWORD_POLICY_PROFILE=standard      # basic | standard | hardened | custom (nist/stig = aliases; code default hardened; new installs standard)
PASSWORD_MAX_LENGTH=0                 # 0 = level default
PASSWORD_BLOCKLIST_ENABLED=           # empty = level default
PASSWORD_BLOCKLIST_PATH=
PASSWORD_CONTEXT_WORDS=
PASSWORD_HIBP_ENABLED=false           # online k-anonymity lookup; fails open
PASSWORD_HIBP_URL=https://api.pwnedpasswords.com/range
PASSWORD_HIBP_TIMEOUT_SECONDS=3
MFA_REQUIRED_FOR_ADMINS=false         # recommended true when MFA_ENABLED=true
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

:::tip[Configure these in the UI]
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

:::warning[AWS variables are shared]
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

:::tip[Configure these in the UI]
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

:::danger[Keep it false on multi-tenant deployments]
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

:::warning[Changing chunk size requires a full reindex]
Chunk boundaries are baked into the index at write time. Changing
`SEARCH_CHUNK_TARGET_WORDS` or `SEARCH_CHUNK_OVERLAP_WORDS` affects only
newly-indexed content until you reindex everything, which leaves a corpus chunked
two different ways in the meantime.
:::

:::warning[Replicas on a single node]
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

:::note[Setting these is optional]
None of the above is required. A deployment with `LLM_PROVIDER` empty and
`ASR_PROVIDER=local` transcribes normally with no cloud account at all — which is
the default self-hosted configuration.
:::
