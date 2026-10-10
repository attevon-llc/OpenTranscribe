# ASR Cloud Provider Testing Guide

Reference for testing each of OpenTranscribe's 10 ASR providers (1 local + 9 cloud).

## Overview

All providers share the same integration surface: a `validate_connection()` method for quick credential checks and a `transcribe()` method for end-to-end audio processing. Configuration can be set via `.env` variables or through the Settings UI (per-user ASR configs stored in the database).

**What you can test without any API keys:**
- Local (GPU) provider -- always available when a GPU is present
- Factory fallback logic -- misconfigured cloud providers fall back to local
- Provider catalog API -- `GET /api/asr-settings/providers` returns all providers and models
- Connection test endpoint -- `POST /api/asr-settings/test` with dummy keys (will return auth errors)

**What requires API keys:**
- `validate_connection()` for any cloud provider (lightweight credential check)
- Full `transcribe()` calls (uploads audio, incurs API costs)

## Deployment Mode: API-Lite (`DEPLOYMENT_MODE=lite`)

OpenTranscribe v0.4.0 introduces a lightweight deployment mode that removes the GPU worker entirely and routes all transcription through cloud ASR providers.

```bash
# .env
DEPLOYMENT_MODE=lite          # Disables GPU worker, enables cloud-only mode
ASR_PROVIDER=deepgram         # Required: set a cloud provider as default
```

**How it differs from full mode:**

| Aspect | `full` (default) | `lite` |
|--------|-----------------|--------|
| GPU worker | Required | Not started |
| Local (GPU) ASR | Yes | No |
| Cloud ASR | Optional | Required |
| PyAnnote diarization | Yes | Depends on provider |
| WhisperX models | Downloaded | Not downloaded |
| Min RAM for transcription | ~6GB VRAM | 0 (host RAM only) |
| Docker image size | ~12GB | ~3GB |

**Use cases for `lite` mode:**
- Deploying on CPU-only servers without GPU hardware
- Cost-optimized cloud deployments where GPU instances are too expensive
- Edge deployments with limited resources
- Development/testing without a GPU

**Limitations of `lite` mode:**
- No local fallback if cloud provider fails
- Speaker diarization depends entirely on the chosen provider's capabilities
- Translation to English requires a provider that supports it (only `whisper-1` via OpenAI)
- pyannote.ai provider (`transcribe()`) is not yet implemented (Phase A stub)

## Custom Vocabulary

Several providers support custom vocabulary to improve accuracy for domain-specific terms, proper nouns, and jargon.

| Provider | Feature Name | Max Terms | Format |
|----------|-------------|-----------|--------|
| Local | `initial_prompt` | Unlimited | Free-text hint to Whisper |
| Deepgram | Keywords | 100 per request | List of strings with optional weight |
| AssemblyAI | `word_boost` | Unlimited | List of strings |
| AWS | Custom Vocabulary | 256 terms | S3-uploaded vocab file |
| Speechmatics | `additional_vocab` | Unlimited | List of strings |
| Gladia | `custom_vocabulary` | Unlimited | List of strings |
| OpenAI | No vocabulary support | — | — |
| Google | No vocabulary support | — | — |
| Azure | No vocabulary support | — | — |
| pyannote.ai | No vocabulary support | — | Phase A stub only |

**Configuration via Settings UI:** Settings → Transcription → Provider & Model → Edit Config → Custom Vocabulary field accepts comma-separated terms, stored per-provider config in the database.

**Custom vocabulary is passed to the provider at transcription time.** The local provider passes vocabulary as an `initial_prompt` to Whisper (e.g., `"Vocabulary: PyAnnote, WhisperX, CTranslate2"`), which biases the model toward those spellings.

---

## Local Provider

No keys, no network. Requires a CUDA-capable GPU.

| Item | Value |
|------|-------|
| Provider ID | `local` |
| Default model | `large-v3-turbo` |
| Diarization | Yes (PyAnnote) |
| Translation | `large-v3` and `large-v2` only (`large-v3-turbo` cannot translate) |
| Vocabulary | Yes |

**.env configuration:**
```bash
ASR_PROVIDER=local          # default
WHISPER_MODEL=large-v3-turbo
```

**Quick validation:**
```bash
# Check GPU is visible inside the container
docker exec opentranscribe-celery-worker nvidia-smi

# Upload any short audio file via the UI -- should process without errors
```

---

## Deepgram

| Item | Value |
|------|-------|
| Provider ID | `deepgram` |
| Console | https://console.deepgram.com |
| Key format | `dg_*` |
| Default model | `nova-3` |
| Diarization | Yes |
| Translation | No |
| Vocabulary | Yes (up to 100 keywords) |
| SDK | `deepgram-sdk>=3.0.0` |

**.env variables:**
```bash
ASR_PROVIDER=deepgram
DEEPGRAM_API_KEY=dg_xxxxxxxxxxxxxxxx
DEEPGRAM_MODEL=nova-3          # or nova-3-medical, nova-2
```

**UI configuration:** Settings > Transcription > Provider & Model > Add Config > Deepgram > paste API key > select model > Test Connection.

**Models:**

| Model | Languages | Price/min | Notes |
|-------|-----------|-----------|-------|
| `nova-3` | 36 | $0.0043 | Default, best accuracy |
| `nova-3-medical` | 1 (English) | $0.0043 | Medical terminology |
| `nova-2` | 36 | $0.0043 | Legacy |

**Expected behavior:**
- `validate_connection()` calls `client.manage.v("1").get_projects()` -- succeeds if the key has any project access.
- Diarization uses Deepgram's native utterance-based speaker labels (0-indexed integers normalized to `SPEAKER_XX`).
- `language: "auto"` sends `language=None` to Deepgram for auto-detection.

**Common errors:**
| Error | Cause | Fix |
|-------|-------|-----|
| `401 Unauthorized` | Invalid or expired key | Regenerate key in Deepgram console |
| `deepgram-sdk not installed` | Missing SDK in container | Included in full image; check `DEPLOYMENT_MODE` |
| Empty transcript | Audio format not supported | Convert to WAV/MP3 first |

**Free tier:** $200 credit on signup.

---

## AssemblyAI

| Item | Value |
|------|-------|
| Provider ID | `assemblyai` |
| Console | https://www.assemblyai.com/dashboard |
| Key format | Starts with `aai_` (some legacy keys don't) |
| Default model | `universal` |
| Diarization | Yes (all models) |
| Translation | No |
| Vocabulary | Yes (`word_boost`) |
| SDK | `assemblyai>=0.30.0` |

**.env variables:**
```bash
ASR_PROVIDER=assemblyai
ASSEMBLYAI_API_KEY=aai_xxxxxxxxxxxxxxxx
ASSEMBLYAI_MODEL=universal      # or universal-multilingual, slam-1, nano
```

**Models:**

| Model | Languages | Price/min | Notes |
|-------|-----------|-----------|-------|
| `universal` | 1 (English) | $0.0025 | Default |
| `universal-multilingual` | 99 | $0.0045 | Auto language detection |
| `slam-1` | 1 (English) | $0.0062 | Highest accuracy |
| `nano` | 1 (English) | $0.0020 | Budget-friendly |

**Expected behavior:**
- `validate_connection()` does a lightweight `GET /v2/transcript?limit=1` with the API key in the `authorization` header.
- Speaker labels are single letters (`"A"`, `"B"`, ...) normalized to `SPEAKER_00`, `SPEAKER_01`, etc.

**Common errors:**
| Error | Cause | Fix |
|-------|-------|-----|
| `401 Unauthorized` | Bad API key | Check key in AssemblyAI dashboard |
| `402 Payment Required` | Exhausted free tier | Add payment method |
| Timeout on long files | Large audio + `slam-1` | Use `universal` for faster turnaround |

**Free tier:** Available (limited hours/month).

---

## OpenAI

| Item | Value |
|------|-------|
| Provider ID | `openai` |
| Console | https://platform.openai.com/api-keys |
| Key format | `sk-*` |
| Default model | `gpt-4o-transcribe` |
| Diarization | No (neither model supports it) |
| Translation | `whisper-1` only |
| Vocabulary | No |
| File limit | **25 MB maximum** |
| SDK | `openai>=1.0.0` |

**.env variables:**
```bash
ASR_PROVIDER=openai
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx    # shared with LLM provider if also using OpenAI
OPENAI_ASR_MODEL=gpt-4o-transcribe   # or whisper-1
```

**Models:**

| Model | Languages | Price/min | Translation | Notes |
|-------|-----------|-----------|-------------|-------|
| `gpt-4o-transcribe` | — | $0.006 | No | Higher accuracy, default |
| `whisper-1` | 99 | $0.006 | Yes | Supports translate to English |

**Expected behavior:**
- `validate_connection()` calls `client.models.list()` to verify the API key.
- No diarization at all. If diarization is needed, use a different provider.
- Files over 25 MB are rejected before upload.
- `whisper-1` returns `avg_logprob` per segment, converted to confidence via `exp()`.

**Common errors:**
| Error | Cause | Fix |
|-------|-------|-----|
| `413 / file too large` | Audio exceeds 25 MB | Compress audio or use a different provider |
| `401 invalid_api_key` | Bad or revoked key | Regenerate at platform.openai.com |
| `429 rate_limit_exceeded` | Too many concurrent requests | Wait and retry, or upgrade plan |
| No speaker labels | Expected behavior | OpenAI does not support diarization |

**Free tier:** None (pay-as-you-go). New accounts may have usage limits.

---

## Google Cloud Speech

| Item | Value |
|------|-------|
| Provider ID | `google` |
| Console | https://console.cloud.google.com/speech |
| Authentication | Service account JSON (not API key) |
| Default model | `chirp-3` |
| Diarization | Yes |
| Translation | No |
| Vocabulary | No |
| SDK | `google-cloud-speech>=2.24.0` |

**.env variables:**
```bash
ASR_PROVIDER=google
GOOGLE_CLOUD_CREDENTIALS=/path/to/service-account.json
GOOGLE_ASR_MODEL=chirp-3        # or long, short
```

**Setup steps:**
1. Create a GCP project at https://console.cloud.google.com
2. Enable the Speech-to-Text API
3. Create a service account with the "Cloud Speech Client" role
4. Download the JSON key file
5. Mount/copy the JSON file into the backend container
6. Set `GOOGLE_CLOUD_CREDENTIALS` to the path inside the container (or pass via UI config)

**Models:**

| Model | Languages | Price/min | Notes |
|-------|-----------|-----------|-------|
| `chirp-3` | 100+ | $0.024 | Default, best multilingual |
| `long` | 100+ | $0.024 | Optimized for long recordings |
| `short` | 100+ | $0.024 | Optimized for short clips |

**Expected behavior:**
- `validate_connection()` instantiates a `SpeechClient` -- succeeds if `GOOGLE_APPLICATION_CREDENTIALS` is valid.
- Speaker tags are 1-indexed integers from Google (1, 2, 3...) converted to 0-indexed `SPEAKER_XX` labels.
- `language: "auto"` defaults to `"en-US"` (Google requires an explicit language code).
- Uses `long_running_recognize` with a 1-hour timeout.

**Common errors:**
| Error | Cause | Fix |
|-------|-------|-----|
| `DefaultCredentialsError` | No credentials file found | Set `GOOGLE_CLOUD_CREDENTIALS` or `GOOGLE_APPLICATION_CREDENTIALS` |
| `403 permission denied` | Service account lacks Speech API role | Add "Cloud Speech Client" role in IAM |
| `400 invalid encoding` | Audio format mismatch | Google expects LINEAR16; provider handles conversion |

**Free tier:** $300 credit on signup (90 days).

---

## Azure Speech

| Item | Value |
|------|-------|
| Provider ID | `azure` |
| Console | https://portal.azure.com/#view/Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/SpeechServices |
| Key format | 32-character hex string |
| Region | **Required** (27 supported regions) |
| Default model | `whisper` |
| Diarization | Yes (via `ConversationTranscriber`) |
| Translation | No |
| Vocabulary | No |
| SDK | `azure-cognitiveservices-speech>=1.35.0` |

**.env variables:**
```bash
ASR_PROVIDER=azure
AZURE_SPEECH_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
AZURE_SPEECH_REGION=eastus       # must match your resource's region
AZURE_ASR_MODEL=whisper          # or standard
```

**Supported regions:**
`westus`, `westus2`, `eastus`, `eastus2`, `centralus`, `northcentralus`, `southcentralus`, `westeurope`, `northeurope`, `uksouth`, `ukwest`, `francecentral`, `germanywestcentral`, `switzerlandnorth`, `australiaeast`, `australiasoutheast`, `southeastasia`, `eastasia`, `japaneast`, `japanwest`, `koreacentral`, `koreasouth`, `canadacentral`, `canadaeast`, `brazilsouth`, `southafricanorth`, `uaenorth`

**Models:**

| Model | Languages | Price/min | Notes |
|-------|-----------|-----------|-------|
| `whisper` | 99 | $0.017 | Default, Whisper via Azure |
| `standard` | 100+ | $0.017 | Azure's own model |

**Expected behavior:**
- `validate_connection()` constructs a `SpeechConfig` object (lightweight, no audio processing).
- Diarization uses `ConversationTranscriber` (the only Azure SDK class with speaker IDs). Speaker labels are `"Guest-1"`, `"Guest-2"`, etc., normalized to `SPEAKER_00`, `SPEAKER_01`.
- Non-diarization path uses `SpeechRecognizer` with continuous recognition.
- Timestamps are in 100-nanosecond ticks, converted to seconds internally.

**Common errors:**
| Error | Cause | Fix |
|-------|-------|-----|
| `401 PermissionDenied` | Invalid key or wrong region | Verify key+region match your Azure resource |
| `CancellationReason.Error` | Network issue or unsupported format | Check Azure Speech service health; verify audio format |
| Region validation error | Typo or unsupported region | Use one of the 27 regions listed above |

**Free tier:** $200 Azure credit on signup; Speech Services has a free tier of 5 hours/month.

---

## Amazon Transcribe (AWS)

| Item | Value |
|------|-------|
| Provider ID | `aws` |
| Console | https://console.aws.amazon.com/transcribe |
| Authentication | IAM role (preferred) or access key + secret |
| Region | **Required** (23 supported regions) |
| Default model | `standard` |
| Diarization | Yes |
| Translation | No |
| Vocabulary | Yes (custom vocabulary) |
| SDK | `boto3>=1.26.0` |

**.env variables:**
```bash
ASR_PROVIDER=aws
AWS_REGION=us-east-1

# Option A: IAM role (leave blank -- uses instance profile / task role)
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=

# Option B: Explicit credentials
AWS_ACCESS_KEY_ID=AKIAxxxxxxxxxxxxxxxx
AWS_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

AWS_ASR_MODEL=standard           # or medical
AWS_TRANSCRIBE_BUCKET=my-transcribe-bucket  # S3 bucket for intermediate files
```

**Supported regions:**
`us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`, `ca-central-1`, `ca-west-1`, `eu-west-1`, `eu-west-2`, `eu-west-3`, `eu-central-1`, `eu-north-1`, `eu-south-1`, `ap-southeast-1`, `ap-southeast-2`, `ap-southeast-3`, `ap-northeast-1`, `ap-northeast-2`, `ap-northeast-3`, `ap-south-1`, `ap-east-1`, `sa-east-1`, `me-south-1`, `af-south-1`

**Models:**

| Model | Price/min | Notes |
|-------|-----------|-------|
| `standard` | $0.024 | Default, general purpose |
| `medical` | $0.075 | HIPAA-eligible, medical vocabulary |

**Expected behavior:**
- `validate_connection()` calls `list_transcription_jobs(MaxResults=1)` to verify credentials and region.
- Audio is uploaded to a temporary S3 bucket, transcribed, and cleaned up after completion (even on failure).
- Speaker labels use `"spk_0"`, `"spk_1"`, etc. (0-indexed), normalized to `SPEAKER_XX`.

**Common errors:**
| Error | Cause | Fix |
|-------|-------|-----|
| `AccessDeniedException` | IAM policy missing `transcribe:*` permissions | Attach `AmazonTranscribeFullAccess` policy |
| `NoSuchBucket` | S3 bucket doesn't exist or wrong region | Create bucket in the same region as `AWS_REGION` |
| `InvalidClientTokenId` | Bad access key | Verify credentials in IAM console |
| Region validation error | Unsupported region string | Use one of the 23 regions listed above |

**Free tier:** 60 minutes/month for the first 12 months.

---

## Speechmatics

| Item | Value |
|------|-------|
| Provider ID | `speechmatics` |
| Console | https://portal.speechmatics.com |
| Key format | Bearer token |
| Default model | `standard` |
| Diarization | Yes (3 modes: speaker / channel / none) |
| Translation | No |
| Vocabulary | Yes |
| SDK | `speechmatics-python>=1.11.0` |

**.env variables:**
```bash
ASR_PROVIDER=speechmatics
SPEECHMATICS_API_KEY=xxxxxxxxxxxxxxxx
SPEECHMATICS_MODEL=standard
```

**Models:**

| Model | Languages | Price/min | Notes |
|-------|-----------|-----------|-------|
| `standard` | 55+ | $0.004 | Only model; all features included |

**Expected behavior:**
- `validate_connection()` calls `GET /v2/jobs?limit=1` with the Bearer token.
- Speaker labels use `"S1"`, `"S2"`, etc. (1-indexed), normalized to `SPEAKER_00`, `SPEAKER_01`.
- Batch API with polling (10s intervals, 2-hour hard cap).

**Common errors:**
| Error | Cause | Fix |
|-------|-------|-----|
| `401 Unauthorized` | Invalid API key | Regenerate key in Speechmatics portal |
| `speechmatics-python not installed` | Missing SDK | Included in full image |
| Job timeout | Very long audio + slow processing | Increase timeout or split audio |

**Free tier:** Free trial available.

---

## Gladia

| Item | Value |
|------|-------|
| Provider ID | `gladia` |
| Console | https://app.gladia.io |
| Key format | Passed via `x-gladia-key` header |
| Default model | `standard` |
| Diarization | Yes |
| Translation | No |
| Vocabulary | Yes |
| SDK | None (REST API via `requests`) |

**.env variables:**
```bash
ASR_PROVIDER=gladia
GLADIA_API_KEY=xxxxxxxxxxxxxxxx
GLADIA_MODEL=standard
```

**Models:**

| Model | Languages | Price/min | Notes |
|-------|-----------|-----------|-------|
| `standard` | 100+ | $0.010 | All features bundled |

**Expected behavior:**
- `validate_connection()` calls `GET /v2/live` with the API key header.
- Speaker labels use `"speaker_0"`, `"speaker_1"`, etc. (0-indexed), normalized to `SPEAKER_XX`.
- Uses Gladia REST API v2 directly (no dedicated Python SDK required).

**Common errors:**
| Error | Cause | Fix |
|-------|-------|-----|
| `401` from `/v2/live` | Invalid API key | Regenerate key at app.gladia.io |
| `requests not installed` | Missing dependency | Should be pre-installed; check container |
| Polling timeout | Long audio, slow processing | Audio is processed asynchronously; wait for webhook/poll |

**Free tier:** Available.

---

## pyannote.ai

| Item | Value |
|------|-------|
| Provider ID | `pyannote` |
| Console | https://dashboard.pyannote.ai |
| Key format | Bearer token |
| Default model | `parakeet` |
| Diarization | Yes (premium, bundled) |
| Translation | No |
| Vocabulary | No |
| Status | **Phase A** -- `validate_connection()` only; `transcribe()` raises `NotImplementedError` |

**.env variables:**
```bash
# Not yet in .env.example (Phase A stub)
PYANNOTE_API_KEY=xxxxxxxxxxxxxxxx
PYANNOTE_MODEL=parakeet          # or whisper-large-v3-turbo
```

**Models:**

| Model | Languages | Price/min | Notes |
|-------|-----------|-----------|-------|
| `parakeet` | 100 | $0.027 | NVIDIA Parakeet + premium diarization |
| `whisper-large-v3-turbo` | 100 | $0.027 | OpenAI Whisper + premium diarization |

**Expected behavior:**
- `validate_connection()` calls `GET /v1/info` with the Bearer token. Returns success on HTTP 200, auth error on 401.
- `transcribe()` currently raises `NotImplementedError` (Phase B pending).

**Common errors:**
| Error | Cause | Fix |
|-------|-------|-----|
| `401 Invalid API key` | Bad token | Regenerate at dashboard.pyannote.ai |
| `httpx not installed` | Missing dependency | Should be pre-installed; check container |
| `NotImplementedError` | Transcription not yet implemented | Expected in Phase A; use another provider for actual transcription |

**Free tier:** Contact pyannote.ai for pricing.

---

## Running the Test Suite

### Unit tests (no API keys needed)

The ASR provider code is testable via mocked SDK calls. Currently there are no dedicated ASR provider unit tests. To run the existing test suite:

```bash
source backend/venv/bin/activate

# All backend tests
pytest backend/tests/ -v

# Unit tests only
pytest backend/tests/unit/ -v

# Sharing tests (includes ASR config sharing)
pytest backend/tests/api/endpoints/test_sharing.py -v
```

### Connection test via API (requires running dev environment)

```bash
# Start dev environment
./opentr.sh start dev

# Test a provider connection (replace with real key)
curl -X POST http://localhost:5174/api/asr-settings/test \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-jwt-token>" \
  -d '{"provider": "deepgram", "api_key": "dg_xxxx"}'

# Response:
# {"success": true, "message": "Deepgram connection successful", "response_time_ms": 342.1}
```

### End-to-end transcription test

1. Configure a cloud provider in Settings → Transcription → Provider & Model
2. Click "Test Connection" to verify credentials
3. Set the config as active
4. Upload a short audio file
5. Verify the transcript appears with expected segments/speakers

### Provider catalog API (no auth needed for catalog, auth needed for configs)

```bash
# List all providers and their models
curl http://localhost:5174/api/asr-settings/providers

# Check current ASR status for authenticated user
curl http://localhost:5174/api/asr-settings/status \
  -H "Authorization: Bearer <your-jwt-token>"
```

---

## Test Audio Recommendations

For quick validation of cloud providers, use short clips (5-15 seconds) to minimize cost and turnaround time.

| Clip Type | Duration | Why |
|-----------|----------|-----|
| Single speaker, clear English | ~10s | Baseline -- every provider should handle this |
| Two speakers, conversation | ~15s | Tests diarization (skip for OpenAI) |
| Non-English speech | ~10s | Tests language detection / multilingual models |
| Noisy background | ~10s | Tests robustness |

**Recommended free sources:**
- [LibriSpeech test-clean](https://www.openslr.org/12/) -- single speaker, clean English
- [AMI Corpus samples](https://groups.inf.ed.ac.uk/ami/corpus/) -- multi-speaker meetings
- Record a short clip with your phone (fastest approach for a quick smoke test)

**File format notes:**
- All providers accept WAV and MP3.
- OpenAI has a **25 MB file limit** -- use short clips or compressed formats.
- Google expects LINEAR16 encoding internally; the provider handles conversion.
- AWS uploads audio to S3 as an intermediate step; ensure the bucket exists.

---

## Quick Reference: Provider Capabilities

| Provider | Diarization | Translation | Vocabulary | Region Required | Auth Method |
|----------|:-----------:|:-----------:|:----------:|:---------------:|-------------|
| Local | Yes | Partial | Yes | No | None |
| Deepgram | Yes | No | Yes | No | API key |
| AssemblyAI | Yes | No | Yes | No | API key |
| OpenAI | No | `whisper-1` only | No | No | API key |
| Google | Yes | No | No | No | Service account JSON |
| Azure | Yes | No | No | Yes (27) | API key + region |
| AWS | Yes | No | Yes | Yes (23) | IAM role or access keys |
| Speechmatics | Yes | No | Yes | No | API key |
| Gladia | Yes | No | Yes | No | API key |
| pyannote.ai | Yes | No | No | No | API key |

| Provider | Speaker Label Format | Indexing | Normalization Example |
|----------|---------------------|----------|----------------------|
| Local | `SPEAKER_XX` (PyAnnote) | 0-indexed | `SPEAKER_00` (pass-through) |
| Deepgram | `"0"`, `"1"`, ... | 0-indexed | `"0"` -> `SPEAKER_00` |
| AssemblyAI | `"A"`, `"B"`, ... | 0-indexed | `"B"` -> `SPEAKER_01` |
| Google | `1`, `2`, ... (int tags) | 1-indexed | `2` -> `SPEAKER_01` |
| Azure | `"Guest-1"`, `"Guest-2"`, ... | 1-indexed | `"Guest-2"` -> `SPEAKER_01` |
| AWS | `"spk_0"`, `"spk_1"`, ... | 0-indexed | `"spk_1"` -> `SPEAKER_01` |
| Speechmatics | `"S1"`, `"S2"`, ... | 1-indexed | `"S1"` -> `SPEAKER_00` |
| Gladia | `"speaker_0"`, `"speaker_1"`, ... | 0-indexed | `"speaker_0"` -> `SPEAKER_00` |
