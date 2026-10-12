---
sidebar_position: 2
title: Deployment Variants
description: Full (GPU), CPU-only and Lite (cloud-only) compared, the published images, which cloud providers return speakers, and which transcription and speaker-detection combinations are valid
---

import Images from './_generated/images.md';
import ProviderSpeakers from './_generated/provider-speakers.md';
import ValidityMatrix from './_generated/validity-matrix.md';

# Deployment Variants

OpenTranscribe processes a recording in four steps:

1. **Transcription**: speech to text, with timestamps.
2. **Speaker detection**: who spoke when (also called diarization).
3. **Speaker identification**: a voiceprint per speaker, used to recognise the same person in
   other recordings.
4. **AI features** (optional): summaries, topics and suggested speaker names from an LLM.

Each user chooses, in Settings, **where transcription runs** and **where speaker detection
runs**, independently. Voiceprints are always computed on your own server. Which choices are
available depends on the variant you install.

## The three variants

| | Full (GPU) | CPU-only | Lite (cloud-only) |
|---|---|---|---|
| Backend image | `opentranscribe-backend` | `opentranscribe-backend` (same image) | `opentranscribe-backend-lite` |
| Where transcription runs | This server's GPU (Whisper), or a cloud provider per user | This server's CPU (Whisper, slow), or a cloud provider per user | A cloud provider only |
| Where speaker detection runs | This server, pyannote.ai, or a cloud provider | This server (on the CPU, slow), pyannote.ai, or a cloud provider | pyannote.ai or a cloud provider only |
| Where voiceprints run | This server | This server (CPU) | This server (CPU) |
| Hardware | NVIDIA GPU with the NVIDIA container runtime | Any x86-64 host | Any x86-64 or arm64 host |
| Accounts | HuggingFace token for the local speaker models ([setup](./huggingface-setup.md)); cloud keys only if users want cloud providers | Same as Full | An API key for at least one cloud transcription provider; pyannote.ai or a second provider if your transcription model returns text only; a HuggingFace token if the diar-native sidecar provisions the voiceprint model here ([setup](./huggingface-setup.md)) |
| Choose it when | You have a GPU and want audio to stay on your server | You have no GPU, want audio to stay on your server, and can accept slow processing | You have no GPU (or an arm64 machine) and are willing to send audio to a cloud provider |

### Full (GPU)

The default install. Whisper (faster-whisper) transcribes on the GPU, and speaker detection on
this server uses the engine the administrator chose: the diar-native sidecar (the default) or
in-process PyAnnote. Users can still pick a cloud provider for either step in Settings.

```bash
curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

### CPU-only

The **same image** as Full, run on a host without a usable GPU. The installer switches to it
automatically when it finds no NVIDIA GPU, or when you ask for it:

```bash
curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/setup-opentranscribe.sh | bash -s -- --cpu
```

`--cpu` (or `OPENTRANSCRIBE_FORCE_CPU=1`) writes `FORCE_CPU_MODE=true` to `.env`, and
`opentranscribe.sh` then never loads the GPU overlays, even if an NVIDIA runtime appears later.
On a CPU host the installer recommends the small `base` Whisper model and writes
`ENABLE_DIARIZATION=false`.

:::caution Speaker detection is not switched off by `ENABLE_DIARIZATION`
In this release each file's speaker detection follows the user's **Speaker detection** setting,
not `ENABLE_DIARIZATION`. On a CPU-only host, speaker detection on this server runs on the CPU
and is slow. To skip it, set **Speaker detection** to **Off** in Settings. See
[environment variables](../configuration/environment-variables.md) for what the variable does.
:::

### Lite (cloud-only)

A smaller, CPU-only backend image with **no local transcription and no local speaker
detection**. Every file is transcribed by a cloud provider, and speakers come from that
provider, from pyannote.ai, or from a second provider you have saved. Voiceprints are still
computed on this server's CPU, so cross-file speaker identification keeps working and no
vendor ever stores a voiceprint for you.

```bash
curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/setup-opentranscribe.sh | bash -s -- --lite
```

`--lite` implies `--cpu` and writes `DEPLOYMENT_MODE=lite`. On an arm64 host the management
script uses the lite image by default, because it is the only backend published for arm64.

On lite, choosing local transcription or "This server" for speaker detection is refused with a
clear message:

| Code | Meaning |
|---|---|
| `local_asr_unavailable` | This deployment cannot transcribe on this server. Choose a cloud provider. |
| `local_diarization_unavailable` | This deployment has no local speaker detection. Choose pyannote.ai or a provider that returns speakers. |
| `automatic_needs_speaker_provider` | Your transcription model returns text only and this deployment cannot detect speakers itself. Choose pyannote.ai or another provider for speaker detection. |

When an existing install switches to lite, a one-time database migration moves users whose
speaker detection was set to "This server" to **Automatic**, but only when their active cloud
model returns speakers itself. Everyone else keeps their setting and sees the message above in
Settings until they pick a speaker-detection provider. No transcript, speaker or voiceprint is
changed by it.

## Published images

The table is generated from `./scripts/docker-build-push.sh list-platforms` and
`./scripts/security-scan.sh list-repos`, the two scripts the release pipeline builds and scans
from, so it always matches what is published.

<Images />

`vX.Y.Z` on a backend repository is a multi-architecture index over the per-architecture tags
listed beside it, and `latest` points at the same index. The frontend and docs images are shared
by every variant.

## Which cloud providers return speakers

A provider that **returns speakers** labels each part of the transcript with a speaker itself.
A **text only** model returns no speakers; on the Full and CPU-only images "Automatic" then
detects speakers on this server, and on Lite you must pick pyannote.ai or another provider.

**Timing** is how precisely the provider timestamps its text, and it decides how speaker turns
are matched to the text:

- **Word**: each word gets the speaker who was talking at that moment. A change of speaker in
  the middle of a sentence is kept.
- **Segment**: each segment (roughly a sentence) gets the speaker who talked most during it, and
  its words follow. A change of speaker inside one segment is lost (warning
  `segment_timing_only`).
- **None**: there is nothing to match speakers to, so speaker detection is refused for that
  model (`asr_no_timing_for_speakers`). OpenTranscribe does not run a separate alignment model
  to add timestamps. Choose a model with word or segment timing, or turn speaker detection off.

**Speaker-count hints** are the minimum, maximum or exact number of speakers you can set in
Settings or per file; a provider that does not receive them detects the count itself. On this
server, the in-process PyAnnote engine honours all three hints and the default diar-native
engine counts speakers automatically and ignores them.

This table is generated from the provider catalog in `backend/app/services/asr/factory.py`.

<ProviderSpeakers />

## Which combinations are valid

The table below is produced by running the same decision function the server uses for every
file (`resolve_pipeline_plan` in `backend/app/services/pipeline_plan.py`) over each
combination. Each cell says who detects the speakers and when, or the code of the message a
user sees when the combination is refused. Refused combinations are refused before any work
starts: the file is marked as failed with that message and nothing is sent to a provider.

- **During transcription**: speakers are detected in the same step as the text.
- **Alongside transcription**: a cloud transcription and a cloud speaker-detection call run
  together, in one cloud task.
- **After transcription**: the transcript is saved first, then speaker detection runs as a
  second step and the transcript is re-split at speaker changes.
- If a cloud speaker detector fails, the transcript is kept, without speakers; the file can be
  re-diarized later.
- **Extra billing** and **segment merge** are explained on this page.
- The columns are the choices under **Settings → Speaker Identification → Speaker detection**:
  "Automatic (recommended)", "Always on this server's GPU" (on a CPU-only host it runs on the
  CPU), "pyannote.ai cloud service" and "Off (one speaker for the whole recording)". "Another provider's speaker detection" (`asr_config`) is offered to users allowed to save their
  own providers.
- In every cell where speakers are detected, voiceprints are then computed on this server.

<ValidityMatrix />

The "This server" engine is the one the administrator chose (diar-native sidecar or in-process
PyAnnote); users see it but do not choose it. A deployment can also lock speaker detection for
everyone; users then see that their administrator manages speaker detection.

## When audio leaves your server

Audio is sent to a third party only for a step that a cloud provider performs:

- **Cloud transcription** sends the audio to that provider.
- **pyannote.ai speaker detection** sends the audio to pyannote.ai.
- **Another provider for speakers only** sends the audio to that provider as a full
  transcription request. Its text is thrown away; only the speaker turns are kept.

Billing follows the same lines. Each provider bills you under your own account for the audio it
receives. Using a provider for speakers only means it charges for a **full transcription** even
though its text is discarded, so with a different transcription provider you pay **two
vendors** for every file; OpenTranscribe flags that choice with an extra-billing warning
(`diarizer_double_billing`). Choosing
the same provider and model you transcribe with uses its own speakers at no extra cost.

What never leaves your server: voiceprints (they are always computed here, whichever provider
detected the speakers) and the stored transcript, which is kept in your own database and
storage. AI features are configured separately; see [LLM integration](../features/llm-integration.md).

## Where each file's processing is recorded

Every file records which provider and model completed each step and where it ran (this server
or a named cloud provider). Open a file's details or its status to see it. Files processed
before this was recorded show "Not recorded" for those steps rather than a guess. See
[uploading files](../user-guide/uploading-files.md).

## See also

- [Speaker diarization](../features/speaker-diarization.md): how speaker detection and
  voiceprints work.
- [Transcription](../features/transcription.md): local Whisper and the cloud providers.
- [Deployment configuration](../operations/deployment-configuration.md): every launch command
  and overlay.
- [Hardware requirements](./hardware-requirements.md) and [GPU setup](./gpu-setup.md).
