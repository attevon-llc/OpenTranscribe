---
title: What a Self-Hosted OpenTranscribe Install Sends Outside Your Network (Data-Flows Annex)
key: opentranscribe-data-flows-annex
audience: Public - administrators and procurement/security reviewers of self-hosted installs
version: 0.1-draft
status: draft
effective: unset
reviewed: never
owner: Attevon LLC
---

DRAFT - engineering-authored, NOT reviewed by counsel. Do not publish or rely on.

## Summary

- Verified against `attevon-llc/OpenTranscribe` master @ `107cd925` (2026-10-20); the telemetry
  defaults in section 2 were re-checked against `backend/app/core/privacy_env.py` and
  `docker-compose.yml`. Other rows were carried over from the earlier review at `12379984` and are
  not yet re-verified. File paths
  below are relative to that repo unless noted.
- **Attevon receives nothing from a running install.** The app makes no calls to attevon.com,
  opentranscribe.app or any Attevon server, and has no analytics, update check or crash reporting
  enabled.
- **Your audio and transcripts stay on your servers** unless an administrator turns on a cloud
  speech or AI provider.
- **Third-party telemetry is off by default:** pyannote.audio usage metrics, Hugging Face library
  telemetry and the Deno update check are disabled by `backend/app/core/privacy_env.py` and the
  `docker-compose.yml` defaults (commit `07687714`). An operator can turn them back on.
- Installing and updating downloads software from GitHub, Docker Hub and Hugging Face. Those
  services see your IP address like any download.
- Offline mode (`docker-compose.offline.yml`) runs with no model downloads.

## 1. Sent to Attevon LLC

**At runtime: nothing.** No code in `backend/app` or `frontend/src` contacts an Attevon domain.
`docs/design/telemetry_plan.md` describes possible future anonymous telemetry and states it is
"PLAN ONLY - not started". If it ships, this statement must be updated first.

**During install and update (when an administrator runs the scripts):** the scripts fetch files
from GitHub repositories Attevon controls. GitHub, not Attevon, operates these servers; Attevon
receives at most GitHub's and Docker Hub's aggregate download statistics.

- `setup-opentranscribe.sh` (around lines 476, 643, 671-752, 958-959): reachability probe, release
  lookup on `api.github.com/repos/attevon-llc/OpenTranscribe/releases/latest`, and downloads of the
  files in `release-manifest.txt` from `raw.githubusercontent.com`.
- `opentranscribe.sh`: `update` / `update-full` (around lines 1845, 1978-2052) download the same
  files; `version` (around line 2473) checks the latest release.
- Image pulls from Docker Hub `davidamacey/opentranscribe-*` (`docker-compose.prod.yml`, pulled by
  `setup-opentranscribe.sh` around line 2115 and `opentranscribe.sh` around line 1315).

## 2. Sent to third parties by default, and telemetry that is off by default

| What | Destination | Content | Where in code | How to stop it |
| --- | --- | --- | --- | --- |
| Model downloads | Hugging Face Hub | Model requests (IP address, HF library user-agent; HF token for gated pyannote models) | `scripts/download-models.py` (run by setup); `backend/app/transcription/native_provision.py`; `HF_HUB_OFFLINE=0` default in `.env.example` | Offline mode, or pre-load models |
| Search model downloads | `artifacts.opensearch.org` | Model file requests | `backend/app/services/search/model_downloader.py`, `neural_bootstrap.py` | Offline package |
| NLTK data | GitHub-hosted NLTK data | Data file requests | `scripts/download-models.py` lines ~415-425 | `NLTK_OFFLINE` |
| Base images | Docker Hub, quay.io | Image pulls (postgres, redis, opensearch, minio, nginx) | `docker-compose.yml` | Private registry mirror |

**Third-party telemetry that is OFF by default** (`backend/app/core/privacy_env.py`
`TELEMETRY_OFF_DEFAULTS`, and `${VAR:-default}` entries in `docker-compose.yml`). Each can be turned
back on by setting the variable:

| What | Destination if enabled | Default | Variable |
| --- | --- | --- | --- |
| pyannote.audio usage metrics | `otel.pyannote.ai` (pyannoteAI) | off | `PYANNOTE_METRICS_ENABLED=false` |
| Hugging Face library telemetry | Hugging Face | off | `HF_HUB_DISABLE_TELEMETRY=1` |
| Deno update check (used by yt-dlp) | `dl.deno.land` | off | `DENO_NO_UPDATE_CHECK=1` |

Model downloads from Hugging Face still send ordinary request metadata (IP address, library
user-agent).

## 3. Sent to third parties only if an administrator or user configures it

| Feature | Providers | Data sent | Where |
| --- | --- | --- | --- |
| Cloud speech recognition | Deepgram, AssemblyAI, OpenAI, Google, Azure, AWS, Speechmatics, Gladia, pyannoteAI (default `local`) | **Audio** of transcribed files | `backend/app/schemas/asr_settings.py`; `ASR_PROVIDER=local` in `.env.example` |
| Cloud diarization | pyannoteAI | **Audio** | `backend/app/services/diarization/pyannote_provider.py` |
| AI summaries, chat, search answers | OpenAI, Anthropic, OpenRouter, AWS Bedrock, or self-hosted vLLM / Ollama / custom endpoint (none by default) | **Transcript text** and prompts | `backend/app/services/llm_service.py` (`LLMProvider`); `LLM_PROVIDER=` empty in `.env.example` |
| URL / YouTube import | The site the user supplies; `img.youtube.com` for thumbnails | The URL request | `backend/app/services/media_download_service.py` |
| Email notifications | Admin's SMTP server | Notification email content | `SMTP_HOST` empty by default |
| Sign-in | Admin's LDAP / OIDC (Keycloak, Authentik, Azure AD, Google) / PKI revocation | Authentication data | Disabled by default in `.env.example` |
| Object storage | S3-compatible provider instead of bundled MinIO | All stored media | `STORAGE_BACKEND=minio` default |
| Error reporting | Sentry, only if built with `VITE_SENTRY_DSN` | Front-end errors | `frontend/src/lib/monitoring.ts` (inactive; SDK not installed) |

When a cloud provider is configured, that provider's own terms and privacy policy govern the data
it receives. Attevon is not a party to that relationship.

## 4. Browser side

The web app loads fonts and the FFmpeg WebAssembly module from the install itself
(`frontend/static/`). Its Content Security Policy limits connections to the install's own origin
(`frontend/svelte.config.js`, `nginx/site.conf.template`). Links to GitHub or Hugging Face in the
UI are ordinary links followed only if clicked.

## 5. Keeping this annex true

Re-verify before each release (search for new HTTP clients, SDKs and env defaults). Questions:
info@attevon.com.

## Open questions for counsel

1. (Resolved by engineering, 2026-10: third-party telemetry now ships off by default. Counsel to
   confirm the "off by default" wording is acceptable.)
2. If an operator turns the pyannote metrics flow on (audio duration, speaker counts, session ID,
   IP), does it create any disclosure duty for Attevon as distributor, or only for the operator?
3. Is IP-address exposure to GitHub/Docker Hub/Hugging Face during install worth stating in a
   privacy context, or is it adequately covered as ordinary downloading?
4. The OpenRouter integration sends `HTTP-Referer: https://opentranscribe.ai` (a domain not known
   to be Attevon's; `llm_service.py` around line 255). Should it be changed to an Attevon-owned
   domain?
5. Should this statement carry a disclaimer that operators who modify the software are
   responsible for their own data flows?
