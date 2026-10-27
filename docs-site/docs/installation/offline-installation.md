---
sidebar_position: 5
---

# Offline / Airgapped Installation

OpenTranscribe supports complete offline deployment for airgapped environments, secure facilities, or locations with limited internet access.

## Overview

In offline mode, OpenTranscribe operates without any internet connectivity:
- ✅ Transcription works fully offline
- ✅ Speaker diarization works offline — **only if the diarizer's weights were included in the
  package you built**. Neither engine can provision itself air-gapped: the native `diar-native`
  sidecar's ONNX/PLDA export and the in-process PyAnnote fork's weights both come from
  HuggingFace, and an offline machine has no route there. See
  [Including the native diarizer (diar-native)](#including-the-native-diarizer-diar-native)
  below.
- ✅ All AI models cached locally — **when fetched with `scripts/download-models.sh`**.
  Assembling the cache by hand omits the NLTK corpora; see the warning below.
- ✅ No external API calls
- 💡 Set `NLTK_OFFLINE=1` alongside `HF_HUB_OFFLINE=1` so a missing corpus fails
  fast naming the setup step, instead of hanging on a socket timeout.
- Not supported: YouTube downloads (requires internet)
- Not supported: Cloud LLM providers (use local LLM instead)

## Recommended: build and install the offline package

The maintained route is the packaged one. On an internet-connected checkout of the repository:

```bash
export HUGGINGFACE_TOKEN=hf_your_token_here
./scripts/build-offline-package.sh            # add --local to package locally built images
```

It pulls the images, downloads every model group (including the native diarizer export), and writes `offline-package-build/linux/opentranscribe-offline-v<version>.tar.xz` with a `.sha256` file (the package is tens of GB). On the air-gapped machine, extract it and run `sudo ./install.sh`; the installer loads the images, installs to `/opt/opentranscribe/` and copies the models. Then edit `/opt/opentranscribe/.env` and manage the stack with `sudo ./opentr.sh start` from `/opt/opentranscribe/` (that file is `scripts/opentr-offline.sh`, which builds the base plus offline compose chain for you). The repository's `README-OFFLINE.md` documents the package build, install, usage and maintenance in full.

The manual steps below show what a package contains and are a fallback.

## Prerequisites

You'll need an **internet-connected machine** to:
1. Download Docker images
2. Download AI models (several GB — see below)
3. Prepare installation package

Then transfer everything to your **offline machine**.

## Step 1: Prepare on Internet-Connected Machine

### Download Docker Images

```bash
# Pull all required images
docker pull davidamacey/opentranscribe-backend:latest
docker pull davidamacey/opentranscribe-frontend:latest
docker pull davidamacey/opentranscribe-docs:latest
docker pull postgres:17.5-alpine
docker pull redis:8.2.2-alpine3.22
docker pull quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z
docker pull opensearchproject/opensearch:3.4.0

# Save images to tarball
docker save -o opentranscribe-images.tar \
  davidamacey/opentranscribe-backend:latest \
  davidamacey/opentranscribe-frontend:latest \
  davidamacey/opentranscribe-docs:latest \
  postgres:17.5-alpine \
  redis:8.2.2-alpine3.22 \
  quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z \
  opensearchproject/opensearch:3.4.0
```

### Download AI Models

Run the download script from a repository checkout. It fetches every model group the app loads (transcription, speaker detection, NLTK corpora, embeddings, reranker, neural search, redaction) into a cache directory, using the backend image you pulled above:

```bash
# Set HuggingFace token
export HUGGINGFACE_TOKEN=hf_your_token_here

# Optional: choose the Whisper model and which neural-search models to include
export WHISPER_MODEL=large-v3-turbo
export DOWNLOAD_ALL_OPENSEARCH_MODELS=true

# Download into ./models
./scripts/download-models.sh ./models

# Package the model cache
tar -czf ai-models.tar.gz -C models .
```

:::warning[Do not assemble the cache by hand]

Fetching only the Whisper and PyAnnote weights yourself **omits the NLTK corpora**,
which the sentence splitter and topic extraction load at runtime — and on an airgapped
host those fetches do not fail fast, because `nltk.download` swallows its own network
errors. The symptoms are quiet: transcripts chunked by the regex fallback instead of
punkt (different chunk boundaries, therefore different search results), and keyword
extraction keeping common words. It also skips the native diarizer export described
below. To fetch just the corpora:

```bash
python3 scripts/download-models.py --only nltk
```

Issue #491 tracked this gap.
:::

### Including the native diarizer (diar-native)

The **native diarization engine** (`diar-native`, the default `local` diarizer as of v0.5.0 —
see [Speaker Diarization](../features/speaker-diarization.md#native-diarization-engine-new-in-v050))
needs an ONNX/PLDA export that `diar-server` produces from PyAnnote's weights. A cache assembled
by hand ships no `diar-native` model set at all. That is not fatal — without it, diarization falls
back to the in-process PyAnnote engine (with a warning per run, or a failed task if **Require
Diarization Sidecar** / `ENGINE_DIARIZER_REQUIRE_SIDECAR=true` is on) — but it means an air-gapped
install with no native sidecar unless you provision one before disconnecting.

The maintained way to build a complete offline package is `scripts/build-offline-package.sh`
(not the manual `docker save`/`tar` steps below, which remain useful for understanding what a
package contains). It already handles `diar-native`:
- `download_models()` mounts a `diar-native` model-cache directory into the backend container
  and runs `scripts/download-models.py` with no filter, which includes the `diar-native` group
  — this shells out to `diar-server provision-models` with the **same** `HUGGINGFACE_TOKEN` used
  for every other model, producing the export on the build machine (which has internet access).
- The export is copied into the package at `models/diar-native/`, and
  `docker-compose.diar-native.yml` is copied into `config/` so the offline install can load the
  sidecar with nothing else to fetch.
- `scripts/opentr-offline.sh` (the offline stack's equivalent of `opentr.sh`) auto-loads that
  overlay whenever `models/diar-native/` is non-empty on the target machine — there is no
  network route for it to check `HUGGINGFACE_TOKEN` and provision on the fly the way the
  connected install's lifespan hook does, so a populated export is the **only** signal it can
  act on.
- If `HUGGINGFACE_TOKEN` lacks access to
  [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
  when you run the build script, it copies nothing and prints a warning rather than failing the
  whole build — the resulting package still works, just on the PyAnnote fallback instead of the
  native engine.

### Download Installation Files

```bash
# Clone repository
git clone https://github.com/attevon-llc/OpenTranscribe.git
cd OpenTranscribe

# Create offline package
tar -czf opentranscribe-offline.tar.gz \
  docker-compose.yml \
  docker-compose.offline.yml \
  .env.example \
  database/ \
  scripts/ \
  opentranscribe.sh

# Copy offline setup script
cp setup-opentranscribe.sh opentranscribe-offline-setup.sh
```

## Step 2: Transfer to Offline Machine

Transfer these files to offline machine:
- `opentranscribe-images.tar` (~8GB)
- `ai-models.tar.gz` (several GB; size depends on `WHISPER_MODEL` and how many neural-search models you include)
- `opentranscribe-offline.tar.gz` (~5MB)

Via USB drive, secure file transfer, or your organization's approved method.

## Step 3: Install on Offline Machine

### Load Docker Images

```bash
# Load images
docker load -i opentranscribe-images.tar
```

### Extract Installation Files

```bash
# Extract installation
mkdir opentranscribe
tar -xzf opentranscribe-offline.tar.gz -C opentranscribe
cd opentranscribe

# Extract AI models
mkdir -p models
tar -xzf ../ai-models.tar.gz -C models/
```

### Configure Environment

```bash
# Copy environment template
cp .env.example .env

# Edit .env and configure:
# - Set HUGGINGFACE_TOKEN (still required for model loading)
# - Set MODEL_CACHE_DIR=./models
# - Configure passwords and secrets
nano .env
```

### Start Services

```bash
# Make script executable
chmod +x opentranscribe.sh

# Start in offline mode
# Note: opentranscribe.sh's `start` subcommand does not read an "offline"
# argument today, so `./opentranscribe.sh start offline` will NOT load this
# overlay -- use docker compose directly:
docker compose -f docker-compose.yml -f docker-compose.offline.yml up -d
```

## Offline Configuration

The `docker-compose.offline.yml` file makes the stack run from what is already on the machine:

- `pull_policy: never` on every service, so only pre-loaded images are used (it names the `:latest` tags that `docker save` wrote)
- `HF_HUB_OFFLINE=1` and `NLTK_OFFLINE=1` on the model-using workers, so a missing model or corpus fails fast instead of hanging on the network
- Models are read from `MODEL_CACHE_DIR` (`huggingface`, `torch`, `nltk_data`, `sentence-transformers`)

It does not disable the download worker or block outbound traffic; features that need the internet (media URL downloads, cloud ASR, cloud LLM providers) simply fail without a connection.

## Local LLM for Offline AI Features

To use AI summarization in offline mode, deploy a local LLM:

### Option 1: vLLM

```bash
# On separate GPU (recommended)
docker run --gpus '"device=0"' -p 8000:8000 \
  vllm/vllm-openai:latest \
  --model meta-llama/Llama-2-70b-chat-hf
```

### Option 2: Ollama

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull model (on internet machine, then copy)
ollama pull llama2:70b
```

Configure the provider in **Settings > AI & Chat > LLM Provider Configuration** (the supported route), or as a `.env` fallback:
```bash
LLM_PROVIDER=vllm  # or ollama
VLLM_BASE_URL=http://your-server:8000/v1
```

## Updating Offline Installation

To update an offline installation:

1. On internet machine: Pull new Docker images
2. Save to tarball
3. Transfer to offline machine
4. Load new images
5. Restart services

## Verification

```bash
# Check all services running (packaged install: sudo ./opentr.sh status)
docker compose -f docker-compose.yml -f docker-compose.offline.yml ps

# Verify no internet access attempts
docker compose -f docker-compose.yml -f docker-compose.offline.yml logs | grep -i "connect\|download"

# Test transcription
# Upload a test file through web UI
```

## Limitations

- Cannot download YouTube videos
- Cannot use cloud LLM providers (OpenAI, Claude, etc.)
- Cannot auto-update models
- Cannot provision (or re-provision) the native diarizer's weights on the air-gapped machine —
  build the package with `scripts/build-offline-package.sh` and a valid `HUGGINGFACE_TOKEN` so
  `models/diar-native/` ships already populated (see above), or accept the automatic fallback
  to the in-process PyAnnote engine
- ✅ All transcription features work
- ✅ Speaker diarization works (native engine if its weights shipped with the package, PyAnnote
  fallback otherwise)
- ✅ Local LLM works (if configured)

## Next Steps

- [Docker Compose Installation](./docker-compose.md)
- [HuggingFace Setup](./huggingface-setup.md)
- [LLM Integration](../features/llm-integration.md)
