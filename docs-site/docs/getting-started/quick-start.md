---
sidebar_position: 2
title: Quick Start
---

# Quick Start Guide

Get OpenTranscribe up and running in less than 5 minutes with our one-line installer.

## One-Line Installation

Run this single command on any platform (Linux, macOS, Windows WSL2):

```bash
curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

The installer will:
✅ Detect your hardware automatically (NVIDIA GPU, Apple Silicon, CPU)
✅ Configure optimal settings for your platform
✅ Download Docker images from Docker Hub
✅ Generate secure configuration
✅ Set up management scripts

## Prerequisites

Before running the installer, ensure you have:

1. **Docker & Docker Compose** installed and running
2. **Internet connection** for downloading images and models
3. **8GB+ RAM** (16GB+ recommended)

:::info HuggingFace Token Required
For speaker diarization to work, you'll need a **free HuggingFace token**. The installer will prompt you for it. See [HuggingFace Setup](../installation/huggingface-setup.md) for details.
:::

## Installation Steps

### Step 1: Run the Installer

```bash
curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

### Step 2: Follow the Prompts

The installer will ask for:

1. **HuggingFace Token** (optional but recommended)
   - Used for speaker diarization models
   - Get a free token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)

2. **Whisper Model Size** (default: `large-v3-turbo` — auto-detected based on hardware)
   - `large-v3-turbo` - 6x faster, excellent accuracy (default, NVIDIA GPU recommended)
   - `large-v3` - Best accuracy, required for translation to English
   - `medium` - Good balance (8GB+ GPU or Apple Silicon)
   - `base` - Fast (CPU-only systems)

### Step 3: Start OpenTranscribe

```bash
cd opentranscribe
./opentranscribe.sh start
```

### Step 4: Access the Application

Open your browser and navigate to:

- **Web Interface**: http://localhost:5173
- **API Documentation**: http://localhost:5174/docs
- **Task Monitor** (Flower): http://localhost:5175/flower

## First Transcription

### 1. Create an Account

When you first access OpenTranscribe, you'll see the registration page:

1. Enter your email and password
2. Click "Sign Up"
3. You're automatically logged in

### Guided First-Run Setup Wizard

The very first account created on a fresh install becomes the bootstrap `super_admin`, and that
account sees a one-time guided setup wizard on first login instead of the empty gallery. Rather
than leaving you to discover dozens of auth-related environment variables and several admin
tabs on your own, it surfaces the three things a first-time operator actually needs:

- **Change your password** from whatever was used during account creation
- **SSO / LDAP setup**, if you're connecting an identity provider
- **Security defaults** — MFA-required, login banner, and approval-on-signup

The wizard presents OpenTranscribe's real settings screens rather than a separate duplicate
flow, so anything you configure here is the same configuration you'd reach from **Settings →
Authentication** later. It's shown once; closing or completing it won't bring it back.

### 2. Upload a Media File

1. Click the **"Upload Files"** button in the navbar
2. Drag and drop a file or click to browse
3. Supported formats: MP3, WAV, MP4, MOV, etc.
4. Files up to 4GB are supported

### 3. Monitor Processing

Watch the progress in real-time:

- **Upload progress** shows in the floating upload manager
- **Processing stages** display 13 detailed steps
- **Notifications** appear when transcription completes

### 4. View Your Transcript

Once processing completes:

1. Click on the file in your library
2. View the interactive transcript with speaker labels
3. Click on any word to jump to that moment in the audio
4. Use the waveform visualization for precise navigation

## What's Next?

Now that you have OpenTranscribe running, explore these features:

### Configure AI Features

Set up LLM integration for AI-powered summarization:

- Go to **User Settings** → **LLM Configuration**
- Choose a provider (OpenAI, Claude, vLLM, Ollama)
- Enter your API key
- Test the connection

See [LLM Integration](../features/llm-integration.md) for details.

### Enterprise Authentication (Optional)

For enterprise deployments, OpenTranscribe supports:

- **LDAP/Active Directory** - Integrate with existing AD
- **Keycloak/OIDC** - Single Sign-On
- **PKI/X.509** - Certificate-based authentication
- **MFA** - TOTP-based multi-factor authentication

See [Authentication Overview](../authentication/overview.md) for setup guides.

### Manage Speakers

OpenTranscribe automatically detects speakers, but you can improve accuracy:

- Edit speaker names in any transcript
- Create global speaker profiles
- Let AI suggest speaker identities across videos

See [Speaker Management](../user-guide/speaker-management.md) for more.

### Organize with Collections

Group related media files:

- Create collections for projects, topics, or events
- Add files to multiple collections
- Filter your library by collection

See [Collections](../user-guide/collections.md) for details.

### Advanced Search

Find content across all your transcriptions:

- **Keyword search** - Find exact words or phrases
- **Semantic search** - Find similar concepts
- **Filters** - By speaker, date, duration, tags
- **Speaker analytics** - See who speaks most across your library

See [Search & Filters](../user-guide/search-and-filters.md) for tips.

## Common Commands

### Management Commands

```bash
# Start OpenTranscribe
./opentranscribe.sh start

# Stop all services
./opentranscribe.sh stop

# View logs
./opentranscribe.sh logs

# Check status
./opentranscribe.sh status

# Restart services
./opentranscribe.sh restart

# Access database
./opentranscribe.sh shell postgres
```

### Updating OpenTranscribe

```bash
# Check current version and available updates
./opentranscribe.sh version

# Update containers only (quick)
./opentranscribe.sh update

# Full update including config files (recommended)
./opentranscribe.sh update-full
```

## Troubleshooting

### Services Won't Start

```bash
# Check Docker is running
docker ps

# Check logs for errors
./opentranscribe.sh logs backend
```

### GPU Not Detected

```bash
# Check GPU availability
nvidia-smi

# Test GPU in Docker
docker run --rm --gpus all nvidia/cuda:11.8-base-ubuntu22.04 nvidia-smi
```

### Permission Errors

```bash
# Fix model cache permissions
./scripts/fix-model-permissions.sh
```

### Slow Transcription

- Ensure GPU acceleration is enabled (`USE_GPU=true` in `.env`)
- Reduce model size if running out of memory
- Check GPU memory usage with `nvidia-smi`

See [Troubleshooting Guide](../installation/troubleshooting.md) for more solutions.

## Getting Help

If you encounter issues:

1. Check the [FAQ](../faq.md)
2. Search [GitHub Issues](https://github.com/attevon-llc/OpenTranscribe/issues)
3. Ask in [GitHub Discussions](https://github.com/attevon-llc/OpenTranscribe/discussions)
4. Read the [Installation Guide](../installation/docker-compose.md) for detailed setup

## Next Steps

- **User Guide**: Learn about [all features](../user-guide/uploading-files.md)
- **Configuration**: Customize [environment variables](../configuration/environment-variables.md)
- **Development**: Learn to [contribute](../developer-guide/contributing.md)

Welcome to OpenTranscribe!
