# api/endpoints — routes with no frontend caller

Moved intact from `backend/app/api/CLAUDE.md`. Router conventions and the per-route gotchas stay in `backend/app/api/CLAUDE.md`.

## Endpoints with no frontend caller — deliberate API surface

**This is a self-hosted app; the non-UI API is a feature.** Homelab users, cron scripts, CI and
AI agents drive it directly, so "no SPA call site" is not evidence an endpoint is dead.
`tests/unit/test_route_has_a_caller.py` xfails **78 of 441** route paths for having no frontend
caller (measured 2026-10-27; re-derive with that test rather than trusting the number); the
sections below say what they are *for*, so the xfail list reads as intent. Backend-first
routes whose UI is planned (support access #1122, the pipeline view #1224) carry their own
reason there.

The detector keys on path literals in `frontend/src`, which gives it two blind spots — check
both before believing it:

- **Server-provided URLs.** `set_file_urls` (`files/detail_response.py`) stamps `thumbnail_url` onto file
  responses, so the SPA consumes the field and never builds `/files/{uuid}/thumbnail`. That
  route is normally a presigned MinIO URL but falls back to the API path under `SKIP_S3` and on
  presign failure — genuinely reachable, invisible to the scan.
- **Last-segment keying.** A route scores on its final segment, so a sibling route absorbing
  the traffic looks like a caller for both. `waveform` appearing 58 times in the SPA is
  `GET /files/{uuid}/waveform`, **not** `/waveform/peaks` — `/peaks` really has zero callers and
  is a redundant second rendering of the same data. Same shape for `/files/{uuid}/retry` vs the
  `POST /my-files/{uuid}/retry` the SPA actually calls (`user_files.py`).

**Method matters and several of these are not GET.** `DELETE /tags/cleanup`, `DELETE /custom-vocabulary/all`, `PUT /search/models/neural/active`,
`DELETE /admin/users/{uuid}`, `DELETE /files/{uuid}/force`, and DELETE-only
`/speakers/combined-migration/progress` + `/speaker-attributes/migration/progress`. Generating
a client that assumes GET from the route table will collect 405s.

### One-time ops / maintenance tooling

Operator-invoked by curl or a runbook, not the SPA. All idempotent and safe to re-run unless
noted. Admin (`get_current_admin_user`) unless stated.

| Route | Notes |
|---|---|
| `POST /admin/imohash-recompute/start` · `GET .../status` | Dispatches Celery `recompute_all`; `already_running` guard, progress in Redis |
| `POST /admin/profile-embeddings/repair` | **Synchronous in-request** over every `SpeakerProfile` deployment-wide, and no `already_running` guard — unlike its Celery-dispatching siblings, concurrent calls stack |
| `POST /search/repair-indices` | Probes 4 indices, repairs only on failure. The speakers branch **recreates the index from Postgres**, so OpenSearch-only docs are lost |
| `POST /speakers/cleanup-orphaned-embeddings` | `get_current_active_user` — self-scoped by `user_id`, the only unprivileged search-index mutation here |
| `POST /files/management/cleanup-orphaned` · `GET /files/management/stuck` | Stuck-file recovery, **not** orphan cleanup — see Gotchas |
| `GET /admin/data-integrity/counts` | `run_orphan_cleanup(dry_run=True)` inline; read-only; the SPA's Data Integrity panel calls it |
| `GET /admin/data-integrity/storage/{counts,status}` · `POST /admin/data-integrity/storage?confirm=true[&force=true]` | Storage-object orphan sweep (#1212; `admin_storage_integrity.py` + `tasks/storage_orphan_sweep_task.py`). Dry run first; the POST refuses without `confirm=true`, dispatches Celery `storage_orphan_sweep` and audits `admin.data_integrity.storage_purge` (request, then completion with counters only). Guards mirror the index sweep: empty Postgres refused (force cannot override), 10%-of-objects ratio guard (`force`), per-file Postgres re-check, 1 h grace period |
| `POST /admin/media-duration-backfill` | super_admin; dispatches `recovery.media_duration_backfill` (issue #969), `dry_run=true` default |
| `/speakers/combined-migration/{start,status,stop}` · `DELETE .../progress` | super_admin; `stop` revokes in-flight batches |
| `DELETE /speaker-attributes/migration/progress` | super_admin; clears stale Redis progress, refuses while running |
| `GET /embeddings/migration/mode` | super_admin; pure config read (declares an unused `db` dependency) |
| `POST /tasks/system/fix-file/{uuid}` | Per-file version of the stuck-file fix; admin sees any file |
| `GET /tags/unused` · `DELETE /tags/cleanup` | `/cleanup` is caller-scoped by default; `scope=all_users` needs `confirm=true` and, in a multi-tenant deployment, a super_admin (`ctx.bypass.deployment_scope_writable`). See Gotchas |
| `POST /files/waveforms/generate` · `GET /files/waveforms/status` | Bulk waveform backfill + coverage. Admin via an **inline** `is_admin` check, not a dependency |
| `POST /files/{uuid}/waveform/generate` · `POST /files/{uuid}/analytics/refresh` | Per-file recompute |
| `DELETE /files/{uuid}/force` · `POST /files/{uuid}/{retry,recover,cancel}` | Recovery verbs on `files/management.py`; see the `get_current_user` gotcha |
| `GET/POST /admin/settings/media-sources` · `PUT/DELETE .../{source_id}` | Deployment-wide protected-media credentials. Superseded for the SPA by the per-user `/user-settings/media-sources` |

`/admin/hf-token` (GET/PUT/DELETE, `POST /test`) is the one place the platform Hugging Face token is
set: super_admin (`get_current_active_superuser`) under the `engine.settings` capability, write-only
(`{is_set, source}` is all any route returns), fixed-sentence test results, audit event
`admin.settings.change` with the action and never the value.

`/asr-settings/local-model/{active,set,restart}` live in `endpoints/asr_local_model.py` and sit behind
`asr.model_selection`, not `asr.user_providers` like the rest of `/asr-settings` (#1207): a deployment without
bring-your-own ASR keys still lets its super_admin pin the local model. Same prefix and paths, a second
`include_router_with_consistency` in `router.py`.

`/admin/asr-providers` (GET/PUT, super_admin, capability `asr.user_providers`) is the cloud-ASR
provider policy (#1246): which providers plain users may pick, whether they may add their own
keys, and the admin-shared configurations (flags only, listed for the caller's active
organisation) that carry the credentials. The decision itself lives in the pipeline plan; see
`services/asr_provider_policy.py`.

`/admin/local-models` (#1246; `GET ""`, `POST /download`, `POST /download/cancel`) is the local Whisper
model library: super_admin only (`engine.settings`, in `SUPER_ADMIN_PREFIXES` of `tests/unit/test_route_privilege_tiers.py`), 409
`local_asr_unavailable` on lite, catalog ids only (the repo and every path derive from the catalog).
One download at a time deployment-wide (Redis lock taken here before dispatch; an unreachable Redis is a
503, fail closed). Errors are `{"code", "message"}` with the fixed sentences in
`services/local_models/downloader.ERROR_MESSAGES`. Activation stays `/asr-settings/local-model/{set,restart}`:
`/set` writes the DB only (running workers are pinned); `/restart` sends a warm shutdown **by name** to
the workers in `core/constants.WHISPER_WORKER_PREFIXES` (never `gpu-diarize@`, never a broadcast to all;
nobody answering is 409 `gpu_worker_not_found`) and returns `workers` + `restart_requested_at`;
`GET /local-model/active` carries `loaded_models` (`{worker, model, ready_at}`, written by each worker's
`preload_models` via `services/local_models/worker_report`). A switch is confirmed when every worker named in
the restart response reports the new model with `ready_at` after `restart_requested_at`; never wait on a worker
that is not in that list (a recreated container's old record lingers up to a week).

### Compliance

Legally significant, audit-logged in the service layer, and deliberately without a
one-click UI button.

| Route | Authority |
|---|---|
| `POST /admin/gdpr/erase-user/{uuid}` | super_admin. `erase_user` cascades storage + OpenSearch + rows, then **deletes the account** |
| `POST /org-admin/gdpr/erase-user/{uuid}` | `require_org_admin` + `require_capability("organizations")`, refused under a support grant, re-auth in the body. **Schedules** (202, #1105, `org_admin_erasure.py`); when it runs it destroys only rows stamped with **this** org; the account and personal-scope data survive |
| `POST /org-admin/gdpr/erase-organization?confirm=true` | Same gate; refuses without `confirm`. Schedules the irreversible whole-tenant erasure; members keep their accounts |
| `GET /org-admin/gdpr/erasures`, `POST /org-admin/gdpr/erasures/{uuid}/cancel` | Same gate. The org's open scheduled erasures; any org admin cancels before `scheduled_for` (409 after) |
| `GET /org-admin/audit-logs` | Same gate; scoped to org members, and a `user_id` (actor) or `target_user_id` (subject, issue #443/#828) filter outside the org is 403 either way. Also `user_uuid` / `target_user_uuid` (same guard; an unknown uuid and a non-member's answer identically), `resource_type`, `resource_uuid`. Each event gains `actor_display` / `target_display` / `resource_display`, resolved at read time **inside the org only** (`services/org_audit_view.py`), never stored. `org_admin.org_audit_query` is the one place the scope and guards are applied |
| `GET /org-admin/audit-logs/export?format=csv\|json` | `require_org_admin` + server-enforced `org.audit_logs` (404 when off, no superuser bypass, 403 under a support grant). Same scope and filters as the read via `org_audit_query`; capped at `AUDIT_EXPORT_MAX_ROWS` (10,000, `X-Export-Truncated`); rendered by `auth/audit_export.py`, the writer `GET /admin/audit-logs/export` now shares. Rate limited, and audited as `audit.export`. `endpoints/org_admin_audit.py` |
| `GET\|PATCH /org-admin/policy` · `POST /org-admin/policy/purge` | `require_org_admin` + server-enforced `org.defaults` (404 when off, no superuser bypass, 403 under a support grant; the key is off in community, so 404 there). Scope is ALWAYS `ctx.org_id`, there is no org parameter. Voiceprint and gender switches plus the stored redaction floor (`redaction_floor` is null, and PATCHing it 404, while `redaction.policy` is off); audited `org.policy.change` with field NAMES only. `endpoints/org_admin_policy.py`, logic in `services/privacy/policy_admin.py` |
| `GET /privacy/effective` · `GET\|PUT /privacy/me` · `POST /privacy/me/purge` · `GET /privacy/purge/{task_id}` | Any signed-in user. `effective?file_uuid=` is access-checked and answers 404 for a file the caller cannot see (403 and 404 both read as 404). `PUT /me` is 409 `policy_locked` while a higher layer is off. A purge status is readable only by who started it. `/me*` and the status refuse a support grant. `endpoints/privacy.py` (`user_router`) |
| `GET\|PUT /admin/privacy-policy` · `POST /admin/privacy-policy/purge` | `get_current_active_superuser`; `/admin` refuses a grant by prefix. The instance switches (`privacy.*` in `SystemSettings`, audited `admin.settings.change`). Purge `scope="all"` is 403 `purge_scope_not_allowed` unless `tenancy_mode(db)` is `SINGLE` (the #1122 test) |
| `GET\|POST /users/me/erasure-request` · `DELETE /users/me/erasure-request/{ledger_uuid}` | Any signed-in user, refuses a support grant. The **subject's own** erasure (issue #1354), which is why it has a UI while operator erasure deliberately has none. Off by default (POST/DELETE answer 404 while off). POST needs `confirm: "ERASE"` plus the password or a TOTP code (409 `reauth_unavailable` for an SSO-only account without MFA), is rate limited, and answers 409 `last_super_admin` / `request_exists`; DELETE of another user's uuid is a 404, after the schedule 409 `already_executing`. Returns the request receipt (ledger uuid, times, sha256 digest). `endpoints/self_erasure.py`, logic in `services/self_erasure_service.py` |
| `GET\|PUT /admin/self-erasure` | `get_current_active_superuser`; the switch (`gdpr.self_erasure_enabled`) and the grace period (`gdpr.self_erasure_grace_days`, 0 to 21), audited `admin.settings.change` |
| `GET /admin/files/quarantined` · `POST /admin/files/{uuid}/{quarantine,release}` | Admin. DMCA/abuse takedown + legal hold; `release` is 409 when not quarantined. No capability gate and no support grant (the operator must always be able to act) |
| `/admin/takedown-notices` (`POST`, `GET`, `GET /export`, `GET /{uuid}`, `PATCH /{uuid}/files/{file_uuid}`, `POST /{uuid}/notify`) | Admin. The durable notice record (issue #1254): every quarantine cites a notice or states an operator action. `export` is capped, formula-neutralised and audited; `notify` takes no recipients. Counterpart `GET /takedown-notices/mine` is the signed-in owner's own history (no basis, reference or handler). `endpoints/admin_takedown_notices.py`, `takedown_notices_user.py` |
| `GET /org-admin/files/quarantined` · `POST /org-admin/files/{uuid}/{quarantine,release}` | `require_org_admin` + server-enforced `org.takedown` (404 when off, no superuser bypass). Own-org files only (looked up with `organization_id` IN the query, so foreign / personal / missing are one 404). **Platform wins**: an org cannot release or overwrite a takedown the operator placed (`takedown_platform_origin`). Never places a legal hold. `endpoints/org_admin_takedown.py` |
| `GET/POST /org-admin/holds` · `GET /org-admin/holds/{uuid}` · `POST /org-admin/holds/{uuid}/release` | `require_org_admin` + server-enforced `org.legal_hold` (404 when off, no superuser bypass, 403 under a support grant). Files and collections are resolved with `organization_id == ctx.org_id` IN the query, so foreign / personal / missing are one 404, as is another organization's hold. Placing and releasing share the `ORG_HOLD_RATE_LIMIT` budget. The reason is returned to org admins only, never audited. `endpoints/org_admin_holds.py`; the logic is `services/content_hold_service.py` and every destroy path asks `services/hold_guard.py` |
| `GET/POST /admin/holds` · `GET /admin/holds/{uuid}` · `POST /admin/holds/{uuid}/release` | `get_current_admin_user` (`admin` and `super_admin`), no capability (an operator must always be able to act on a legal demand), 403 under a support grant. The operator's legal hold WITHOUT a takedown (v467): a `content_hold` row with `authority = 'platform'`, on any tenant's files or collection (missing is a 404). Only platform holds are reachable here (an organization's hold is a 404 to read and release, never listed); the org routes never show a platform hold. The GETs call `audit_metadata_access`. Placing and releasing share the `ADMIN_HOLD_RATE_LIMIT` budget. The reason is returned to instance admins only, never audited. `endpoints/admin_holds.py`; records and lookups `services/content_hold_records.py`, writes `services/content_hold_service.py` |
| `GET/PUT /admin/audit/content-access` | `get_current_active_superuser`, capability `audit.logs`. The super_admin switch for the opt-in `content.*` audit events (default off); `GET` also reports `capability_available` / `active` and the storage estimate. Audited as `admin.settings.change`. `endpoints/admin_audit_content_access.py` |
| `GET /admin/security/audit-integrity` | `get_current_active_superuser`, capability `audit.logs`. The MEASURED state of audit-record protection for the connection's database role (`db/role_check.measure_role`: `enforced`, superuser/owner flags, per-table privileges and guard-trigger presence; no role names). Read-only; classified `settings` in the platform access matrix. `endpoints/admin_audit_integrity.py` |

An org admin has authority over the tenant's **data**, never over the **person's account** —
that boundary is why there are two erasure entry points rather than one.

### Monitoring / diagnostics

Scrape targets for a homelab dashboard or an operator shell. All read-only.

`GET /admin/stats` (psutil + DB aggregates + recent tasks) · `GET /admin/timing`,
`/admin/timing/{task_id}` (merges live Redis with the persisted row), `/admin/timing-summary/recent` ·
`GET /admin/gpu-profiles` (Redis profile history) · `GET /admin/engine-settings/metrics`
(per-worker queue depth; **super_admin**, unlike its admin-gated `/admin/*` siblings — a plain
admin gets 403 here and 200 on `/admin/stats`) · `GET /admin/auth-config/status` (super_admin,
per-method enabled flags) · `GET /speaker-clusters/stats` (`get_current_active_user`, self-scoped
counts + coverage) · `GET /files/{uuid}/subtitles/validate` (timing-issue report).

For live per-stage transcription progress use `GET /tasks/progress/active` (Redis); `GET
/tasks/{task_id}` reports the persisted task row's progress, which is coarser but survives a
restart — see Gotchas.

### Automation-facing

Shaped for a script or agent rather than a screen.

| Route | Purpose |
|---|---|
| `GET /files/{uuid}/info` | Lightweight identity/size/duration/language/status with no transcript — the integration read |
| `GET /files/{uuid}/status-detail` | Status + stuck detection + retry counters + `active_task_id`. Mixed shape: also carries English `recommendations` |
| `GET /files/supported-formats` | Static `{"subtitle_formats": [...]}`. **No auth dependency at all** — the only ungated route in that router |
| `GET /files/{uuid}/subtitles` | Renders srt/webvtt/txt as an attachment with redaction applied. The player's track is the sibling `GET /files/{uuid}/captions` (inline WebVTT, transcript-READ gating, `files/captions.py`); both cut cues with `services/captions`. **409 while the file's redaction scan is incomplete** — see Gotchas |
| `GET /usage/me` · `GET /usage/me/daily` | Per-user LLM token/cost reporting. **Core, not a cloud seam** — `usage.py`'s docstring is explicit that a self-hoster paying an OpenAI bill is the intended reader. Reporting only; quotas and billing are the part that was carved out. The only usage UI in this repo is the `isCloudEdition`-gated managed-edition stub, so the self-host panel these promise does not exist yet |
| `GET /prompts/by-content-type/{type}` | System + user prompts for one of 6 content types |
| `GET /search/filters` | Available filter facets from OpenSearch aggs; degrades to empty lists when the index is absent |
| `GET /asr-settings/local-models` | Scans the HF cache for usable faster-whisper repos |
| `GET /custom-vocabulary/export` · `DELETE /custom-vocabulary/all` | JSON export (attachment) and bulk delete. The **listing** is `GET /custom-vocabulary`, not `/all` |
| `GET /files/youtube/quota` | Sliding-window quota pre-flight; `-1` means unlimited |
| `POST /files/batch-extract` | Queues topic extraction across many files (202 + task id) |
| `POST /files/{uuid}/identify-speakers` | Queues the LLM speaker-ID task. Suggestions are **never auto-applied** |
| `GET /speaker-profiles/collections` · `POST` same path | Vestigial: functional, but the router has no member-management routes to go with it |

### Model administration

**Deployment-wide mutating routes take `ctx.bypass.require_deployment_scope("<Operation>")`** (403 for a
plain admin in a multi-tenant deployment, any admin in a single-tenant one, `super_admin` always;
never conferred by a support grant): `POST /search/{reindex,reindex/stop,repair-indices,models,
reembed-degraded}`, `POST /search/models/neural/{model}/{register,deploy,undeploy}`, `PUT
/search/models/neural/active`, `POST /admin/{data-integrity,data-integrity/storage,
embedding-consistency/repair,embedding-consistency/stop,profile-embeddings/repair,
imohash-recompute/start,settings/cache-config/clear,settings/retention-config/run}`. The matrix
category is `deployment-scope`; `tests/api/test_deployment_ops_gating.py` is the per-route proof
(add a new such route to its `OPS` table). Read-only status/count routes stay plain-admin.

OpenSearch neural-search lifecycle, admin-gated and idempotent (`already_registered` /
`already_deployed`): `POST /search/models/neural/{model}/{register,deploy,undeploy}`.
Read state via `GET /search/models/neural/status` (which also carries the `embedding_provenance`
survey — the one query answering whether the index is a single comparable vector space) or
`GET /search/models/neural`.
**`/search/models/neural/active` is `PUT`-only and triggers a full reindex** — there is no GET.
It and `POST /search/models` are now **the same implementation**
(`services/search/model_switch.py`): they were two halves of one job, neither of which switched
anything (#437). "Full reindex" means **one coordinator per owner of a COMPLETED file** — a
per-caller dispatch left every other user's chunks in the previous model's vector space. Both
answer **409** for a model that is not registered *and* deployed, because recording a selection
whose pipeline cannot emit the new dimension makes the coordinator delete the chunks index and
then fail every write. Detail: `backend/app/services/search/CLAUDE.md`.
User administration: `GET /users` and `DELETE /admin/users/{uuid}` (creation is
`POST /admin/users`; role/lock/reset live on their own sub-paths).

### Debug

`GET /speakers/debug/cross-media-{data,by-name}` — replay the cross-media speaker-matching
decision for an operator diagnosing a bad match. **Keep them; they are correctly
`get_current_admin_user`-gated.** Two asymmetries worth knowing: `cross-media-data` is *also*
self-scoped to `current_user.id` (so an admin cannot inspect another user's data with it), while
`cross-media-by-name` relies on the admin gate alone and returns every user's speakers. The
latter also carries three `if not current_user.is_admin:` branches that are unreachable under an
admin-only dependency.

### UI-only, no caller found

Real UI features whose call site the scan missed or which have no panel yet:
`/files/{uuid}/thumbnail` (server-provided URL, above) · `/files/{uuid}/{apply,auto-label}` ·
`/speaker-profiles/profiles/{uuid}/occurrences`, `/speaker-profiles/speakers/{uuid}/{assign-profile,suggestions}` ·
`/speakers/{uuid}/verify` · `/files/{uuid}/waveform/peaks` (redundant with `/waveform`).
