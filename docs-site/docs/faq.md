---
sidebar_position: 100
title: Frequently Asked Questions
---

# Frequently Asked Questions

Common questions and answers about OpenTranscribe.

## General

### What is OpenTranscribe?

OpenTranscribe is an open-source, self-hosted AI-powered transcription and media analysis platform. It uses state-of-the-art AI models (Whisper via faster-whisper, a PyAnnote-based speaker engine, LLMs), or cloud providers you choose, to transcribe audio/video files, identify speakers, generate summaries, and enable powerful search across your media library.

### Is OpenTranscribe free?

Yes! OpenTranscribe is completely free and open-source under the GNU Affero General Public License v3.0 (AGPL-3.0). There are no subscription fees, usage limits, or hidden costs. You can use it for personal, commercial, or any other purpose, as long as you comply with the AGPL-3.0 license terms.

### What makes OpenTranscribe different from other transcription services?

- **Self-hosted** - Your data stays on your infrastructure unless you choose a cloud provider
- **Open source** - Full transparency and customizability
- **Privacy-first** - No cloud services required; cloud transcription, speaker detection and LLM providers are all optional
- **Advanced features** - Speaker diarization, cross-video speaker matching, AI summarization
- **No usage limits** - Transcribe unlimited content
- **GPU acceleration** - Fast processing with your own hardware
- **Offline capable** - Works in airgapped environments

### Can I use OpenTranscribe commercially?

Yes! The AGPL-3.0 license permits commercial use. However, if you modify OpenTranscribe and offer it as a network service (SaaS), you must make your modified source code available to your users under the same AGPL-3.0 license.

## Installation & Setup

### What are the system requirements?

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

See [Hardware Requirements](./installation/hardware-requirements.md) for detailed recommendations.

### Do I need a GPU?

No, but **highly recommended** for practical use when transcribing on your own server. CPU-only processing is very slow:
- **GPU (RTX 3080)**: 1-hour video → ~5 minutes
- **CPU (8-core)**: 1-hour video → ~60 minutes

Speaker detection on your own server also runs without a GPU, more slowly. On a CPU-only install the installer writes `ENABLE_DIARIZATION=false` to `.env`, but that variable no longer turns speaker detection off: to skip it, choose **Off** under **Settings → Speaker Identification → Speaker Detection**.

If you have no GPU at all, the lite deployment sends transcription and speaker detection to cloud providers you configure; see [Deployment variants](./installation/deployment-variants.md).

### Can I use Apple Silicon (M1/M2/M3)?

Yes, with the `--lite` image — but **not** with MPS acceleration. Docker
Desktop on macOS has no Metal/GPU passthrough into the Linux VM the container runs
in, so every container on Apple Silicon is CPU-only regardless of the host chip's
MPS capability. Lite is cloud-only for transcription and speaker detection: configure a
cloud transcription provider (and pyannote.ai, or a provider that returns speakers, for
speaker detection); only voiceprints are computed on the Mac's CPU. See
[Deployment variants](./installation/deployment-variants.md#which-cloud-providers-return-speakers)
for which providers return speakers. (An MPS code path
exists in the diarization engine, but it is unreachable in every containerized
deployment; see `backend/app/transcription/CLAUDE.md`.)

### What GPUs are supported?

Any NVIDIA GPU with CUDA support:
- **Minimum**: GTX 1060 (6GB VRAM)
- **Recommended**: RTX 3070 or better (8GB+ VRAM)
- **Best**: RTX 4090, A6000, A100

AMD GPUs are not currently supported (ROCm support planned for future).

### How do I get a HuggingFace token?

1. Create free account at [huggingface.co](https://huggingface.co)
2. Go to [Settings → Access Tokens](https://huggingface.co/settings/tokens)
3. Click "New token", select "Read" access
4. Accept the agreement for [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) — this is the only repo OpenTranscribe actually gates on (auto-approved, CC-BY-4.0)
5. Copy token to your `.env` file

See [HuggingFace Setup](./installation/huggingface-setup.md) for detailed instructions.

### Why do I need a HuggingFace token?

The PyAnnote speaker diarization models are "gated" - they require accepting a user agreement before downloading. The token authenticates that you've accepted the terms. Both engines that detect speakers on your server (diar-native and the in-process PyAnnote engine) are built from those models.

**Without a token:** Transcription works, but your server cannot load its speaker detection models, so speaker detection on this server fails with a message about the missing model access. Speaker detection by a cloud provider (pyannote.ai, or a transcription provider that returns speakers) does not need the token.

### Can I run OpenTranscribe offline?

Yes! After initial setup and model downloads, OpenTranscribe works completely offline. Use the offline Docker Compose configuration:

```bash
docker compose -f docker-compose.yml -f docker-compose.offline.yml up -d
```

See [Offline Installation](./installation/offline-installation.md) for details.

## Features & Usage

### What file formats are supported?

**Audio:** MP3, WAV, FLAC, M4A, OGG, AAC
**Video:** MP4, MOV, AVI, MKV, WEBM, FLV

Maximum file size: **4GB**

### What languages are supported for transcription?

**100+ languages** are supported for transcription via Whisper. See the [Whisper documentation](https://github.com/openai/whisper#available-models-and-languages) for the full list.

**v0.2.0 Language Features:**
- **Source Language Selection**: Auto-detect or manually specify the audio language
- **Translation Toggle**: Choose to keep original language or translate to English
- **Word-Level Timestamps**: the built-in model produces word timestamps for every language, directly during transcription (there is no separate alignment step). Cloud providers return word or segment timing depending on the provider

Configure language settings in: **Settings → Transcription → Language**

**Note:** English transcription quality is best. Other languages work but accuracy varies.

### What languages is the UI available in?

**12 languages** (every release ships all 12 in lockstep):
- English (default)
- Arabic (\u0627\u0644\u0639\u0631\u0628\u064a\u0629) \u2014 right-to-left
- German (Deutsch)
- Spanish (Espa\u00f1ol)
- French (Fran\u00e7ais)
- Italian (Italiano)
- Japanese (\u65e5\u672c\u8a9e)
- Korean (\ud55c\uad6d\uc5b4)
- Dutch (Nederlands)
- Portuguese (Portugu\u00eas)
- Russian (\u0420\u0443\u0441\u0441\u043a\u0438\u0439)
- Chinese (\u4e2d\u6587)

Change the UI language in: **Settings → Profile & Security** (Language)

Want to contribute a translation? Submit a PR with a new locale file!

### How accurate is the transcription?

Accuracy depends on:
- **Audio quality** - Clear audio with minimal background noise
- **Model size** - `large-v2` is most accurate
- **Language** - English has best accuracy
- **Speaker accent** - Native accents perform better

**Typical accuracy:** 85-95% word accuracy with good audio quality.

### How does speaker diarization work?

You choose who detects speakers, separately from who transcribes: your own server (the diar-native engine, or the in-process PyAnnote engine, chosen by an administrator), the cloud transcription provider when it returns speakers, or the pyannote.ai cloud service. On your server the engine:
1. **Detects voice activity** - Finds when people are speaking
2. **Extracts voice features** - Creates voice fingerprints
3. **Clusters speakers** - Groups similar voices
4. **Assigns labels** - Tags each segment with SPEAKER_00, SPEAKER_01, etc.

The speaker turns are then matched to the transcript by word timestamps (or by segment, when the model only returns segment timing). Voiceprints are always computed on your server, whoever detected the speakers. You can then manually edit speaker names, and OpenTranscribe will remember those voices across future transcriptions. See [Speaker Diarization](./features/speaker-diarization.md#where-speakers-are-detected).

### Can OpenTranscribe identify speakers by name automatically?

Not automatically, but it can:
1. **Suggest speaker identities** using LLM analysis of conversation content
2. **Match voices across videos** using voice fingerprints
3. **Remember speakers** once you've identified them in one video

See [Speaker Management](./user-guide/speaker-management.md) for details.

### Why are some speaker labels corrected automatically?

OpenTranscribe applies diarization boundary correction to fix speaker mislabeling at turn boundaries. Word-boundary smoothing (default on) collapses short "wrong-speaker islands," and an optional acoustic re-check re-embeds short disputed words and reassigns them by voiceprint. This relabels existing words only -- it never invents speech. See [Boundary Correction](./features/boundary-correction.md).

### Can OpenTranscribe redact PII, profanity, or toxic content?

Yes. Content redaction detects PII, profanity, and toxic/offensive words and masks them with `[CATEGORY]` placeholders at every display and export surface. Masking is a read-time transform -- the full original transcript is always kept in the database. It is a per-user feature, **off by default (opt-in)** (Settings → Privacy & Redaction → Content Redaction), with an admin enforcement floor (Redaction Policy) that can force categories on and mandate censored exports for all users. See [Content Redaction](./features/content-redaction.md).

### How many speakers can it detect?

The default diar-native engine counts speakers automatically. You can also give a speaker range (1 to 50) in **Settings → Speaker Identification → Speaker Detection**, or per file when you upload or reprocess; the system default range is `MIN_SPEAKERS`/`MAX_SPEAKERS` in `.env` (1-20).

The range is a hint: pyannote.ai and the in-process PyAnnote engine apply it, cloud transcription providers apply what they support, and the default diar-native engine ignores it. Accuracy decreases with very large groups (30+ speakers).

### What LLM providers are supported?

OpenTranscribe supports multiple LLM providers for AI summarization:

**Cloud Providers:**
- **OpenAI** (GPT-4o, GPT-4o-mini, GPT-4-turbo)
- **Anthropic Claude** (Claude 3.5 Sonnet, Claude 3 Opus)
- **OpenRouter** (access to 100+ models)

**Self-Hosted:**
- **vLLM** (fast self-hosted inference)
- **Ollama** (local models)
- **Custom OpenAI-compatible APIs**

See [LLM Integration](./features/llm-integration.md) for setup instructions.

### Do I need an LLM for transcription?

No! LLMs are **optional** and only used for:
- AI-powered summarization
- Topic extraction
- Enhanced speaker name suggestions

Transcription and speaker diarization work without any LLM.

### Can I generate AI summaries in languages other than English?

Yes! AI summaries can be generated in **12 languages** (`LLM_OUTPUT_LANGUAGES`,
`backend/app/core/constants.py`):
- English, Spanish, French, German
- Italian, Portuguese, Dutch, Russian
- Chinese, Japanese, Korean, Arabic

Configure in: **Settings → Transcription → Language** (*Language for AI features*)

The LLM will generate the summary in your chosen language regardless of the original audio language.

### Can I use OpenTranscribe without any cloud services?

Yes! You can run completely offline and local:
- Transcription: The built-in Whisper models (choose the built-in model under **Settings → Transcription**)
- Speakers: Detected on your server (choose **Automatic** with the built-in model, or **Always on this server's GPU**)
- Voiceprints: Always computed on your server
- LLM (optional): Local vLLM or Ollama

This needs the full deployment: a lite deployment cannot transcribe or detect speakers on the server.

No data leaves your infrastructure.

### How long does processing take?

Depends on hardware and content length:

| Content Length | GPU (RTX 3080) | CPU (8-core) |
|----------------|----------------|--------------|
| 5 minutes      | ~30 seconds    | ~5 minutes   |
| 30 minutes     | ~3 minutes     | ~30 minutes  |
| 1 hour         | ~5 minutes     | ~60 minutes  |
| 3 hours        | ~15 minutes    | ~3 hours     |

**Processing speed:** ~70x realtime with GPU and `large-v2` model.

### Can I process multiple files at once?

Yes! OpenTranscribe uses Celery workers to process multiple files in parallel:
- **Default:** 1-2 files at once (depending on GPU memory)
- **Multi-GPU:** 4+ files at once with [Multi-GPU Scaling](./configuration/multi-gpu-scaling.md)

### How much disk space do I need?

**For OpenTranscribe:**
- Docker images: ~5GB
- AI models: several GB (depends on `WHISPER_MODEL`; see [HuggingFace setup](./installation/huggingface-setup.md))
- Database: ~100MB (grows with transcriptions)

**For media files:**
- Plan **~10% of original file size** for processed data
- Example: 100 hours of audio (10GB) → ~11GB total storage needed

### Can I download videos from YouTube and other platforms?

Yes! OpenTranscribe supports **1800+ platforms** via yt-dlp integration:
- **Best supported**: YouTube (including playlists), Dailymotion, Twitter/X
- **Limited support**: Vimeo, Instagram, Facebook, TikTok (may require authentication)
- Automatically downloads and transcribes
- User-friendly error messages for authentication-required videos

### How do I view system statistics?

System statistics (CPU, memory, disk, GPU usage) are visible to all authenticated users in the **navbar** - look for the system stats icon. This shows real-time resource usage of your OpenTranscribe server.

### How does pagination work for large transcripts?

For transcripts with thousands of segments (3+ hour recordings), OpenTranscribe automatically paginates the display to prevent browser slowdown. Segments load progressively as you scroll through the transcript.

### Can I share transcriptions with specific groups of users?

Yes! OpenTranscribe supports **User Groups** and **Collection Sharing**. Admins can create groups of users and then share entire collections with those groups. This makes it easy to organize team access — for example, share a "Legal Team" collection with a "Legal" user group so all members can access the same transcriptions.

### Can I use cloud speech-to-text instead of local Whisper?

Yes! OpenTranscribe supports **multi-provider cloud ASR** in addition to the built-in Whisper models. Speaker detection is a separate choice: most cloud providers also return speakers, and with **Automatic** their labels are used. For a model that returns text only, your server detects the speakers (full deployment), or you choose pyannote.ai. A model that returns no timestamps at all cannot be combined with speaker detection.

A lite deployment needs no GPU and is cloud-only: transcription and speaker detection both use cloud providers you configure, while voiceprints are still computed on your server.

When you use a cloud provider, your audio is sent to that vendor and processed under its terms; the same applies to pyannote.ai. See [Deployment variants](./installation/deployment-variants.md#which-cloud-providers-return-speakers) for which providers return speakers and [which combinations are valid](./installation/deployment-variants.md#which-combinations-are-valid).

### Can I reprocess just the speaker diarization without re-transcribing?

Yes! The **selective reprocessing** feature lets you pick specific stages to re-run. Instead of reprocessing an entire file from scratch, you can choose to redo only speaker diarization (**Re-diarize Only (v4)**), only summarization, or any other individual stage — saving significant time when you just need to tweak one part of the pipeline.

Re-diarizing uses your **current** speaker-detection choice: pyannote.ai if you chose it, otherwise your server's engine (a cloud provider's own speakers cannot be fetched again without transcribing again). On a lite deployment a re-run that would need your server's engine is refused. The reprocess dialog currently offers this stage only while your active transcription provider is the built-in model.

### How does cross-video speaker matching work?

OpenTranscribe uses **GPU-accelerated speaker pre-clustering** to group similar voices across all your transcriptions. Voice embeddings (voiceprints) are computed on your server after speakers are detected, whoever detected them, and then clustered so the system can suggest matches when the same speaker appears in different recordings. This powers the cross-video speaker identification workflow.

### Can OpenTranscribe automatically tag my transcriptions?

Yes! The **AI auto-labeling** feature uses your configured LLM to analyze transcription content and suggest relevant topics. These suggestions are automatically applied as tags and can be organized into collections, making it easy to categorize and find related content without manual effort.

### Can files be automatically deleted after a certain period?

Yes! Admins can configure **file retention policies** to automatically clean up old files after a specified number of days. This is useful for compliance requirements or simply managing disk space. Retention policies are configured in the admin settings and apply system-wide.

### How can I improve AI summary quality for my organization?

Configure **Organization Context** in **Settings → Organization Context**. This lets you provide background information about your organization — such as its mission, common terminology, and typical meeting types. The LLM uses this context when generating summaries, resulting in more relevant and accurate output tailored to your specific domain.

### Can I switch between grid and list view?

Yes! The **gallery view** supports both grid and list layouts. You can toggle between them using the view switcher in the toolbar. Your preference is remembered between sessions. The gallery also includes pagination for efficiently browsing large libraries.

### Does OpenTranscribe detect speaker gender?

Yes! OpenTranscribe includes **automatic gender classification** for detected speakers. Gender predictions are made from each speaker's audio by a voice-attribute classifier (a Wav2Vec2-based model, or the diar-native engine in the same pass when it detected the speakers) and displayed alongside speaker information. This helps validate speaker clusters and provides additional metadata for organizing and identifying speakers.

### Can I customize export format?

Yes! The **TXT export** feature includes configurable options that let you control what's included in the exported file — such as timestamps, speaker labels, and formatting preferences. Your export settings are saved as persistent preferences so you don't have to reconfigure them each time.

## Performance & Optimization

### How can I speed up processing?

1. **Use a GPU** - Fastest option (required for practical use)
2. **Use larger batch size** - If you have GPU memory (edit `BATCH_SIZE` in `.env`)
3. **Use smaller model** - `medium` or `base` (faster but less accurate)
4. **Multi-GPU scaling** - Process 4+ files in parallel
5. **SSD storage** - Faster disk I/O

### My GPU is running out of memory. What can I do?

1. **Reduce batch size**: `BATCH_SIZE=8` (or lower)
2. **Use smaller model**: `WHISPER_MODEL=medium` or `base`
3. **Use quantization**: `COMPUTE_TYPE=int8`
4. **Close other GPU applications**
5. **Upgrade GPU** (if feasible)

### Can I use multiple GPUs?

Yes! OpenTranscribe supports multi-GPU scaling for high-throughput processing:

```bash
# Configure in .env
GPU_SCALE_ENABLED=true
GPU_SCALE_DEVICE_ID=2  # Which GPU to use
GPU_SCALE_WORKERS=4    # Number of parallel workers

# Start with GPU scaling
./opentr.sh start dev --gpu-scale
```

See [Multi-GPU Scaling](./configuration/multi-gpu-scaling.md) for details.

## Troubleshooting

### OpenTranscribe won't start

```bash
# Check Docker is running
docker ps

# Check logs
docker compose logs

# Common issues:
# - Port conflicts (5173, 8080, etc. already in use)
# - Insufficient memory
# - Docker Compose not installed
```

See [Troubleshooting Guide](./installation/troubleshooting.md).

### "Permission denied" error for model cache

```bash
# Fix permissions
./scripts/fix-model-permissions.sh

# Or manually
sudo chown -R 1000:1000 ./models
```

This happens because Docker creates directories as root, but containers run as non-root user (UID 1000) for security.

### Transcription fails with "CUDA out of memory"

Reduce GPU memory usage:
```bash
# Edit .env
BATCH_SIZE=8        # Reduce from 16
COMPUTE_TYPE=int8   # Use 8-bit quantization

# Or use smaller model
WHISPER_MODEL=medium
```

### Speaker diarization not working

1. **Check your choice** under **Settings → Speaker Identification → Speaker Detection** (it may be **Off**), and check whether the file shows a refusal message (for example a model with no timestamps, or pyannote.ai chosen without a key)
2. For detection on your server: **check the HuggingFace token** is set in `.env`
3. **Accept the model agreement** for [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
4. **Check logs** for download errors: `docker compose logs celery-worker`
5. **Re-download models** if corrupted

On a lite deployment there is no speaker detection on the server; choose a cloud model that returns speakers or pyannote.ai.

### Poor transcription quality

- **Use better audio** - Clear, well-recorded audio
- **Use larger model** - `large-v2` is most accurate
- **Reduce background noise** - Use noise cancellation
- **Check language setting** - Ensure correct language selected

### YouTube downloads failing

- **Check yt-dlp version** - May need updating
- **Check video availability** - Some videos are region-locked
- **Check age restrictions** - Age-restricted videos may not work
- **Try direct file upload** - Download manually first

## Security & Privacy

### Is my data secure?

Yes. By default all processing happens on your server:
- Media files stored in MinIO (local S3-compatible storage)
- Transcripts stored in PostgreSQL (local database)
- AI models run locally with the built-in transcription model and speaker detection on your server
- Voiceprints are always computed on your server
- Optional LLM calls can use self-hosted providers

If you choose a cloud transcription provider, pyannote.ai, or a cloud LLM, the audio (or, for an LLM, transcript text) is sent to that vendor.

### Are transcripts encrypted?

It depends on the store:

- **Media files (MinIO)**: encrypted at rest by default — the installer enables AES-256-GCM
  server-side encryption automatically.
- **Credentials and secrets** (API keys, TOTP secrets): encrypted at the application layer
  with AES-256-GCM before they reach the database.
- **Transcript text (PostgreSQL and OpenSearch)**: not encrypted at the application layer —
  search and AI features require the backend to read it. PostgreSQL has no built-in at-rest
  encryption, so use **full-disk encryption** (LUKS/dm-crypt, FileVault, BitLocker) or place
  the data volumes on an encrypted filesystem.
- **Backups**: use `./opentranscribe.sh backup --encrypt` for GPG AES-256 encrypted dumps.

See [Security Hardening](operations/security-hardening.md) for the complete picture.

### Who can access my transcriptions?

Only users you create. OpenTranscribe has role-based access control:
- **Admin users** - Full access to all data
- **Regular users** - Access only to their own data

### Can I use OpenTranscribe for sensitive content?

Yes! OpenTranscribe is designed for privacy-sensitive use cases:
- Legal depositions
- Medical consultations
- Business strategy meetings
- Personal recordings

With the built-in transcription model, speaker detection on your server and a local (or no) LLM, nothing is sent to cloud services. Cloud transcription, pyannote.ai speaker detection and cloud LLMs are each an explicit choice that sends audio or text to that vendor.

## Authentication

### What authentication methods are supported?

OpenTranscribe supports multiple authentication methods:

- **Local** - Username/password stored in PostgreSQL (default)
- **LDAP/Active Directory** - Authenticate against existing AD infrastructure
- **OIDC/Keycloak** - Single Sign-On via OpenID Connect
- **PKI/X.509** - Certificate-based authentication (CAC/PIV card support)

Multiple methods can be enabled simultaneously for hybrid deployments.

See [Authentication Overview](./authentication/overview.md) for details.

### Does OpenTranscribe support MFA?

Yes! OpenTranscribe supports TOTP-based multi-factor authentication:
- Works with Google Authenticator, Authy, and other TOTP apps
- QR code setup for easy configuration
- Backup codes for account recovery
- Per-user enablement

Enable MFA in your `.env` file:
```bash
MFA_ENABLED=true
MFA_ISSUER_NAME=OpenTranscribe
```

### Can I integrate with Active Directory?

Yes! OpenTranscribe supports LDAP/Active Directory authentication:
- Users authenticate with AD credentials
- Accounts auto-created on first login
- Admin roles configurable via `LDAP_ADMIN_USERS`
- Hybrid mode supports both local and AD users

See the [LDAP Setup Guide](./authentication/ldap) for configuration.

### What password policies are available?

OpenTranscribe includes FedRAMP-compliant password policies:
- **Minimum length** (default: 12 characters)
- **Complexity requirements** (uppercase, lowercase, digits, special characters)
- **Password history** (prevent reuse of last 24 passwords)
- **Password expiration** (default: 60 days)
- **Common pattern detection** (blocks weak passwords)

Configure in `.env`:
```bash
PASSWORD_POLICY_ENABLED=true
PASSWORD_MIN_LENGTH=12
PASSWORD_HISTORY_COUNT=24
PASSWORD_MAX_AGE_DAYS=60
```

### Is there account lockout protection?

Yes! OpenTranscribe implements NIST AC-7 compliant account lockout:
- Lock account after failed attempts (default: 5)
- Progressive lockout durations (15 min, 30 min, 60 min, 24 hours)
- Admin unlock capability
- Automatic unlock after lockout expires

Configure in `.env`:
```bash
ACCOUNT_LOCKOUT_ENABLED=true
ACCOUNT_LOCKOUT_THRESHOLD=5
ACCOUNT_LOCKOUT_DURATION_MINUTES=15
ACCOUNT_LOCKOUT_PROGRESSIVE=true
```

### Does OpenTranscribe support SSO?

Yes — via **any conforming OpenID Connect provider**, discovered from its
`.well-known/openid-configuration`: Keycloak, Authentik, Authelia, Okta, Entra ID, Auth0,
Zitadel, and so on.

- Authorization-code flow with PKCE, ID-token-only validation
- Group and role synchronization through a configurable roles claim
- [Group mapping](./authentication/groups) from directory groups to in-app groups and roles
- RP-initiated (federated) logout where the provider publishes an end-session endpoint

See the [OIDC Setup Guide](./authentication/oidc) for configuration. Existing `KEYCLOAK_*`
environment variables keep working permanently.

### Can I use CAC/PIV cards for authentication?

Yes! OpenTranscribe supports PKI/X.509 certificate authentication:
- Client certificate authentication via mutual TLS
- CAC/PIV smart card support
- No passwords required
- Admin designation via certificate DN

See the [PKI Setup Guide](./authentication/pki) for configuration.

### How is audit logging handled?

OpenTranscribe includes FedRAMP-compliant audit logging:
- Structured JSON or CEF format
- All authentication events logged
- Optional OpenSearch integration for analysis
- Tracks logins, logouts, failed attempts, MFA events, admin actions

Configure in `.env`:
```bash
AUDIT_LOG_ENABLED=true
AUDIT_LOG_FORMAT=json
AUDIT_LOG_TO_OPENSEARCH=false
```

## Development & Contribution

### How can I contribute?

We welcome contributions! See [Contributing Guide](./developer-guide/contributing.md) for:
- Code contributions
- Documentation improvements
- Bug reports
- Feature requests
- Translations

### Can I customize OpenTranscribe?

Yes! OpenTranscribe is open source (AGPL-3.0 License):
- Modify the code
- Add custom features
- Integrate with other services
- White-label for your organization

See [Developer Guide](./developer-guide/architecture.md) to get started.

**Important:** If you modify OpenTranscribe and offer it as a network service (SaaS), you must make your modified source code available to your users under the AGPL-3.0 license.

### How is OpenTranscribe built?

**Frontend:** Svelte + TypeScript + Vite
**Backend:** Python + FastAPI + SQLAlchemy
**AI:** faster-whisper + diar-native / PyAnnote + LangChain
**Infrastructure:** Docker + PostgreSQL + Redis + MinIO + OpenSearch

See [Architecture](./developer-guide/architecture.md) for details.

### What AI models does it use?

- **Whisper (faster-whisper)** - Speech recognition, with word timestamps produced during transcription (no separate alignment model)
- **PyAnnote community-1** - Speaker diarization, run by the diar-native engine or in-process PyAnnote
- **WeSpeaker** - Voiceprints for cross-file speaker matching
- **Wav2Vec2 gender/age classifier** - Optional speaker attribute detection
- **Sentence Transformers** - Semantic search embeddings
- **LLMs** (optional) - Summarization and analysis

## Licensing & Legal

### What is the license?

OpenTranscribe is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)** - a strong copyleft open-source license. You can:
- Use commercially
- Modify the code
- Distribute
- Private use

**Key requirement:** If you modify OpenTranscribe and offer it as a network service (SaaS), you must provide your users access to the modified source code under the same AGPL-3.0 license. This ensures the open source community benefits from improvements.

### Can I sell OpenTranscribe?

Yes! The AGPL-3.0 license permits commercial use, including:
- Offering it as a paid service
- Selling access to your installation
- Using it for commercial transcription work

**Important:** If you modify the code and run it as a network service, you must make your modifications available under AGPL-3.0.

### Do I need to credit OpenTranscribe?

Credit is appreciated but not required. The AGPL-3.0 license requires that you:
1. Include the original license notice in copies of the software
2. Make source code available if you offer it as a network service
3. Preserve copyright notices

### What if I don't want to share my modifications?

If you modify OpenTranscribe for internal use only (not as a network service), you don't need to share your changes. The AGPL-3.0 only requires source disclosure when you offer the software as a service to others over a network.

### What about the AI models' licenses?

- **WhisperX**: Apache 2.0 License
- **PyAnnote**: MIT License
- **Wav2Vec2 gender/age classifier**: Apache 2.0 License

All are permissive licenses compatible with commercial use.

## Still Have Questions?

- **GitHub Discussions**: [github.com/attevon-llc/OpenTranscribe/discussions](https://github.com/attevon-llc/OpenTranscribe/discussions)
- **GitHub Issues**: [github.com/attevon-llc/OpenTranscribe/issues](https://github.com/attevon-llc/OpenTranscribe/issues)
- **Documentation**: Browse the rest of the docs!
