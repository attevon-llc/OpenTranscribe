---
sidebar_position: 5
---

# Offline / Airgapped Installation

OpenTranscribe supports complete offline deployment for airgapped environments, secure facilities, or locations with limited internet access.

## Overview

In offline mode, OpenTranscribe operates without any internet connectivity:
- ✅ Transcription works fully offline
- ✅ Speaker diarization works offline
- ✅ All AI models cached locally
- ✅ No external API calls
- Not supported: YouTube downloads (requires internet)
- Not supported: Cloud LLM providers (use local LLM instead)

## Prerequisites

You'll need an **internet-connected machine** to:
1. Download Docker images
2. Download AI models (~2.9GB)
3. Prepare installation package

Then transfer everything to your **offline machine**.

## Step 1: Prepare on Internet-Connected Machine

### Download Docker Images

```bash
# Pull all required images
docker pull davidamacey/opentranscribe-backend:latest
docker pull davidamacey/opentranscribe-frontend:latest
docker pull postgres:15-alpine
docker pull redis:7-alpine
docker pull minio/minio:latest
docker pull opensearchproject/opensearch:3.3.1

# Save images to tarball
docker save -o opentranscribe-images.tar \
  davidamacey/opentranscribe-backend:latest \
  davidamacey/opentranscribe-frontend:latest \
  postgres:15-alpine \
  redis:7-alpine \
  minio/minio:latest \
  opensearchproject/opensearch:3.3.1
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
tar -czf ai-models.tar.gz ~/.cache/huggingface ~/.cache/torch
```

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
- `ai-models.tar.gz` (~2.9GB)
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
