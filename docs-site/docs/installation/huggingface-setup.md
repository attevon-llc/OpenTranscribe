---
sidebar_position: 4
---

# HuggingFace Token Setup

Speaker diarization in OpenTranscribe requires access to gated PyAnnote models on HuggingFace. This page guides you through obtaining a free token and accepting the necessary model agreements.

:::warning[Critical Requirement]
**Speaker diarization will NOT work** without a valid HuggingFace token and acceptance of the `pyannote/speaker-diarization-community-1` model agreement. Transcription will still work, but speakers will not be identified.
:::

## Why is HuggingFace Required?

OpenTranscribe uses PyAnnote.audio for speaker diarization (identifying "who spoke when"). PyAnnote's pre-trained models are hosted on HuggingFace as "gated" repositories, meaning you must:

1. Create a free HuggingFace account
2. Accept the model license agreements
3. Use an access token to download the models

This is a one-time setup process. Once configured, models are cached locally and don't require internet access.

:::note[This same token also provisions the native diarization engine]
As of v0.5.0, `local` diarization defaults to a **native `diar-native` sidecar**
rather than the in-process PyAnnote pipeline described on this page (see
[Speaker Diarization](../features/speaker-diarization.md#native-diarization-engine-new-in-v050)).
It reuses the identical `HUGGINGFACE_TOKEN` you configure here to export its own ONNX/PLDA
model set automatically on the backend's first startup — there is nothing extra to set up. If
that export fails for any reason (missing token, gate not accepted), diarization falls back to
the PyAnnote path this page walks through, so following the steps below keeps you covered
either way.
:::

## Step 1: Create HuggingFace Account

If you don't already have a HuggingFace account:

1. Visit [https://huggingface.co/join](https://huggingface.co/join)
2. Sign up with email or GitHub/Google account
3. Verify your email address

**Time required**: 2 minutes

## Step 2: Generate Access Token

1. Go to [https://huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
2. Click **"New token"**
3. Configure the token:
   - **Name**: `OpenTranscribe` (or any descriptive name)
   - **Role**: Select **"Read"** (default)
   - **Description**: Optional
4. Click **"Generate token"**
5. **IMPORTANT**: Copy the token and save it securely (you won't see it again)

Example token format: `hf_` followed by random characters

:::tip[Token Storage]
Save your token in a password manager or secure note. You'll need it during OpenTranscribe setup. Tokens don't expire unless you delete them.
:::

**Time required**: 2 minutes

## Step 3: Accept the Model Agreement

Speaker diarization needs access to **one** gated model:
`pyannote/speaker-diarization-community-1`. It's CC-BY-4.0 and **auto-approved** — there's no
waiting list or human review, but you still have to click through the agreement once per
account.

### pyannote/speaker-diarization-community-1

1. Visit [https://huggingface.co/pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
2. Scroll to the model card
3. Click **"Agree and access repository"**
4. ✅ You should see "You have been granted access to this model"

:::caution[The token and the acceptance must be the SAME HuggingFace account]
The gate is checked **per account**, not just per repo. If `HUGGINGFACE_TOKEN` was generated
under a different account than the one that clicked "Agree and access repository" above, every
request fails with HTTP 403 — identically to a token that never accepted anything. This is the
single most common way this setup step goes wrong. If diarization still fails after you're sure
you accepted the agreement, check which account actually issued the token.
:::

:::note[What about segmentation-3.0 and speaker-diarization-3.1?]
Older docs and forum posts reference `pyannote/segmentation-3.0` and
`pyannote/speaker-diarization-3.1` — that was the model pair OpenTranscribe used before moving
to `speaker-diarization-community-1`. `community-1` bundles its own segmentation, embedding, and
PLDA weights in one self-contained repo, so accepting the old pair grants nothing for it, and
the native `diar-native` sidecar's export deliberately never touches them (their weights differ
from community-1's own). The in-process PyAnnote engine — the failover used when the sidecar is
unavailable — does keep `speaker-diarization-3.1` as an internal last-resort fallback if loading
`community-1` itself fails for a non-licensing reason. Accepting the old pair is optional, only
helps that inner fallback, and is never a substitute for accepting `community-1`.
:::

**Time required**: 2 minutes

## Step 4: Configure OpenTranscribe

### Quick Install Method

If using the one-liner installer:

```bash
curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

The installer will prompt you:

```
Enter your HuggingFace token (or press Enter to skip): hf_your_token_here
```

Paste your token and press Enter. The installer will:
- Validate the token
- Check model access permissions
- Download and cache models (~500MB)
- Configure the `.env` file automatically

### Manual Install Method

If installing from source:

1. Copy the environment template:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and add your token:
   ```bash
   # Required for speaker diarization
   HUGGINGFACE_TOKEN=hf_your_token_here
   ```

3. Start OpenTranscribe:
   ```bash
   ./opentr.sh start dev
   ```

Models will download automatically on first use (~10-30 minutes).

## Verification

### Method 1: Check Model Cache

After first transcription with speaker diarization:

```bash
# Check if the diarization pipeline was downloaded
ls -lh models/huggingface/hub/ | grep pyannote

# You should see (this is the only one required):
# models--pyannote--speaker-diarization-community-1
```

If the in-process PyAnnote engine's internal last-resort fallback ever ran, you'll also see
`models--pyannote--speaker-diarization-3.1` and `models--pyannote--segmentation-3.0` here —
that isn't an error, just evidence the fallback fired at some point.

### Method 2: Test Transcription

1. Upload a test file with multiple speakers
2. Enable speaker diarization
3. Process the file
4. Check for speaker labels (Speaker 1, Speaker 2, etc.)

If diarization works, you'll see:
- ✅ Speaker segments identified
- ✅ Different speakers color-coded
- ✅ Speaker analytics in dashboard

### Method 3: Check Container Logs

```bash
./opentr.sh logs celery-worker | grep -i pyannote
```

Success indicators (`./opentr.sh logs backend` for the native diarizer,
`./opentr.sh logs celery-worker` for the in-process PyAnnote fallback):
```
✅ "diar-native models already provisioned" / "diar-native models exported to ..."
✅ "Loading PyAnnote v4 pipeline: pyannote/speaker-diarization-community-1"
✅ "Loaded PyAnnote v4 model: pyannote/speaker-diarization-community-1"
```

Error indicators:
```
"diar-native provisioning failed (exit 5)"   — TOKEN_DENIED, native engine
"Failed to load v4 model ... Trying fallback" — in-process engine falling back to 3.1
HTTP 403 / PermissionError mentioning speaker-diarization-community-1
```

## Troubleshooting

### Error: "Cannot access gated repository" / HTTP 403 / exit 5 (TOKEN_DENIED)

**Cause**: The `speaker-diarization-community-1` agreement was not accepted by the account that
issued `HUGGINGFACE_TOKEN`, or the token itself is invalid.

**What this looks like now**: the native diarizer's model export runs at backend startup and
exits with code 5 (`TOKEN_DENIED`) on a rejected token. That failure is never fatal to the
stack — `/readyz` stays 503 for the native diarizer and every diarization job falls back to the
in-process PyAnnote engine, which loads the identical gated repo and hits the identical 403. The
practical result is a healthy-looking stack with **no working diarizer**, not a crash. Look for
`diar-native provisioning failed (exit 5)` in the backend's startup log.

**Solution**:
1. Confirm the agreement was accepted (see Step 3) — **by the same HuggingFace account** that
   generated `HUGGINGFACE_TOKEN`. A token from one account and an "Agree and access repository"
   click from another fails identically to a token that never accepted anything.
2. Check `HUGGINGFACE_TOKEN` is correct in `.env`
3. Regenerate the token if needed
4. Restart OpenTranscribe: `./opentr.sh restart-backend` (re-runs provisioning) or
   `./opentr.sh restart-all`

### Error: "Invalid HuggingFace token"

**Cause**: Token format incorrect or expired

**Solution**:
1. Verify token starts with `hf_`
2. Check for extra spaces or quotes in `.env`
3. Regenerate token from HuggingFace settings
4. Update `.env` and restart

### Models Download on Every Restart

**Cause**: Model cache not persisting

**Solution**:
1. Check `MODEL_CACHE_DIR` in `.env` (default: `./models`)
2. Verify directory permissions:
   ```bash
   ls -la models/
   # Should be owned by user running Docker
   ```
3. Fix permissions:
   ```bash
   ./scripts/fix-model-permissions.sh
   ```

### Slow Model Download

**Cause**: Large model files (~500MB total)

**Solution**:
- Be patient on first setup (10-30 minutes)
- Models are cached permanently after first download
- Use wired connection for faster downloads
- Check internet speed: [https://fast.com](https://fast.com)

### Speaker Diarization Not Working

**Checklist**:
- [ ] HuggingFace token configured in `.env`
- [ ] `speaker-diarization-community-1` agreement accepted — by the token's own account
- [ ] Models downloaded successfully (check logs)
- [ ] Speaker diarization enabled in UI settings
- [ ] Audio file has multiple speakers
- [ ] MIN_SPEAKERS and MAX_SPEAKERS configured correctly

## Security Considerations

### Token Security

Your HuggingFace token is **sensitive information**:

- ✅ **DO**: Store in `.env` file (git-ignored)
- ✅ **DO**: Use read-only token permissions
- ✅ **DO**: Regenerate if compromised
- **DON'T**: Commit to version control
- **DON'T**: Share publicly
- **DON'T**: Use write permissions (unnecessary)

### Revoking Access

If your token is compromised:

1. Go to [https://huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
2. Click "Revoke" next to the compromised token
3. Generate a new token
4. Update `.env` with new token
5. Restart OpenTranscribe

## Model Caching

### Storage Location

Models are cached at:
```bash
${MODEL_CACHE_DIR}/huggingface/hub/models--pyannote--speaker-diarization-community-1/
```

Default: `./models/huggingface/hub/models--pyannote--speaker-diarization-community-1/`. This is
the standard `huggingface_hub` cache layout pyannote.audio 4.x uses for every gated model, not a
PyAnnote-specific directory — `./models/huggingface/hub/` also holds every other HuggingFace
model the app downloads.

### Disk Space

`speaker-diarization-community-1` is a single self-contained repo — segmentation, embedding, and
PLDA weights bundled together — **~500MB total**.

Plus, downloaded by `scripts/download-models.sh`:
- WhisperX transcription models — the largest single item, and the one that
  varies most with `WHISPER_MODEL`
- Wav2Vec2 alignment and the gender classifier
- NLTK tokenizers and sentence-transformers embeddings
- Chat reranker (cross-encoder, used by RAG chat)
- OpenSearch neural-search models
- Content-redaction models (PII / toxicity)

**Total: several GB.** It is not a fixed number — it depends on `WHISPER_MODEL`
and on whether you set `DOWNLOAD_ALL_OPENSEARCH_MODELS=true`, so budget disk
generously rather than to a specific figure.

### Offline Use

Once models are downloaded:
- ✅ No internet required for transcription
- ✅ Models cached permanently
- ✅ Works in airgapped environments
- Initial download requires internet

## Alternative: Offline Installation

For airgapped/offline environments:

1. Download models on internet-connected machine:
   ```bash
   # Set token and download the pipeline the app actually loads
   export HUGGINGFACE_TOKEN=hf_your_token_here
   python3 -c "from pyannote.audio import Pipeline; Pipeline.from_pretrained('pyannote/speaker-diarization-community-1')"
   ```

2. Copy model cache to offline machine:
   ```bash
   # On internet machine
   tar -czf pyannote-models.tar.gz ~/.cache/huggingface/hub/models--pyannote--speaker-diarization-community-1/

   # On offline machine
   tar -xzf pyannote-models.tar.gz -C /path/to/opentranscribe/models/huggingface/hub/
   ```

3. Configure `.env` on offline machine:
   ```bash
   HUGGINGFACE_TOKEN=hf_your_token_here  # Still required
   MODEL_CACHE_DIR=./models
   ```

See [Offline Installation](./offline-installation.md) for complete airgapped setup guide.

## Quick Reference

### URLs

- **Create Account**: [https://huggingface.co/join](https://huggingface.co/join)
- **Token Settings**: [https://huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
- **Diarization Model**: [https://huggingface.co/pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)

### Environment Variables

```bash
# Required for speaker diarization
HUGGINGFACE_TOKEN=hf_your_token_here

# Model cache location
MODEL_CACHE_DIR=./models

# Speaker detection range
MIN_SPEAKERS=1
MAX_SPEAKERS=20
```

### Verification Commands

```bash
# Check token configured
grep HUGGINGFACE_TOKEN .env

# Check the diarization pipeline was downloaded
ls -lh models/huggingface/hub/ | grep speaker-diarization-community-1

# Check backend startup for native diarizer provisioning
./opentr.sh logs backend | grep -i diar-native

# Check container logs for the in-process PyAnnote fallback
./opentr.sh logs celery-worker | grep -i pyannote
```

## Next Steps

- [Docker Compose Installation](./docker-compose.md) - Complete installation guide
- [GPU Setup](./gpu-setup.md) - Configure GPU acceleration
- [First Transcription](../getting-started/first-transcription.md) - Test speaker diarization
- [Troubleshooting](./troubleshooting.md) - Fix common issues
