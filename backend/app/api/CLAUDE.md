# backend/app/api — FastAPI routers, endpoints, WebSocket

## Purpose

Every HTTP/WS surface. Endpoints do auth gating, tenant scoping, and response shaping;
business logic belongs in `app/services`, pipeline work in `app/tasks`.

## Key files

- `router.py` — the single mount point. Register new routers here via
  `include_router_with_consistency(router, prefix, tags, capability=...)`, which normalizes
  the prefix (trailing-slash parity for nginx) and optionally attaches a capability gate.
  `main.py` mounts `api_router` under `settings.API_PREFIX` (`/api`).
- `endpoints/auth/` — **also the dependency-injection module.** `get_current_user`,
  `get_current_active_user` (~31 call sites), `get_current_admin_user`,
  `get_current_active_superuser`, `get_optional_current_user` are defined in
  `endpoints/auth/dependencies.py` and re-exported from the package — there is no `deps.py`,
  and `from app.api.endpoints.auth import get_current_active_user` still works. The rest of
  the package is one module per flow (`login`, `registration`, `profile`, `oidc`, `pki`,
  `methods`, `mfa` + `mfa_tokens`, `sessions`), each owning its own `APIRouter` that
  `__init__.py` includes in declaration order.
- `deps_context.py` — tenant-aware DI: `get_current_context` → `RequestContext(user, org_id,
  org_role)`, `require_org_admin` (the **server-side** authority for org-admin actions — the
  frontend capability check is cosmetic), and `scope_to_context(query, model, ctx)`.
- `websockets.py` — `/ws`, the in-process `ConnectionManager`, and the Redis subscriber on the
  `websocket_notifications` channel.
- `endpoints/metrics.py` — `/metrics`, mounted at **root** (no `/api`), unauthenticated by design.
- `endpoints/scim/` — SCIM 2.0, also mounted at **root** (`/scim/v2`, RFC 7644 §3.1 fixes the
  base path). Bearer-token authenticated, not session-authenticated, and deliberately **not**
  rate limited; its errors are SCIM Error resources via `main.py`'s `SCIMError` handler.
- `endpoints/chat/` — conversations, messages, export, projects, plus the user and admin
  settings routers. **`projects.router` is included BEFORE `conversations.router`**: both live
  under `/chat` and FastAPI matches in registration order, so `/chat/projects` would otherwise
  be shadowed. The admin settings router requires `get_current_admin_user` on **both** GET and
  PUT — the UI tab is cosmetic, that dependency is the authority.
- `endpoints/files/` — the oversized files router split into a package (`upload`, `crud`,
  `filtering`, `streaming`, `subtitles`, `reprocess`, `url_processing`, `waveform`, …).
  `management.py` exports a second router mounted at the same `/files` prefix.

## Conventions / patterns

- **Path params are UUIDs, never DB integers** (`/{file_uuid}`, `/{speaker_uuid}`). Resolve via
  `app/utils/uuid_helpers.py` — `get_file_by_uuid_with_permission` also applies the takedown
  and tenant gates, `require_resource_owner` replaces copy-pasted 403 checks.
- Guard with `Depends(get_current_active_user)` / `get_current_admin_user`. `role` is the
  authorization truth; `is_superuser` is only its derived mirror.
- Endpoints raise `fastapi.HTTPException` **directly** — deliberately NOT `core/exceptions.py`,
  which is reserved for the service layer (see its module docstring).
- Responses carry **pre-formatted display fields** built by `services/formatting_service.py`
  (`formatted_duration`, `display_status`, `resolved_speaker_name`, …). The SPA renders them.
- Read surfaces mask transcript text at read time via `services/redaction/spans.py`; owner
  reveal is `?redact=false`, audited as `transcript.view_unredacted` by **one** implementation,
  `files/crud.py::audit_unredacted_reveal`, called from the transcript read and from
  `files/subtitles.py` (`surface` distinguishes them). The export half was missing entirely
  until #85 — the more consequential of the two, since that reveal leaves the application as a
  file on disk — and this file claimed both were audited until it was checked. Two copies of a
  compliance trail is how one of them silently stops matching the other.
- **Owner-scoped listings go through `PermissionService.get_accessible_file_ids_subquery`** —
  it already covers owned files plus collections shared directly and via groups, and applies the
  org tenant gate. Never write a second sharing rule beside it.

## Endpoints with no frontend caller — deliberate API surface

**This is a self-hosted app; the non-UI API is a feature.** Homelab users, cron scripts, CI and
AI agents drive it directly, so "no SPA call site" is not evidence an endpoint is dead.
`tests/unit/test_route_has_a_caller.py` xfails **78 of 374** routes for having no frontend
caller; the sections below say what those 78 are *for*, so the xfail list reads as intent.

The detector keys on path literals in `frontend/src`, which gives it two blind spots — check
both before believing it:

- **Server-provided URLs.** `set_file_urls` (`files/crud.py`) stamps `thumbnail_url` onto file
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
| `GET /admin/data-integrity/counts` | `run_orphan_cleanup(dry_run=True)` inline; read-only |
| `/speakers/combined-migration/{start,status,stop}` · `DELETE .../progress` | super_admin; `stop` revokes in-flight batches |
| `DELETE /speaker-attributes/migration/progress` | super_admin; clears stale Redis progress, refuses while running |
| `GET /embeddings/migration/mode` | super_admin; pure config read (declares an unused `db` dependency) |
| `POST /tasks/system/fix-file/{uuid}` | Per-file version of the stuck-file fix; admin sees any file |
| `GET /tags/unused` · `DELETE /tags/cleanup` | `/cleanup` is caller-scoped by default; `scope=all_users` needs `confirm=true`. See Gotchas |
| `POST /files/waveforms/generate` · `GET /files/waveforms/status` | Bulk waveform backfill + coverage. Admin via an **inline** `is_admin` check, not a dependency |
| `POST /files/{uuid}/waveform/generate` · `POST /files/{uuid}/analytics/refresh` | Per-file recompute |
| `DELETE /files/{uuid}/force` · `POST /files/{uuid}/{retry,recover,cancel}` | Recovery verbs on `files/management.py`; see the `get_current_user` gotcha |
| `GET/POST /admin/settings/media-sources` · `PUT/DELETE .../{source_id}` | Deployment-wide protected-media credentials. Superseded for the SPA by the per-user `/user-settings/media-sources` |

### Compliance

Legally significant, audit-logged in the service layer, and deliberately without a
one-click UI button.

| Route | Authority |
|---|---|
| `POST /admin/gdpr/erase-user/{uuid}` | super_admin. `erase_user` cascades storage + OpenSearch + rows, then **deletes the account** |
| `POST /org-admin/gdpr/erase-user/{uuid}` | `require_org_admin` + `require_capability("organizations")`. Destroys only rows stamped with **this** org; the account and personal-scope data survive |
| `POST /org-admin/gdpr/erase-organization?confirm=true` | Same gate; refuses without `confirm`. Irreversible whole-tenant erasure; members keep their accounts |
| `GET /org-admin/audit-logs` | Same gate; scoped to org members, and a `user_id` filter outside the org is 403 |
| `GET /admin/files/quarantined` · `POST /admin/files/{uuid}/{quarantine,release}` | Admin. DMCA/abuse takedown + legal hold; `release` is 409 when not quarantined |

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
| `GET /files/{uuid}/subtitles` | Renders srt/webvtt/txt as an attachment with redaction applied; the player builds its own VTT client-side, hence no SPA caller. **409 while the file's redaction scan is incomplete** — see Gotchas |
| `GET /usage/me` · `GET /usage/me/daily` | Per-user LLM token/cost reporting. **Core, not a cloud seam** — `usage.py`'s docstring is explicit that a self-hoster paying an OpenAI bill is the intended reader. Reporting only; quotas and billing are the part that was carved out. The only usage UI in this repo is the `isCloudEdition`-gated managed-edition stub, so the self-host panel these promise does not exist yet |
| `GET /prompts/by-content-type/{type}` | System + user prompts for one of 5 content types |
| `GET /search/filters` | Available filter facets from OpenSearch aggs; degrades to empty lists when the index is absent |
| `GET /asr-settings/local-models` | Scans the HF cache for usable faster-whisper repos |
| `GET /custom-vocabulary/export` · `DELETE /custom-vocabulary/all` | JSON export (attachment) and bulk delete. The **listing** is `GET /custom-vocabulary`, not `/all` |
| `GET /files/youtube/quota` | Sliding-window quota pre-flight; `-1` means unlimited |
| `POST /files/batch-extract` | Queues topic extraction across many files (202 + task id) |
| `POST /files/{uuid}/identify-speakers` | Queues the LLM speaker-ID task. Suggestions are **never auto-applied** |
| `GET /speaker-profiles/collections` · `POST` same path | Vestigial: functional, but the router has no member-management routes to go with it |

### Model administration

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
`/speaker-profiles/profiles/{uuid}/occurrences`, `.../assign-profile`, `.../suggestions` ·
`/speakers/{uuid}/verify` · `/files/{uuid}/waveform/peaks` (redundant with `/waveform`).

## How it connects

Pydantic models in `app/schemas`, logic in `app/services`, dispatch into `app/tasks`.
See `backend/CLAUDE.md`, `backend/app/auth/CLAUDE.md`, `backend/app/services/CLAUDE.md`,
`backend/app/tasks/CLAUDE.md`.

## Gotchas

- `require_capability(key)` (`core/capabilities.py`) returns **404, not 403** — a gated router
  must look like an unknown route. Platform superusers bypass it.
- **`endpoints/tags/` is a package** (`crud` · `discovery` · `sharing` · `operations` + `_common`),
  split like `endpoints/files/`. Every module registers onto the ONE router in `_common` —
  sub-routers were tried and rejected because FastAPI refuses an empty path when including a
  router with no prefix, and `POST ""`/`GET ""` are real routes. **Import order in
  `__init__` is load-bearing**: `/for-files`, `/impact` and `/promote` are literal paths that
  must register before the `/{tag_uuid}` routes. Do NOT re-export third-party names through
  `_common` — ruff prunes them as unused there and the backend stops booting.
- **The tag plane has THREE scopes, and mixing them up is the bug** (`v374_add_tag_user_id`).
  All three live in `services/tag_service.py` so nothing re-derives them:
  | Scope | Rule | Used by |
  |---|---|---|
  | `visible_to()` | system **or** owned **or** explicitly shared (`v386`) **or** attached to an accessible file | `GET /tags` (the list) |
  | `owned_or_system()` | system **or** owned | resolution, `/unused`, `/collisions` |
  | `_writable_tag_ids()` | owned always; system for an **admin**; else **404** | every mutation |
  Reading and rewriting are different rights: `visible_to` admits a tag on a file shared with
  you, but renaming it rewrites its owner's vocabulary everywhere. Narrowing the *list* to
  `owned_or_system` silently drops shared-file tags from the recipient's picker; widening
  *mutation* to `visible_to` lets anyone rename anyone's tag. The non-writable case answers
  404, not 403, so probing cannot enumerate other accounts' tags.
- **Tags are per-owner; the shared tier is `user_id IS NULL`.** `resolve_or_create_tag` takes a
  **required** `user_id` — an ownerless row is a *system* tag, published to every account, and
  only `initial_data._ensure_default_tags` may create one. Lookup prefers the caller's row, then
  a same-named system row, so applying a seeded default attaches the shared row. `POST
  /tags/promote` (admin) moves an owned tag into that tier and folds same-named rows into it.
  Names are unique only **per owner**, so `remove_tag_from_file` resolves via `FileTag` for that
  file, never by name.
- **One file never carries the same word twice — but only where `file_id` is passed.**
  `resolve_or_create_tag(..., file_id=...)` consults `lookup_tag_on_file` first, so the second
  person to tag a shared file reuses the row instead of forking a same-named one. Without it the
  detail page renders the tag twice and the gallery's ALL-filter has to count `DISTINCT Tag.name`
  to compensate. **Verified Aug 2026: `services/auto_label_service.py` is the ONLY caller that
  passes it.** The interactive path `POST /tags/files/{uuid}/tags` → `tags/_common._resolve_tag`
  omits it, and so does `services/tag_bulk.py`, so those dedupe by tag *id* on `FileTag` — a
  repeat post by one user is idempotent, but two users can attach same-named rows of their own to
  one shared file. Treat this as an invariant the auto-label path upholds and the human path does
  not, not as a global guarantee.
- **A tag share grants vocabulary, not administration** (`v386_add_tag_share`). `tag_share`
  mirrors `collection_share` — one user or one group, CHECK-constrained, partial unique indexes —
  but deliberately has **no permission column**: the recipient can see, filter by and apply the
  tag while rename/merge/delete stay with the owner, so a `viewer`/`editor` distinction would be
  a field pretending to be a choice.
- **Bulk tag paths must stay batched.** `prepare_upload.add_tags_to_file` goes through
  `resolve_or_create_tags` (constant SELECTs, issue #284 A2.8), not the per-name resolver;
  `test_upload_prep_batching` fails if the per-name loop returns.
- **`DELETE /tags/cleanup` defaults to the CALLER's tags; the deployment-wide sweep must be asked
  for twice.** It used to delete every owned tag (`user_id IS NOT NULL`) unreferenced by any
  `FileTag` across all users and orgs, while its inspection sibling `GET /tags/unused` is
  caller-scoped — so an admin who read the list and then called `/cleanup` irreversibly deleted
  rows they were never shown, with no impact preview and no parameter that could have warned them
  (#431). Now: `scope=mine` (**the default**) sweeps only the caller's rows; `scope=all_users`
  needs `confirm=true` as well or it is a **400**, the same double opt-in as
  `POST /org-admin/gdpr/erase-organization`. The response gained `scope`; `deleted_count` and
  `message` are unchanged. The sweep lives in `services/tag_operations.cleanup_unreferenced_tags`
  and measures "unreferenced" **globally**, not against accessible files — an owned tag can sit on
  a file the owner can no longer see, and deleting it would strip the tag off someone else's file,
  so `/unused` may list a tag `/cleanup` declines to delete. System tags are exempt in both
  scopes. Still gated by an **inline** `is_admin` check rather than a dependency, so a
  dependency-based authz audit does not see it.
- `GET /api/auth/session` must **never 401** (200 for anonymous); it is the SPA's session probe.
- **`GET /tasks` and `GET /tasks/{task_id}` read the `task` table (fixed in #431) — and accept
  TWO id forms.** #76 had repointed both at `MediaFile` while every writer stayed on `task`, so
  `progress` was hardcoded `0.0 / 0.5 / 1.0`, `error_message` was the literal
  `"Transcription failed"`, and `task_type` was always `"transcription"` (making
  `?task_type=summarization` unmatchable). Anything polling saw a constant `0.5`, which
  `TasksGrid.svelte` rendered as a permanently half-full bar. Both now join the file's real task
  row — `active_task_id` first, else the newest — so ids are **real Celery ids** and feed
  `POST /tasks/system/recover-task/{task_id}` directly. A file with no task row yet keeps the
  legacy `task_<media_file_id>` id and reports `progress` `0.0`/`1.0` with the file's own
  `last_error_message`; **never reintroduce a synthesized mid-point.** `get_task` accepts either
  form. `GET /tasks/progress/active` (Redis `ProgressTracker`) remains the live per-stage feed.
  Related: `list_tasks` no longer swallows exceptions into an empty successful page — a failed
  query is a 500, because an empty 200 is indistinguishable from "this user has no tasks". Note
  `list_tasks` has a `status` **query param that shadows `fastapi.status`**, so raise with a
  literal code inside that handler.
- **Depend on `get_current_active_user`, never `get_current_user`, on an ordinary route** —
  and `tests/unit/test_lifecycle_gate_coverage.py` now enforces it by walking every route's real
  dependency tree. `get_current_user` answers "is this credential valid?"; the *lifecycle* gate
  (deactivated, unapproved, rejected, expired, `must_change_password`, unacknowledged banner)
  lives only in `get_current_active_user`, so depending on the credential layer silently opts a
  route out of all of it. 30 routes were doing exactly that (#431). The big one was **`deps_context.get_current_context`** —
  the credential entry point for ~100 routes (all of chat, org-admin, tags, collections,
  comments, search, upload prepare/cancel) — which meant an expired or force-password-change org
  admin could reach `POST /org-admin/gdpr/erase-organization`. `get_current_admin_user` and
  `get_current_active_superuser` were always correct; they chain through the gate.
  **Remedy-for-its-own-gate routes need no waiver**: the exemption lives *inside* the gate as a
  route-template check (`PASSWORD_CHANGE_EXEMPT_PATHS` / `BANNER_EXEMPT_PATHS`), which survives a
  dependency refactor, so `PUT /users/me` and `POST /auth/banner/acknowledge` depend on the gate
  and are let through by it. Only three waivers are legitimate, all listed with reasons in that
  test: `GET /auth/me` (the SPA reads `must_change_password` off it to render the forced-change
  screen — gating it would 403 the probe), `POST /auth/logout/all` (self-revocation must work
  from any state, else a rejected account's refresh token keeps rotating with no kill switch),
  and `GET /auth/flower-authz` (calls the gate in-body and normalizes every denial to 401,
  because nginx `auth_request` forwards a 403 verbatim and treats only 401 as unauthenticated).
  Note `files/management.py` also enforces admin via an **inline** `is_admin` check on two
  handlers, so a dependency-based authz audit still will not see those.
- **`POST /files/management/cleanup-orphaned` does not clean up orphaned anything.** It runs
  `check_for_stuck_files` + `recover_stuck_file` — it is the bulk sibling of
  `POST /tasks/system/fix-file/{uuid}`. Real orphan cleanup is `POST /admin/data-integrity`.
  The **path stays** (runbooks and cron jobs call it); the docstring, the OpenAPI summary and the
  403 detail now say "recover stuck files", and the dead `"marked_orphaned"` counter — initialized,
  never incremented, so it reported `0` in every deployment ever — is **gone** from the response,
  which is now exactly `stuck_files_found` · `recovered` · `errors` · `dry_run` (#431). It had no
  consumer in `frontend/src` or the tests. Don't re-add an orphan counter to this handler; nothing
  in it marks a file orphaned.
- **`GET /admin/stats`: `system.version` is `core.version.APP_VERSION` and `system.gpu` is always
  a LIST.** The version was a hardcoded `"1.0.0"` that no release moved, so the admin panel
  disagreed with `/health` and the About dialog about the running build; and the
  stats-collection `except` branch substituted a bare **dict** for `gpu` while every
  `get_gpu_usage()` return is a list, so the key's type depended on whether psutil raised (#431).
  Note the sibling `GET /system/stats` spells the same list `gpus` and is what the SPA reads —
  `/admin/stats`' `gpu` has no frontend consumer, so don't "align" the names without checking the
  runbooks.
- **`GET /files/{uuid}/subtitles` is a transcript read surface and is gated like one.**
  `SubtitleService` masks with the segment's **cached** spans (`seg.redactions or []`), so on a
  file whose detection scan never ran, is queued, or is mid-flight there is nothing to apply and
  the export writes the **raw transcript to disk** — with nothing in the file to say the scan was
  incomplete. The endpoint therefore calls the same `_redaction_pending` (`files/crud.py`) the
  detail and segments endpoints use, and answers **409** when it returns True. Not 503: that code
  is already taken by `_resolve_subtitle_redaction`'s "the policy could not be resolved", and the
  two are different problems (the server is fine; the *file* is not ready). Deliberately not an
  on-demand inline scan either — a cold PII model load is ~10 s on a download request, and the
  page the file would be exported from already answers "not yet".
  Its **two sibling export paths are now covered too** (#85): `build_subtitle_archive` (the
  `POST /files/bulk-export/prepare` ZIP, via `tasks/media_download.py`) and the burned-in-subtitle
  render in `video_processing_service`. Both used to call the generators with **no redaction
  config at all** and exported unmasked regardless of policy — including under the admin
  `force_export_redacted` floor, i.e. a control that read as enforced and was not. `cfg=None`
  meant "redaction disabled" to `_redact_segments_inplace`, which is indistinguishable from "the
  caller forgot", so no gate could fire; the parameter is now **required and non-optional** on
  both, and a disabled policy is spelled by a config whose `enabled` is False. Whose policy, when
  it is resolved, and what the alternatives leak: `services/redaction/export_policy.py`. The
  short version — the **requesting user** (matching the single-file endpoint beside it; the file
  owner is `llm_guard`'s subject, and that governs third-party egress rather than a read), and at
  **run time inside the task** from a `user_id`, never a config serialized into the Celery
  signature. A file whose scan is unfinished is *skipped* by the batch (one file must not fail
  the other 99) and *refused* by the burn-in, which cannot be un-burned.
- **A burned-in-subtitle download is keyed by the caller's redaction policy, in three
  places at once** (#85): the object-storage cache key, the Redis dedup guard
  (`download_prep_guard_key`), and the `variant` field every `download_events` message
  carries and `_download_event_frame` filters on. All three must move together. The cache key
  alone is not enough — the dedup guard collapses concurrent requests for one `(file, mode)`
  into a single build and the channel is per FILE, so two readers whose policies mask
  differently would share one render and the second would receive the first one's video. The
  variant is `export_policy_fingerprint(cfg)`, empty for every audio mode and for any policy
  that masks nothing, so those keys/guards/events are byte-identical to before. It is
  **routing only**: the worker re-resolves the real policy at run time and never trusts it.
- **Both SSE streams go check → subscribe → re-check.** `download_stream` (`files/__init__.py`)
  and `bulk_export_stream` (`files/subtitles.py`) read their readiness signal, subscribe to the
  pub/sub channel, then read it **again**. A single check-then-subscribe loses a worker
  completion published in the gap and the stream waits on `get_message` forever (#284 A1.22,
  #334). `test_handler_blocking_io.py` AST-pins the ordering for both.
- The WS endpoint `accept()`s *before* authenticating (cookie, else a 10 s first-message
  `authenticate` frame), then closes with 4001/4002/4003 — not HTTP status codes.
- Never touch `websockets.manager` from sync code; publish with
  `app/utils/websocket_notify.py:send_ws_event` (Redis pub/sub) so all API/worker processes reach
  the connection-owning process.
- Middleware order is load-bearing: `ObservabilityMiddleware` is added **last** in `main.py` so
  it runs outermost.
