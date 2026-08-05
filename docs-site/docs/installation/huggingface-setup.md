---
sidebar_position: 4
---

# HuggingFace Token Setup

Speaker diarization in OpenTranscribe requires access to gated PyAnnote models on HuggingFace. This page guides you through obtaining a free token and accepting the necessary model agreements.

:::warning Critical Requirement
**Speaker diarization will NOT work** without a valid HuggingFace token and acceptance of both gated model agreements. Transcription will still work, but speakers will not be identified.
:::

## Why is HuggingFace Required?

OpenTranscribe uses PyAnnote.audio for speaker diarization (identifying "who spoke when"). PyAnnote's pre-trained models are hosted on HuggingFace as "gated" repositories, meaning you must:

1. Create a free HuggingFace account
2. Accept the model license agreements
3. Use an access token to download the models

This is a one-time setup process. Once configured, models are cached locally and don't require internet access.

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

:::tip Token Storage
Save your token in a password manager or secure note. You'll need it during OpenTranscribe setup. Tokens don't expire unless you delete them.
:::

**Time required**: 2 minutes

## Step 3: Accept Gated Model Agreements

You must accept the license for **BOTH** PyAnnote models. This is required for speaker diarization to work.

### Model 1: PyAnnote Segmentation 3.0

1. Visit [https://huggingface.co/pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
2. Scroll to the model card
3. Click **"Agree and access repository"**
4. ✅ You should see "You have been granted access to this model"

### Model 2: PyAnnote Speaker Diarization 3.1

1. Visit [https://huggingface.co/pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
2. Scroll to the model card
3. Click **"Agree and access repository"**
4. ✅ You should see "You have been granted access to this model"

:::caution Both Models Required
Accepting only one model agreement will result in errors. You must accept **BOTH** model agreements for speaker diarization to function.
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
# Check if PyAnnote models were downloaded
ls -lh models/torch/pyannote/

# You should see:
# segmentation-3.0/
# speaker-diarization-3.1/
```

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

Success indicators:
```
✅ "Loading PyAnnote segmentation model"
✅ "Loading PyAnnote diarization pipeline"
✅ "Speaker diarization completed successfully"
```

Error indicators:
```
"Cannot access gated repository"
"Invalid HuggingFace token"
"Model agreement not accepted"
```

## Troubleshooting

### Error: "Cannot access gated repository"

**Cause**: Model agreement not accepted or token invalid

**Solution**:
1. Verify both model agreements accepted (see Step 3)
2. Check token is correct in `.env` file
3. Regenerate token if needed
4. Restart OpenTranscribe: `./opentr.sh restart`

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
- [ ] Both model agreements accepted
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
${MODEL_CACHE_DIR}/torch/pyannote/
```

Default: `./models/torch/pyannote/`

### Disk Space

PyAnnote models require:
- Segmentation model: ~250MB
- Diarization pipeline: ~250MB
- **Total**: ~500MB

Plus:
- WhisperX models: ~1.5GB
- Wav2Vec2 alignment: ~360MB
- Other models: ~200MB
- **Grand total**: ~2.9GB for all AI models

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
   # Set token and download models
   export HUGGINGFACE_TOKEN=hf_your_token_here
   python3 -c "from pyannote.audio import Model; Model.from_pretrained('pyannote/segmentation-3.0'); Model.from_pretrained('pyannote/speaker-diarization-3.1')"
   ```

2. Copy model cache to offline machine:
   ```bash
   # On internet machine
   tar -czf pyannote-models.tar.gz ~/.cache/torch/pyannote/

   # On offline machine
   tar -xzf pyannote-models.tar.gz -C /path/to/opentranscribe/models/torch/
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
- **Segmentation Model**: [https://huggingface.co/pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
- **Diarization Model**: [https://huggingface.co/pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)

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

# Check models downloaded
ls -lh models/torch/pyannote/

# Check container logs
./opentr.sh logs celery-worker | grep -i pyannote
```

## Next Steps

- [Docker Compose Installation](./docker-compose.md) - Complete installation guide
- [GPU Setup](./gpu-setup.md) - Configure GPU acceleration
- [First Transcription](../getting-started/first-transcription.md) - Test speaker diarization
- [Troubleshooting](./troubleshooting.md) - Fix common issues
