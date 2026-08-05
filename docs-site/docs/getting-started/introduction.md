---
sidebar_position: 1
title: Introduction
---

# Welcome to OpenTranscribe

OpenTranscribe is a powerful, self-hosted AI-powered transcription and media analysis platform that turns your audio and video files into searchable, analyzable text with advanced features like speaker identification, AI summarization, and cross-media intelligence.

## What is OpenTranscribe?

OpenTranscribe combines state-of-the-art AI models with a modern web interface to provide:

- **High-accuracy transcription** using WhisperX with word-level timestamps
- **Automatic speaker identification** with voice fingerprinting across videos
- **AI-powered summarization** with customizable prompts and LLM integration
- **Full-text and semantic search** powered by OpenSearch
- **Privacy-first processing** - everything runs locally on your infrastructure

## Key Features

### Advanced Transcription
- WhisperX 3.8.1 with faster-whisper BatchedInferencePipeline backend
- Native word-level timestamps via cross-attention DTW (all 100+ languages, no separate alignment model)
- Multi-language support with optional translation to English (requires `large-v3` model)
- 40x+ realtime speed on GPU (`large-v3-turbo` default, 6x faster than `large-v2`)
- Support for audio (MP3, WAV, FLAC, M4A) and video (MP4, MOV, AVI, MKV)
- Cloud ASR providers: Deepgram, AssemblyAI, OpenAI, Google, AWS, Azure, Speechmatics, Gladia

### Smart Speaker Management
- Automatic speaker diarization using PyAnnote.audio
- Cross-video speaker recognition with voice fingerprinting
- LLM-enhanced speaker identification
- Global speaker profiles that persist across transcriptions
- Confidence scoring and manual verification workflow

### AI-Powered Features
- LLM summarization with BLUF (Bottom Line Up Front) format
- Support for multiple LLM providers (OpenAI, Claude, vLLM, Ollama, OpenRouter)
- Custom AI prompts for different content types
- Intelligent section-by-section processing for unlimited transcript lengths
- Speaker analytics and interaction patterns

### Search & Discovery
- Hybrid search combining BM25 keyword and neural semantic search via OpenSearch ML Commons
- OpenSearch 3.4.0 with ML Commons native neural search and RRF merging
- Advanced filtering by speaker, date, tags, duration
- Collections for organizing related media with group sharing
- Interactive waveform visualization with click-to-seek

### Analytics & Insights
- Speaker analytics (talk time, interruptions, pace)
- Meeting efficiency metrics
- Action item extraction
- Cross-video speaker tracking

## Why OpenTranscribe?

### Open Source & Self-Hosted
- **Full control** over your data - nothing leaves your infrastructure
- **AGPL-3.0 License** - open source with network copyleft protection
- **No subscription fees** - one-time setup, unlimited use
- **Privacy-first** - ideal for sensitive content (legal, medical, business)

### Production-Ready
- **Docker-based deployment** - runs anywhere
- **GPU acceleration** - NVIDIA GPUs supported
- **Multi-worker architecture** - process multiple files in parallel
- **Offline capable** - works in airgapped environments

### Enterprise Security
- **Multiple authentication methods** - Local, LDAP/AD, OIDC/Keycloak, PKI/X.509
- **Multi-factor authentication** - TOTP-based MFA with backup codes
- **Password policies** - Configurable complexity, history, and expiration
- **Audit logging** - FedRAMP-compliant structured logging
- **Account lockout** - Progressive lockout after failed attempts

### Modern Stack
- **Svelte + TypeScript frontend** - responsive, PWA-enabled
- **FastAPI backend** - high-performance Python
- **PostgreSQL + OpenSearch** - reliable, scalable storage
- **Celery workers** - distributed background processing

### User Interface
- **Light and dark mode** - Toggle between themes via the sun/moon icon in the navbar, or let the app follow your system preference automatically
- **8 UI languages** - Switch the interface language from Settings: English, Spanish, French, German, Portuguese, Chinese, Japanese, and Russian. The app also detects your browser language on first visit
- **Grid and list views** - Toggle between card-based grid view (with thumbnails) and compact list view in the file gallery using the view toggle button
- **Virtual scrolling** - Smooth performance when browsing large libraries with thousands of files, loading only visible items
- **Progressive Web App (PWA)** - Install OpenTranscribe as a standalone app on desktop or mobile from your browser's "Install" or "Add to Home Screen" option for a native app experience

## Use Cases

OpenTranscribe is perfect for:

- **Meeting transcriptions** - Record and analyze team meetings with speaker identification
- **Podcast production** - Generate transcripts and show notes automatically
- **Academic research** - Transcribe interviews and lectures for analysis
- **Legal & compliance** - Accurate transcripts with speaker identification for depositions
- **Customer service** - Analyze support calls for quality and training
- **Content creation** - Generate subtitles and content from videos

## Quick Look

```bash
# Install with one command
curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/setup-opentranscribe.sh | bash

# Start the application
cd opentranscribe
./opentranscribe.sh start

# Access at http://localhost:5173
```

## Architecture Overview

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Frontend   │────▶│   Backend    │────▶│   Workers   │
│   (Svelte)  │     │  (FastAPI)   │     │  (Celery)   │
└─────────────┘     └──────────────┘     └─────────────┘
                            │                    │
                            ▼                    ▼
                    ┌──────────────┐     ┌─────────────┐
                    │  PostgreSQL  │     │  WhisperX   │
                    │    MinIO     │     │  PyAnnote   │
                    │ OpenSearch   │     │     LLM     │
                    └──────────────┘     └─────────────┘
```

## System Requirements

**Minimum:**
- 8GB RAM
- 4 CPU cores
- 50GB disk space
- Docker & Docker Compose

**Recommended:**
- 16GB+ RAM
- 8+ CPU cores
- 100GB+ SSD
- NVIDIA GPU with 8GB+ VRAM (RTX 3070 or better)

## Next Steps

Ready to get started? Follow our [Quick Start Guide](./quick-start.md) to install OpenTranscribe in minutes.

Or explore:
- [Installation Guide](../installation/docker-compose.md) - Detailed installation instructions
- [Hardware Requirements](../installation/hardware-requirements.md) - Hardware recommendations
- [Configuration](../configuration/environment-variables.md) - Customize your setup
- [Authentication](../authentication/overview.md) - Enterprise authentication options
