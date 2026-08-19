---
sidebar_position: 5
---

# Offline / Airgapped Installation

OpenTranscribe supports complete offline deployment for airgapped environments, secure facilities, or locations with limited internet access.

## Overview

In offline mode, OpenTranscribe operates without any internet connectivity:
- ✅ Transcription works fully offline
- ✅ Speaker diarization works offline
- ✅ All AI models cached locally — **when fetched with `scripts/download-models.sh`**.
  The hand-rolled recipe below omits the NLTK corpora; see the warning there.
- ✅ No external API calls
- 💡 Set `NLTK_OFFLINE=1` alongside `HF_HUB_OFFLINE=1` so a missing corpus fails
  fast naming the setup step, instead of hanging on a socket timeout.
- Not supported: YouTube downloads (requires internet)
- Not supported: Cloud LLM providers (use local LLM instead)

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
docker pull postgres:17.5-alpine
docker pull redis:8.2.2-alpine3.22
docker pull minio/minio:RELEASE.2025-09-07T16-13-09Z
docker pull opensearchproject/opensearch:3.4.0

# Save images to tarball
docker save -o opentranscribe-images.tar \
  davidamacey/opentranscribe-backend:latest \
  davidamacey/opentranscribe-frontend:latest \
  postgres:17.5-alpine \
  redis:8.2.2-alpine3.22 \
  minio/minio:RELEASE.2025-09-07T16-13-09Z \
  opensearchproject/opensearch:3.4.0
```

### Download AI Models

```bash
# Set HuggingFace token
export HUGGINGFACE_TOKEN=hf_your_token_here

# Download models using Python
python3 << 'EOF'
from transformers import WhisperForConditionalGeneration, WhisperProcessor
from pyannote.audio import Model
import torch

# WhisperX models
WhisperForConditionalGeneration.from_pretrained("Systran/faster-whisper-large-v2")
WhisperProcessor.from_pretrained("Systran/faster-whisper-large-v2")

# PyAnnote models
Model.from_pretrained("pyannote/segmentation-3.0")
Model.from_pretrained("pyannote/speaker-diarization-3.1")
EOF

# Package model cache
tar -czf ai-models.tar.gz ~/.cache/huggingface ~/.cache/torch ~/.cache/nltk_data
```

:::warning The hand-rolled recipe above is incomplete — prefer the script

The Python snippet fetches the transcription and diarization weights and nothing
else. It **omits the NLTK corpora**, which the sentence splitter and topic
extraction load at runtime — and on an airgapped host those fetches do not fail
fast, because `nltk.download` swallows its own network errors. The symptoms are
quiet: transcripts chunked by the regex fallback instead of punkt (different
chunk boundaries, therefore different search results), and keyword extraction
keeping common words.

Use `scripts/download-models.sh` instead, which fetches every group the app
loads, or `./opentr.sh start`, which calls it. To fetch just the corpora:

```bash
python3 scripts/download-models.py --only nltk
```

Issue #491 tracked this gap.
:::

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
tar -xzf opentranscribe-offline.tar.gz
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
docker compose -f docker-compose.yml -f docker-compose.offline.yml up -d

# Or using the script
./opentranscribe.sh start offline
```

## Offline Configuration

The `docker-compose.offline.yml` file disables internet-dependent features:

- YouTube download worker disabled
- External network access restricted
- Model downloads disabled (uses local cache)

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

Configure in `.env`:
```bash
LLM_PROVIDER=vllm  # or ollama
VLLM_API_URL=http://your-server:8000/v1
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
# Check all services running
docker compose ps

# Verify no internet access attempts
docker compose logs | grep -i "connect\|download"

# Test transcription
# Upload a test file through web UI
```

## Limitations

- Cannot download YouTube videos
- Cannot use cloud LLM providers (OpenAI, Claude, etc.)
- Cannot auto-update models
- ✅ All transcription features work
- ✅ Speaker diarization works
- ✅ Local LLM works (if configured)

## Next Steps

- [Docker Compose Installation](./docker-compose.md)
- [HuggingFace Setup](./huggingface-setup.md)
- [LLM Integration](../features/llm-integration.md)
