# Anonymous Usage Telemetry — Implementation Plan

Status: PLAN ONLY — not started. No code in this repo implements any of this yet (verified:
no outbound "phone home" call exists anywhere in `backend/` today; the only outbound calls are
model downloads).

Owner: David Macey. Driving question: OpenTranscribe has ~6.8k Docker Hub pulls with zero
visibility into how many are real, running, still-active instances vs CI/mirroring/one-off pulls.
This plan adds an anonymous, opt-out (with an opt-in consent gate), non-invasive heartbeat to
answer that, modeled on prior art from comparable self-hosted OSS projects, with the receiver
side also specified end-to-end so a fresh implementation agent can execute this without
re-deriving anything.

## 1. Prior art this plan is built on

Researched directly from each project's current docs/source (not from training-data memory).
Full comparison table with all fields cited retained in conversation history / can be
re-researched if this file goes stale — the summary below is what drives the design decisions.

| Project | Good pattern taken | Bad pattern avoided |
|---|---|---|
| Homebrew | Notice-before-first-send; self-hosted receiver (moved off Google Analytics in 2023 after 2016 backlash); publishes aggregates publicly | Never nag an opted-out user to re-enable (caused a second backlash, brew#15719) |
| Ultralytics YOLO | — | Sent to Google Analytics with the API secret hardcoded in source; ID derived from hashed MAC address; docs arrived months after rollout (issue #6405, a fork exists just to strip telemetry) |
| Next.js | `NEXT_TELEMETRY_DEBUG=1` prints the exact payload and sends nothing — copy this exactly | — |
| Terraform/HashiCorp Checkpoint | Ping gives the user something back (security bulletins) — makes it far less objectionable | — |
| Sentry (self-hosted) — closest prior art: telemetry *for* a self-hosted product | Hourly beacon to their own server; explicit air-gap switch (`SENTRY_AIR_GAP=True`) that hard-disables regardless of other settings | — |
| Grafana / Loki / Tempo | Tempo exposes your own report at `/status/usage-stats` so admins can see exactly what was sent | A 2022 forum report says it still resolved `stats.grafana.org` even with reporting disabled — verify "off" truly means zero network calls |
| Stirling PDF (closest self-hosted-Docker analog) | **First-admin-login opt-in prompt**, not silent opt-out default | — |
| n8n / Langfuse | — | Langfuse sends up to 30 user email domains — scope creep from "anonymous" toward identifying; never do this |
| Apache Superset + Scarf | Scarf backend-only pull-gateway is fine as an *additional*, later, optional signal | A browser-side Scarf tracking pixel got Superset flagged as phishing by Chrome/Firefox (discussion #31856) — never a frontend pixel |
| Go toolchain | — | Proposed opt-out triggered a large backlash purely from being silent about it; ended up switching to opt-in (golang#58894) |
| Docker Desktop | — | Enabled by default with no disclosure at rollout (docker/for-win#13689); a later version also failed to start when offline with the setting stuck on — the app must always work fully offline regardless of telemetry state |

**Cross-project convention:** `DO_NOT_TRACK=1` is honored by Netdata, Scarf's SDKs, and others —
honor it here too, in addition to our own dedicated env var.

**Distilled failure pattern across every controversy:** (1) data routed through a third-party ad
company, (2) a persistent ID derived from hardware, (3) silent rollout with docs arriving late,
(4) an opt-out that didn't actually work, (5) nagging users who opted out. This plan is designed
to avoid all five.

## 2. Decision: receiver

**Primary: PostHog Cloud, EU region.** No infrastructure to build, 1M events/month free tier
(expected volume: 30k-150k events/month at a few thousand instances pinging every ~24h — 7-30x
headroom), open-source core, EU data residency. Same choice made by n8n, Langfuse, and Stirling
PDF for the identical problem.

Required guardrails (must all be true before this ships, and covered by a startup smoke test):
- Enable "discard client IP" in the PostHog project settings.
- Every event also explicitly sends `$ip: null`, `$geoip_disable: true`, `$process_person_profile: false`.
- Dedicated PostHog project used for nothing else (no mixing with any other product's analytics).
- Billing limit set to $0 in the PostHog project so this can never generate a bill.
- API key is the public/write-only PostHog project key (meant to be embedded in client code — verify this is the "project API key" not a personal/secret key before embedding it in the published Docker image).

**Fallback (build only if PostHog's IP-discard cannot be verified in practice, or the maintainer
decides no third party should ever see the source IP): a custom AWS serverless receiver.**
Specified in full in section 3 so it can be built as a drop-in replacement with no client-side
schema change — the client posts a JSON payload to a URL either way.

**Explicitly rejected and why (do not re-litigate without new information):**
- Google Analytics Measurement Protocol — this is the exact mechanism that got Ultralytics in
  public trouble; "sent to Google" is disqualifying for a privacy-positioned tool.
- Segment — free tier caps at 1,000 monthly tracked users; every install_id counts as one MTU, so
  the free account would lock at ~1k installs, and it still needs a destination behind it anyway.
- Countly Flex — 500 MAU free ceiling, same lock-out problem.
- Scarf as the primary heartbeat — it counts pulls, not running instances (CI reruns, mirrors,
  and version-bump re-pulls all inflate the number), and its core value proposition is
  de-anonymizing which *company* is pulling, which is the wrong message to send a
  privacy-sensitive audience. Fine as an *additional*, later, backend-only, opt-in signal for
  pull-count trends — never the instance-liveness heartbeat, and never a frontend pixel.
- A plain EC2 box — no cost advantage over serverless at this volume, and adds real ongoing
  ops burden (OS patching, TLS cert renewal, backups, uptime monitoring) for zero benefit.

## 3. AWS fallback receiver — full spec (build only if PostHog is rejected per §2)

Everything below is expressed so it can be implemented as one CloudFormation or CDK stack,
checked into the repo (`infra/telemetry-receiver/` — new directory, does not exist yet), version
controlled like any other infra.

```
OpenTranscribe instance (backend container, Celery beat task)
  │
  │  HTTPS POST https://telemetry.opentranscribe.io/v1/ping
  │  (own subdomain, so it's a single documented hostname firewalls/network admins can block,
  │   and so the backend can be swapped later without any client release)
  ▼
Lambda Function URL  (NOT API Gateway — a bare Function URL has no extra per-request cost,
  saves the ~$0.10-0.15/month API Gateway would add, and skips the free-tier cliff for AWS
  accounts created after 2025-07-15 that lose the API Gateway free allowance)
  │  access logging OFF — the source IP must never be written to any log, ever
  ▼
Lambda "ingest" (Python 3.12, reserved concurrency = 5 as a hard abuse/cost ceiling)
  - reject any payload over 2 KB
  - validate against a strict allowlist JSON schema (unknown keys dropped, not stored)
  - never read, log, or forward `event["requestContext"]["http"]["sourceIp"]`
  - conditional put to DynamoDB (idempotent: one row per install_id per day)
  ▼
DynamoDB table "opentranscribe-telemetry-pings" (on-demand billing)
  PK = "day#YYYY-MM-DD"
  SK = install_id
  attrs = validated payload fields only
  TTL = insert_time + 400 days (raw pings auto-expire; aggregates below are kept longer)
  ▼
EventBridge Scheduler, weekly
  ▼
Lambda "digest"
  - query (never Scan) the last 7 and 30 day partitions
  - compute: distinct active install_ids, breakdown by app_version, by image_flavor (cuda/lite),
    by deployment_mode, by feature-enabled booleans
  - email the digest via SES to the maintainer
  - write aggregates.json to a public S3 path behind CloudFront, for a public stats page
    (Homebrew-style transparency — see §6)

Guardrails:
  - AWS Budget alarm at $5/month on this stack specifically (SNS → email)
  - Everything defined in IaC, no console-clicked resources
  - Expected cost at target volume: under $1/month (Lambda + DynamoDB both sit inside AWS's
    permanent free allowance at this scale; the Function URL has no additional charge)
  - Known limitation: nothing stops a bad actor sending fake pings (no auth on a public
    telemetry endpoint, by design — real installs have no credential to present). Treat the
    resulting numbers as directionally approximate, not exact. Schema validation + the
    concurrency cap keep the blast radius of abuse bounded and cheap.
```

## 4. Client-side design

### 4.1 Where it runs
- Backend only — a Celery beat task. The frontend SPA never phones home directly.
- Also mirror Sentry's air-gap pattern: any of the offline/PKI/FIPS deployment overlays
  (`docker-compose.offline.yml`, `--with-pki`, FIPS mode) **force telemetry off unconditionally**,
  regardless of the env var or DB setting — these deployments are explicitly for environments
  that must never make outbound calls.
- `HF_HUB_DISABLE_TELEMETRY=1` also gets added to the container environment. This is unrelated to
  our own heartbeat but the transparency doc (§6) should disclose it too — ML libraries (Hugging
  Face Hub, and potentially others) can phone home independently, and disclosure should be
  complete, not just "what we built."

### 4.2 Periodicity — the "best practice" the user specifically asked for
- **On backend startup**, always attempt one ping (subject to consent gate below) — this is how
  a version-change or first-install is detected quickly rather than waiting up to 24h.
- **Every 24 hours** thereafter via Celery beat, **with random jitter of ±1 hour** so that
  thousands of instances don't all hit the receiver at the same wall-clock minute (this is
  standard practice — Grafana uses the same 24h cadence; avoids a thundering-herd spike against
  the free-tier ceiling and against Lambda's reserved-concurrency cap in the AWS fallback).
- **No event-level pings** (i.e. do NOT ping on every transcription, every login, etc.) — that is
  the n8n/Langfuse pattern this plan explicitly avoids; it produces far more data than "is this
  instance alive" requires and increases both privacy exposure and hosted-tier cost risk.
- Single in-process de-dupe: if a ping has already succeeded within the last ~20 hours (covers
  jitter + backend restarts), skip — prevents a crash-loop from spamming the receiver.
- **Timeout: 5 seconds, no retries, fire-and-forget.** A network failure is logged at DEBUG level
  only and must never affect application behavior, never block startup, never raise. The app must
  function identically with the telemetry endpoint completely unreachable (this is the specific
  failure mode that caused Docker Desktop's offline-startup bug, docker/for-win — avoid it by
  construction: the ping call is wrapped in its own try/except and runs detached from request
  paths).

### 4.3 Identity
- A random `uuid4`, generated once on first backend startup, stored in a new `SystemSettings`
  key (or a dedicated `telemetry_installs` row — follow whatever the existing `SystemSettings`
  pattern in `backend/app/core/constants.py` / admin-UI-editable settings already uses; this repo
  already prefers DB-backed settings with coded defaults over new env vars per root CLAUDE.md).
- **Never** derived from MAC address, hostname, machine-id, or any other hardware/network fact —
  this is precisely the Ultralytics complaint (a hashed-MAC-derived ID sent to Google).
- Resetting is a single admin-UI action (regenerates the UUID) — useful for anyone who wants to
  disclaim prior history without fully opting out.
- The ID is a coarse liveness/counting token only. It is never linked to any other identifier
  (no email, no license key, no license domain — reject any future proposal that would add one,
  per the Langfuse email-domain scope-creep warning above).

### 4.4 Payload — fixed, versioned, minimal
```json
{
  "schema_version": 1,
  "install_id": "<uuid4>",
  "app_version": "0.5.0",
  "image_flavor": "cuda | lite",
  "arch": "amd64 | arm64",
  "deployment_mode": "dev | prod | fresh",
  "flags": {
    "pki": false,
    "gpu_scale": false,
    "ldap": false,
    "oidc": false
  },
  "gpu_count_bucket": "0 | 1 | 2+",
  "asr_provider_type": "local | cloud",
  "diarization_engine": "pyannote | diar-native",
  "llm_configured": true,
  "llm_is_local": true,
  "user_count_bucket": "1 | 2-5 | 6-25 | 26+",
  "files_last_7d_bucket": "0 | 1-10 | 11-100 | 100+",
  "uptime_days_bucket": "0 | 1-7 | 8-30 | 30+"
}
```
Every numeric fact that could otherwise be exact is bucketed. `schema_version` bump is required
any time a field is added or removed, in the same PR as a docs update (see §6) and a test
asserting the payload keys match the documented list exactly (prevents silent scope creep —
this is the enforcement mechanism for the Langfuse-style drift problem).

### 4.5 Never collected (explicit negative list — goes in the public docs verbatim)
IP addresses, hostnames, domains, URLs, usernames, emails (including email *domains*), file
names, transcript or audio content, speaker names, LLM endpoints/models/prompts/API keys, exact
(non-bucketed) counts or durations, error text, stack traces, any config value from `.env`.

### 4.6 Consent model — opt-in prompt, not silent opt-out default

The user explicitly asked for "clear opt in on startup" — this plan implements that as the
default consent model, following the Stirling PDF pattern (the closest self-hosted-Docker analog,
and the right choice given this project's privacy-sensitive audience for a transcription tool):

- **Nothing is sent until an admin has been shown a first-login notice** stating exactly what is
  collected (linking the docs page from §6) with two explicit choices: "Enable anonymous usage
  reporting" / "Keep disabled." Neither choice blocks using the app.
- If the notice has never been acknowledged (e.g. a fully headless install that never opens the
  admin UI), the app defaults to **disabled** — never sends without an affirmative choice. This is
  stricter than the opt-out-with-notice Homebrew model, and matches what the user asked for
  ("clear opt in on startup").
- **Never re-prompt or nag a user who chose "Keep disabled."** This is a direct requirement from
  the Homebrew brew#15719 lesson — a renewed prompt asking opted-out users to reconsider caused a
  second wave of community backlash on top of the original one.
- Startup log line always states the current status and exactly how to change it, regardless of
  which way it's set (`Anonymous usage telemetry: DISABLED. Enable in Settings > Privacy, or see
  docs/telemetry.md`).

### 4.7 Controls
- `OPENTRANSCRIBE_TELEMETRY=true|false` — env var **hard override**, wins over the DB setting and
  over the first-login prompt entirely (for scripted/headless deployments that want to pin the
  behavior at deploy time, e.g. `--fresh` test stacks should always pin this to `false`).
- Also honor `DO_NOT_TRACK=1` as an equivalent hard-off (cross-tool convention, per §1).
- Admin UI toggle (Settings → Privacy) backed by the DB `SystemSettings` — this is what the
  first-login prompt writes to.
- `OPENTRANSCRIBE_TELEMETRY_DEBUG=1` — logs the exact outgoing payload at INFO level and sends
  nothing over the network. Copied directly from Next.js's `NEXT_TELEMETRY_DEBUG` — the single
  best transparency mechanism found in the entire prior-art survey.
- Admin UI page: "Settings → Privacy → Show exactly what would be sent" — renders the live
  payload that *would* be sent right now, whether or not telemetry is enabled (mirrors Grafana
  Tempo's `/status/usage-stats`).
- `--fresh` deployments (per root CLAUDE.md's isolated-stack convention) and any CI test run must
  have telemetry forced off by default in their generated overlay — add this to the `--fresh`
  overlay generator alongside the other isolation work, and add a coverage entry to
  `backend/tests/unit/test_opentr_fresh_aux_isolation.py`'s exemption/isolation enumeration if a
  new overlay flag is introduced for this.

## 5. Implementation checklist (for whoever picks this up)

1. Decide receiver: PostHog Cloud EU project (§2) — create the project, enable IP-discard, set
   $0 billing cap, get the public project key.
2. Add `SystemSettings` keys: `telemetry_enabled` (bool, default False), `telemetry_install_id`
   (uuid, generated lazily), `telemetry_prompt_shown` (bool, default False).
3. Add the Celery beat task (`backend/app/tasks/` — follow existing task registration pattern,
   see `backend/app/tasks/CLAUDE.md`) that builds the §4.4 payload, checks all consent gates in
   order (env var override → `DO_NOT_TRACK` → DB setting), and POSTs with a 5s timeout,
   try/except around the entire call.
4. Add the first-login admin notice component (frontend) that reads `telemetry_prompt_shown` and
   renders once; writing either choice sets both `telemetry_enabled` and `telemetry_prompt_shown`.
5. Add `OPENTRANSCRIBE_TELEMETRY_DEBUG` handling and the "show exactly what would be sent" admin
   page.
6. Add `HF_HUB_DISABLE_TELEMETRY=1` to the backend/celery-worker container environment in
   `docker-compose.yml`.
7. Force-disable in `docker-compose.offline.yml` and any PKI/FIPS overlay.
8. Write the payload-schema-parity test (asserts the actual dict keys built by the task exactly
   match the documented list in the new docs page — fails the build if someone adds a field
   without updating docs, enforcing §4.4's "schema bump in the same PR" rule mechanically).
9. Write the docs (§6) — must land in the same PR as the feature, not after.
10. i18n: the first-login notice is user-facing copy — must land in all 12 locales per root
    CLAUDE.md's i18n requirement, `ar` RTL checked.
11. Announce via a GitHub Discussion before the release that ships this, so the community can
    react before it's live — direct mitigation of the Go telemetry backlash (golang#58894), which
    was driven as much by surprise as by the design itself.
12. CHANGELOG entry.
13. (Optional, later, separate PR): Scarf backend-only pull-count gateway, added only as an
    additional signal, never replacing the heartbeat, never a frontend pixel.

## 6. Docs-site page (write in the same PR, per §5 item 9)

New page: `docs-site/docs/operations/telemetry.md`, mirrored as `TELEMETRY.md` at the repo root
(discoverability — this is the file a security-conscious user checks first, so it shouldn't
require finding the docs site). Also add a short section + a link to it from the main README and
from `.env.example` next to `OPENTRANSCRIBE_TELEMETRY`.

Required content (this is the actual user-facing writeup, in plain language — modeled directly
on Homebrew's post-2023 Analytics page as the best example found in the survey):

- **One-paragraph summary at the top**: what it is, why it exists (6.8k Docker pulls with no
  visibility into real usage), that it's off by default and only turns on with an explicit choice.
- **Exactly what is sent** — the full field list from §4.4, in a table, with a real example
  payload (not just field names — an actual filled-in JSON example).
- **Exactly what is never sent** — the §4.5 list, verbatim, as its own clearly-labeled section
  (not buried at the end).
- **How the ID works** — a random UUID stored locally, never derived from hardware, resettable.
- **Where it goes** — name PostHog EU (or, if the AWS fallback was built instead, name the actual
  `telemetry.opentranscribe.io` endpoint and that it's self-hosted AWS infra under the project's
  control) — and how long it's retained.
- **How often** — the §4.2 schedule stated in plain terms ("once when the server starts, then
  about once a day").
- **How to see exactly what would be sent** — document `OPENTRANSCRIBE_TELEMETRY_DEBUG=1`.
- **How to turn it off** (it's off by default, but document re-disabling and the hard overrides):
  the admin UI toggle, `OPENTRANSCRIBE_TELEMETRY=false`, `DO_NOT_TRACK=1`, and the fact that
  offline/PKI/FIPS deployments always have it off regardless.
- **Link to the source** — the exact file(s) implementing this, so the claims in the doc are
  independently checkable against the code, not just asserted.
- **What we publish back** — if/when the digest aggregates (§3) are made public, link that page
  too; transparency in both directions.

## 7. GitHub tracking

File as a GitHub issue with this plan linked (this file), labeled `enhancement` + `backend` +
`docker` + `security` + `epic:platform-ops`, added to the org Roadmap project per root
CLAUDE.md's issue-tracking conventions (Status/Priority/Epic/Target fields set, not just labels).
