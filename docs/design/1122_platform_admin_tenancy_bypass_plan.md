# #1122: Platform-admin bypass vs. organization scoping. Design

Status: **PLAN ONLY.** Nothing here is implemented. First written 2026-10-09 against `master` @
`c3a84221`. **Amended 2026-10-09 against `origin/feat/v0.6.0-frontend-ux` @ `89507512`**:
security review (Appendix A), the frontend plan's amendments A1-A13 folded in, an explicit API
contract (§6), the migration-number fix, and an implementation order (§9). Line numbers were
re-verified on that commit. They drift, so search for the quoted symbol rather than trusting a
number.

Issue: [#1122](https://github.com/attevon-llc/OpenTranscribe/issues/1122) (milestone v0.6.0,
`epic:compliance`). Found during #1103 / PR #1119. Related: `docs/security/tenant-boundaries.md`.
Companion: `docs/design/1122_support_access_frontend_plan.md` ("frontend plan"). Its §1 types
cite §6 of this file.

## 1. Problem

`User.is_admin` (`role in {admin, super_admin}`, `backend/app/models/user.py:257-260`) works as a
**global content bypass**. On every file, speaker, comment, collection and tag path it is checked
*before* the tenant gate, so a platform admin can read, edit and delete any tenant's content by
UUID. Lists also widen to "every user in the active scope", and in personal scope that covers
every user's personal workspace. On a community install there is one tenant, so this is correct
there. On a multi-tenant deployment (cloud edition, or a self-hosted install with organizations)
it means one admin credential gives implicit, unaudited read/write access to every tenant.

`docs/security/tenant-boundaries.md:23` currently says this is by design ("Instance administrators
keep instance-wide operational access"). This plan narrows that statement. Operational access
stays. Access to content becomes explicit, separately authorized, time-boxed and audited.

**Where orgs come from (verified).** Org context is resolved only from
`request.state.external_identity.org_id`, which the external-IdP verifier sets
(`api/deps_context.py:53-101`). There is no org-switch header. No code in this repo creates
`Organization` rows (only tests do). So in practice **org grants exist only in the managed
edition**. A self-hosted install reaches MULTI mode only through `TENANCY_MODE=multi`, and there
only personal-workspace grants are possible. The design still covers both target kinds.

## 2. Industry baseline this design follows

- **Least privilege / tenant-scoped admin by default** (OWASP Multi-Tenancy Cheat Sheet; NIST
  800-53 AC-6, AC-6(5)). Enforce tenant context on every data access, and don't let a privileged
  role skip it implicitly. Tenant administration belongs to the tenant (`org:admin`), and
  platform staff hold no standing access.
- **Customer-approved operator access.** Azure *Customer Lockbox* and Google Cloud *Access
  Approval*: an operator requests access with a justification, the customer approves it, and it
  expires automatically. The approver can't be the requester (AC-5 separation of duties).
- **Break-glass, visible to the customer.** Google *Access Transparency* and AWS break-glass
  guidance: emergency access is possible without approval, but it is limited to a few
  principals, short-lived, carries a mandatory reason, and is logged where the customer can read
  it.
- **Temporary/emergency accounts are time-bound and auto-disabled** (AC-2(2)). Here the grant is
  the temporary authorization, and expiry is enforced on every request.
- **Fail closed.** If the system can't tell whether a deployment is multi-tenant, a grant can't
  be validated, or the grant-use record can't be written, access is denied.
- **Audit every cross-tenant use** (AU-2/AU-3/AU-12, which `app/auth/audit.py` already targets),
  and protect that record (AU-9). §5.6 explains why the per-use record lives in Postgres, not
  only in the OpenSearch audit stream.

## 3. Current-state inventory

Paths are relative to `backend/app/`. Classes:
- **X**: cross-tenant content exposure (must be gated).
- **P**: legitimate platform operation (keep; metadata/ops only).
- **Q**: quarantine-review visibility only, not a tenant bypass (keep on `is_admin`).

### 3.1 Chokepoints (X): fixing these covers most routes

| Site | Behaviour today |
|---|---|
| `utils/uuid_helpers.py:294` `get_file_by_uuid_with_permission` | `if is_admin: return file`, **before** the tenant gate (`:313`, which answers **403**, not 404). It is called with `is_admin=current_user.is_admin` from `api/endpoints/{files/*,comments,speakers,speaker_clusters,summarization,topics,tags/*,tasks,transcript_segments,user_files,search}.py`. Chat passes `is_admin=False` deliberately (`services/chat/context_resolver.py:105-111`) |
| `api/endpoints/files/crud.py:81-84` `get_media_file_by_uuid` | `if is_admin: return get_file_by_uuid(db, uuid)`: a bare lookup that **never** reaches the helper above. Used by `/stream-url` (`files/__init__.py:647-649`), detail, update, delete, segment edit and stream info (`crud.py:912/1056/1151/1240/1354`) |
| `utils/uuid_helpers.py:372` / `:436` | collection lookup bypass; `_with_sharing` returns effective `"owner"` |
| `utils/uuid_helpers.py:490` `require_speaker_access` | unconditional `user.is_admin` bypass |
| `utils/uuid_helpers.py:535` `require_profile_in_scope(allow_admin=)`, `:225` `require_resource_owner(allow_admin=)` (caller `api/endpoints/comments.py:285`) | owner/tenant gate bypass |
| `services/delete_permissions.py:55`, `:110` | admin may delete any file in any tenant (#1103) |
| `services/tag_bulk.py:159` `_require_editor` | X (write) |
| `api/endpoints/tags/_common.py:91` `_writable_tag_ids` | `is_admin` may mutate **system** tags (shared vocabulary): P, keep |

### 3.2 Inline admin branches (X)

| Site | Exposure |
|---|---|
| `api/endpoints/files/__init__.py:306-308` list | admin sees every user's files in scope; in personal scope (`org_pred = organization_id IS NULL`) that is **every personal workspace** |
| `files/__init__.py` filters/info/download prepare+stream/cache clear/analytics (`rg -n is_admin`, 21 sites) | X |
| `files/__init__.py:1103-1179` **thumbnail** | optional auth; uses `resolve_org_context` directly, never `get_current_context`; admin skips both tenant branches (`:1151`, `:1167`). Public files skip auth entirely (`:1144`, unchanged) |
| `files/filtering.py:429` owner facet | admin gets every owner in scope; in personal scope that is every user with a personal file |
| `files/crud.py:588` `_resolve_redaction_for_request` (detail and `files/segments.py:71`), `files/subtitles.py:73`, `files/transcript_export.py:78` | `can_reveal = owner or is_admin`: the admin can **reveal unredacted PII** of any tenant |
| `files/management.py:111, 211, 263, 367, 413, 448, 1036-1079` | status detail, cancel, retry, recover, force-delete, stuck list, bulk action |
| `files/reprocess.py:460-462` | admin takes a **bare** `get_file_by_uuid`, then dispatches up to 8 Celery tasks (`:118-407`) and skips the retry ceiling (`:510`) |
| `files/waveform.py:263, 305, 358`; `files/segments.py:66` | per-file reads/writes |
| `tasks.py:100, 143, 273, 867-870 (bare lookup), 898, 979, 999-1000 (raw id query)` | task list / progress / retry / by-id lookups ignore tenant entirely for admin |
| `tasks.py:772-783` `POST /tasks/system/fix-file/{uuid}` | admin-only, bare lookup. State repair, no content returned: **P**, but must stay metadata-only |
| `speakers.py:180, 275, 840, 1150-1208, 1777, 2624, 2668` | speaker lists, `/debug/cross-media-by-name` (`:1138`, returns speaker names + file titles across all tenants), rename-everywhere (`restrict_to_user_id=None`) |
| `speaker_profiles.py:56-58` (lists **all** profiles of all tenants), `:369` | X |
| `summarization.py:96, 226, 299, 485, 551`; `topics.py:83, 247, 313, 391, 466, 525` | through the §3.1 helper |
| `comments.py:221, 331`; `transcript_segments.py:123`; `media_collections.py:1062` | read/delete/edit |
| `watch_sources.py:210` ("admins operate every tenant's sources"), `:585` `scope=all`, `:636-641` `assign_to_user_uuid` (any user, no membership check) | X |
| `user_files.py:267, 388` | X |

### 3.3 Platform operations (P), which stay admin-gated

`api/endpoints/admin.py` (gated by dependency, so `rg is_admin` finds nothing there): user/role/
session/MFA management, settings, data-integrity, embedding-consistency, imohash, gpu-profiles,
stats (`:694`, metadata only), audit logs, `POST /admin/gdpr/erase-user`, retention purge.
Also `users.py` list/detail, `search.py` instance index health (`:1349`), `tags/crud.py:190`
cleanup, `tags/operations.py:82` promote, `files/waveform.py` bulk backfill,
`files/management.py:1142-1180` bulk stuck recovery, `tasks.py:596/637/658-681/807-830`
recovery jobs, `admin_timing.py:86` (pipeline timings, no content), and
`core/capabilities.py:293` (superuser sees capability-hidden *surfaces*, never data;
`require_org_admin` still 403s them).

Two P routes return **cross-tenant filenames and owner emails**: retention preview
(`admin.py:1282-1340`, `title = f.filename` at `:1326`) and the quarantine list
(`admin.py:2966-3044`). Filenames are tenant content in the Lockbox sense, because they often
name the meeting or the person. In MULTI mode these two routes stamp a
`PLATFORM_ADMIN_METADATA_ACCESS` audit event per response (actor, count, org ids). Whether to
also mask filenames there is product decision D3 (Appendix B).

### 3.4 Quarantine visibility (Q), unchanged

`exclude_quarantined(include_quarantined=is_admin)` and `is_hidden_for(..., is_admin=)` in
`files/__init__.py`, `tasks.py:107/149`, `user_files.py:102/150/199`,
`media_collections.py:336/479/1072`, `speaker_clusters.py:168/230`, `speaker_profiles.py:771`,
`search.py:317/488/497/556/567/744/828/867`,
`services/search/hybrid_search_service.py:1431/1563/1990`, `services/tag_collisions.py:543`,
`api/endpoints/chat/citation_takedown.py:61/91` (via `chat/messages.py:162`, `chat/export.py:262`),
and `services/takedown_service.py:99-113/311`. These only decide whether a taken-down row inside
an already tenant-scoped result is shown. They never widen tenant scope.

### 3.5 Surfaces outside the HTTP chokepoints (verified)

| Surface | Fact | Consequence for this design |
|---|---|---|
| Search | `_build_filters` (`hybrid_search_service.py:1609-1653`) requires `accessible_user_ids ∋ caller` plus `org_filter_clauses`. No admin arm. Speaker/voiceprint kNN is owner + org scoped (`services/opensearch_service/client.py:82-94` and callers) | No bypass to remove. Under a grant search finds nothing of the tenant's, so search routes **refuse** the grant header (§5.2) rather than silently returning the admin's own results under a tenant banner |
| Chat | Scope resolution passes `is_admin=False` (`context_resolver.py:105-111`); conversations are owner+tenant (`chat/common.py:40-59`) | Same: refuse the header. A grant must never feed tenant content to an LLM provider the tenant didn't choose |
| WebSocket `/ws` | per-`user_id` routing only (`api/websockets.py:33-42, 85`), no org context | Grant lifecycle events go to the right users by id (§5.5). Live file events for the assumed tenant don't reach the grantee (accepted, frontend plan §7) |
| SSE | `/files/{uuid}/download-stream` (`files/__init__.py:963-1094`) authorizes via ctx. `EventSource` **cannot send custom headers** (frontend `lib/fileDetail/downloadStream.ts:91`). `/files/bulk-export-stream` (`subtitles.py:349-474`) authorizes by HMAC-signed job id | Downloads and exports are **refused** under a grant in v1 (§4 rule 9, decision D1), so no SSE path needs the header |
| Presigned MinIO URLs | SigV4 bearer URLs, not bound to a user (`services/minio_service.py:143-213`). `MEDIA_URL_EXPIRE_SECONDS = 21600` (6 h, `core/config.py:577`) and thumbnails 900 s (`:579`). The `/stream-url` docstring's "5 minutes" is wrong. Revoking a grant does not invalidate a minted URL | Under a grant, presign TTL = `min(SUPPORT_ACCESS_PRESIGN_MAX_SECONDS (300), seconds until grant expiry)`, floored at the 60 s clamp (`services/storage_backend.py:199-236`). Residual access after revocation is ≤ 5 min (§10) |
| Celery | Tasks don't re-authorize. They re-fetch by id and act with the **owner's** settings (`tasks/summarization.py:373-378`) | The HTTP chokepoint is the only gate. LLM-dispatching verbs (summarize, identify speakers, topics, reprocess) need a **write** grant and are audited. A dispatched task finishes even if the grant is revoked (§10) |
| Redaction config | `resolve_effective_config(db, current_user.id, ...)` (`crud.py:574`, also `files/__init__.py` `_download_redaction_variant`) resolves the **caller's** prefs | Under a grant, resolve with the **file owner's** user id and the file's org, and force `reveal = ∅`. Otherwise "support sees what the tenant's policy shows" (§4 rule 6) is false |
| Audit | `audit_logger.log` never raises. It writes to stdout and to OpenSearch (`replicas: 0`, `max_retries=0`), falls back to a JSONL file, swallows that failure too (`auth/audit.py:233-258`), and is a no-op when `AUDIT_LOG_ENABLED` is false (`:365`) | Not sufficient as the system of record for grant use. §5.6 adds a Postgres use log written fail-closed |

## 4. Decision

1. **Tenancy mode** is explicit and fails closed (§5.1). In **single-tenant** mode the bypass
   keeps today's behaviour exactly (community invariance).
2. In **multi-tenant** mode, `role in {admin, super_admin}` grants **no implicit content access**.
   An admin's content rights inside a tenant are whatever their membership gives them
   (`org:admin` / `org:member` / owner / share). Platform operations (§3.3) are unaffected.
3. Operator access to tenant content requires a **support-access grant**:
   - **Approved grant** (Lockbox model). An `admin`/`super_admin` requests access with a reason,
     access level (`read` | `write`) and duration (15-480 min). An `org:admin` of the target org
     approves it, or the subject user for a personal workspace. The approver may **shorten** the
     duration, never extend it. Self-approval is refused. A pending request lapses after 72 h.
   - **Break-glass grant**. `super_admin` only. It is active immediately, needs a reason **and**
     a ticket reference, and has TTL 15-240 min (default 60). The tenant's org admins (or the
     subject user, for a personal workspace) are notified in real time. The grant appears in
     their support-access list and its uses in their access log. Break-glass on the caller's own
     workspace is refused (422).
4. A grant is **never ambient**. It applies only to requests carrying
   `X-Support-Access-Grant: <grant uuid>`. The grant is bound to the grantee's user id, so a
   leaked uuid is useless to anyone else. Browsers can't attach the header cross-origin without
   a CORS preflight, and `allow_headers` is an explicit list (`main.py:1067-1074`) that this plan
   does **not** extend, so CSRF can't forge it. With an org grant, the request *assumes* that
   tenant (`ctx.org_id = grant.organization_id`, `org_role = None`). With a personal grant, the
   request keeps the caller's personal scope and the bypass applies by UUID to the subject's
   personal rows only.
5. **Every** request authorized under a grant is recorded (§5.6), and every per-resource
   authorization emits `support_access.used` stamped with the tenant's `organization_id` and
   `target_user_id` = resource owner. The record is written **fail-closed**: if the use row can't
   be written, the request gets 503.
6. Unredacted reveal (`?redact=false`) is **owner-only** in multi-tenant mode. A grant never
   unlocks it. Under a grant, redaction is resolved for the **owner** (§3.5), so support sees
   exactly what the tenant's policy shows.
7. **Read grants are read-only by HTTP method.** Under a read grant every non-safe method
   (anything but GET/HEAD/OPTIONS) is refused at context resolution with
   `support_grant_write_required`. This holds even on routes that never call `allows()`, which
   closes the "assumed tenant = implicit membership" gap (Appendix A, F3).
8. **Grants never create tenant content or act as an org admin.** Under any grant, these are
   refused with `support_grant_action_not_permitted`: upload/URL ingest/multipart, chat, search,
   watch-source create, collection/tag/speaker-profile create, all `/org-admin/*` (also enforced
   by `org_role=None`), all `/admin/*` (admins already have it without a grant), sharing changes,
   and `/users/*`.
9. **Downloads and exports are refused under a grant in v1**: media download prepare/stream,
   transcript/subtitle/summary export, bulk export, and audio extraction. Grants are for
   in-product diagnosis, not exfiltration. Product decision D1 can lift this later with a
   signed, grant-bound job id like `bulk_job_owned_by`.
10. **Lifecycle routes never resolve the header** (A8): `/auth/*`, `/system/*`,
    `/support-access/*`, `/org-admin/*` and `/users/me/support-access*` use the base context.
    So a stale header can never 403 the call that would clear it.
11. **No caching of grant resolution.** The grant row is re-read on every request, so revocation,
    expiry, demotion and org deactivation take effect on the next request. Residual exposure is
    bounded by the presign clamp and in-flight requests (§10).

Rejected alternatives:
- **"> 1 org" detection.** A personal workspace plus one org is already two tenants.
- **An admin-UI (`SystemSettings`) toggle for the mode.** The control constrains admins, so
  admins must not be able to switch it off (separation of duties).
- **A session-wide "support mode" cookie.** It gives ambient authority that leaks into ordinary
  browsing.
- **A query-parameter grant** (to make `EventSource`/`<video>` work). It leaks to access logs,
  history and `Referer`.

## 5. Exact changes

### 5.1 Tenancy mode: `core/config.py`, new `services/platform_access.py`

- `config.py`: `TENANCY_MODE: str = os.getenv("TENANCY_MODE", "auto")`. Add it to `.env.example`
  (advanced section) with a one-line explanation. Add `TENANCY_MODE: ${TENANCY_MODE:-auto}` to
  the `environment:` lists of `backend` (`docker-compose.yml:282`) and `celery-worker` (`:400`)
  (**A12**). Celery doesn't read the mode today, but compose parity keeps a future task-side check
  honest.
- `services/platform_access.py` (new, under 300 lines):

```python
class TenancyMode(StrEnum): SINGLE = "single"; MULTI = "multi"

def tenancy_mode(db: Session) -> TenancyMode:
    """'single'/'multi' force. Unknown value -> MULTI + logger.error.
    auto: MULTI iff DEPLOYMENT_EDITION != 'community' OR EXISTS(active Organization row).
    DB error -> MULTI for THIS call only (fail closed), logger.error, never cached.
    A positive MULTI detection is cached and latches for the process; SINGLE is cached
    for SETTINGS_CACHE_TTL (30 s, core/config.py:818). Cache bypassed when TESTING=true
    (same rule as core/settings_cache.py)."""
```

  The original draft latched MULTI on *any* MULTI result. A transient DB error on a community
  install would then strip the admin bypass until restart, so only a positive detection latches
  (Appendix A, F12).
- On startup (`main.py` lifespan): if `TENANCY_MODE=single` is forced while active orgs exist, log
  a WARNING once. In that state every cross-tenant bypass use is audited
  (`PLATFORM_ADMIN_CONTENT_ACCESS`).
- **A1:** `GET /system/capabilities` (`api/endpoints/system.py:32`) gains a top-level
  `"tenancy_mode": "single" | "multi"`. That route is unauthenticated and already exposes
  `edition`, so this discloses nothing new.

### 5.2 `PlatformBypass` and `RequestContext`: `services/platform_access.py`, `api/deps_context.py`

```python
@dataclass(frozen=True)
class SupportGrantView:                    # frozen snapshot of a validated grant
    id: int; uuid: str; organization_id: int | None; subject_user_id: int | None
    access_level: Literal["read", "write"]; grant_mode: Literal["approved", "break_glass"]
    expires_at: datetime

@dataclass(frozen=True)
class PlatformBypass:
    user: User | None
    mode: TenancyMode
    grant: SupportGrantView | None
    _audited: set = field(default_factory=set, compare=False)  # per-request dedupe

    @classmethod
    def none(cls) -> "PlatformBypass": ...
    def allows(self, *, org_id: int | None, owner_id: int, need: Literal["read", "write"],
               resource_type: str, resource_uuid: str) -> bool:
        # single: return user.is_admin (PLATFORM_ADMIN_CONTENT_ACCESS when forced-single and
        #   the row is in another tenant than the caller's)
        # multi, no grant: False
        # multi, grant: tenant match (org grant: org_id == grant.organization_id;
        #   personal grant: org_id is None and owner_id == grant.subject_user_id)
        #   and (need == "read" or grant.access_level == "write")
        #   -> record_use(resource_type, resource_uuid, need) once per key per request -> True
    @property
    def user_is_admin(self) -> bool:      # quarantine-review visibility (Q) only, never tenant scope
        return bool(self.user and self.user.is_admin)
    @property
    def sees_all_in_scope(self) -> bool:
        # single: user.is_admin; multi: grant is an ORG grant (lists of the assumed tenant)
    @property
    def under_grant(self) -> bool:
        return self.grant is not None
    def presign_ttl(self, default_seconds: int) -> int:
        # no grant: default; grant: max(60, min(default, 300, seconds_until(grant.expires_at)))
```

`api/deps_context.py`:
- `RequestContext` gains `bypass: PlatformBypass = PlatformBypass.none()`.
- **Split the dependency (A8):**
  - `get_base_context` is today's `get_current_context` body. It **ignores** the header.
  - `get_current_context` = `get_base_context` + grant resolution.
  - `require_org_admin` switches to `get_base_context`.
  - The routers for `/auth/*`, `/system/*`, `/support-access/*`, `/org-admin/*` and
    `/users/me/support-access*` depend only on `get_base_context` / `get_current_active_user`.
- Grant resolution in `get_current_context`, when `X-Support-Access-Grant` is present:
  1. Mode must be MULTI, else **400** `support_access_unavailable`.
  2. Malformed uuid: **403** `support_grant_invalid`.
  3. `support_access_service.resolve_active_grant(db, uuid, current_user)` returns the grant or
     raises 403 with the §6.4 code:
     - `support_grant_invalid`: unknown uuid, wrong grantee, grantee no longer `is_admin` or
       inactive, target org inactive or deleted, target user deleted.
     - `support_grant_not_active`: pending, denied, or not yet started.
     - `support_grant_expired`
     - `support_grant_revoked`
  4. Never fall back silently to normal scope.
  5. If the method is not GET/HEAD/OPTIONS and the grant is `read`: **403**
     `support_grant_write_required` (rule 7).
  6. For an org grant: `org_id = grant.organization_id`, `org_role = None`, and set
     `request.state.org_id`. For a personal grant the scope is unchanged.
  7. Set `request.state.support_grant_id` and write the **request-level** use row (§5.6). If the
     write fails: **503** `support_access_audit_unavailable`.
  8. Build `bypass = PlatformBypass(user, mode, grant)`.

  Without a header, build `bypass = PlatformBypass(user, mode, None)`.
- New dependency `refuse_under_support_grant(ctx = Depends(get_current_context))` raises 403
  `support_grant_action_not_permitted` when `ctx.bypass.under_grant`. Attach it at router level
  to the rule 8/9 surfaces:
  - **Routers:** chat package, `search.py`, upload/prepare/complete/multipart/`url_processing`,
    `watch_sources` create, `users.py`, `admin*.py`.
  - **Routes:** collection/tag/speaker-profile create, share mutation, download prepare/stream,
    `transcript_export`, `subtitles` export and bulk export, summary export.
  - **Routers that depend only on `get_current_active_user`:** they never see a grant. Because
    the header is otherwise silently ignored there, they must also either take
    `refuse_under_support_grant` or be on the §8 test 31 allowlist.
- **Thumbnail (A9):** `files/__init__.py:1103` keeps optional auth. When a user is present it
  resolves the grant through a shared `resolve_request_bypass(request, db, user)` (same code
  path as `get_current_context`, so the rules can't diverge) and replaces both `is_admin`
  branches with `bypass.allows(..., need="read", resource_type="media_file")`.
- `scope_to_context` is unchanged.

### 5.3 Chokepoint signatures (mechanical migration)

Replace the `is_admin: bool` / `allow_admin: bool` parameter with `bypass: PlatformBypass`
(keyword-only, default `PlatformBypass.none()`), and evaluate it **after** loading the row, with
the row's tenant:

| Function | New check (replaces the early `if is_admin: return`) |
|---|---|
| `uuid_helpers.get_file_by_uuid_with_permission` | `if bypass.allows(org_id=file.organization_id, owner_id=file.user_id, need="write" if min_permission != "viewer" else "read", resource_type="media_file", resource_uuid=str(file.uuid)): return file`. The takedown check keeps `is_hidden_for(file, is_admin=bypass.user_is_admin)` (Q) |
| `files/crud.get_media_file_by_uuid` / `get_media_file_by_id` | **delete** the bare-lookup branch (`crud.py:81-84`) and always delegate to the helper above with `bypass` |
| `get_collection_by_uuid_with_permission` / `_with_sharing` | same, on `collection.organization_id/user_id`; `_with_sharing` returns `"owner"` only for a write-capable bypass, else `"viewer"` |
| `require_speaker_access`, `require_profile_in_scope`, `require_resource_owner` | same, on the speaker's file / profile / resource |
| `services/delete_permissions.can_delete_file` / `get_deletable_file` | `bypass.allows(..., need="write")` replaces `user.is_admin` at `:55` and `:110` |
| `services/tag_bulk._require_editor` | take `bypass` |
| `files/reprocess.py:460-462`, `tasks.py:867-870`, `tasks.py:999-1000` | delete the admin bare-lookup branches. Go through the helper with `bypass` and `need="write"`. The admin retry-ceiling skip (`reprocess.py:510`, `tasks.py:898`) stays, but only after the helper passed |

Call sites: `is_admin=current_user.is_admin` becomes `bypass=ctx.bypass`. A route that depends
only on `get_current_active_user` must add `ctx: RequestContext = Depends(get_current_context)`
(it already sits behind the lifecycle gate, see `api/CLAUDE.md`).

**Status codes.** The file and collection helpers answer **403** for an out-of-tenant row
(`uuid_helpers.py:313`, `:377`), while the speaker and profile helpers answer **404**
(`:500`, `:540`). This plan keeps each helper's existing code, and the tests pin it (§8).
Unifying cross-tenant to 404, which removes an existence oracle, is decision D4.

### 5.4 Inline sites (§3.2)

- "Admin sees all" list branches (`files/__init__.py:306`, `files/filtering.py:429`,
  `tasks.py:100/143/979/999`, `speakers.py:180/275/840/1150-1208/2624/2668`,
  `speaker_profiles.py:56`, `watch_sources.py:585`, `user_files.py`): `if current_user.is_admin`
  becomes `if ctx.bypass.sees_all_in_scope`, and the tenant predicate (`org_pred` /
  `owned_in_tenant` with `ctx.org_id`) **must still apply** in that branch.
  - `speaker_profiles.py:56` today has **no** org filter at all, so add one.
  - `tasks.py:273` (Redis progress) filters to file uuids in scope unless `sees_all_in_scope`.
  - A list under an org grant writes one request-level use row, not one row per item.
- `speakers.py:1777`, `speaker_profiles.py:369`: `restrict_to_user_id=None` only when
  `ctx.bypass.sees_all_in_scope`. Also pass the tenant so a rename never crosses tenants.
- `speakers.py:1138` `/debug/cross-media-by-name`: in MULTI mode, scope the queries with
  `org_scope_pred(MediaFile.organization_id, ctx.org_id)`, and refuse it under a grant.
- Reveal: `files/crud.py:588`, `files/subtitles.py:73`, `files/transcript_export.py:78` become
  `can_reveal = owner or (ctx.bypass.mode is SINGLE and current_user.is_admin)`.
- **Redaction subject under a grant:** `_resolve_redaction_for_request` and
  `_download_redaction_variant` call
  `resolve_effective_config(db, file.user_id if ctx.bypass.under_grant else current_user.id, organization_id=file.organization_id if under_grant else ctx.org_id)`.
  Extend the existing `transcript.view_unredacted` audit to carry `grant_uuid`.
- Presign: every browser-facing presign in `files/__init__.py` (`:156`, `:692`, `:772`),
  `files/crud.py:270` (list thumbnails), `speaker_clusters.py:244` and `speaker_profiles.py:174`
  passes `expires=ctx.bypass.presign_ttl(<current default>)`.
- `watch_sources.py:210/213`: `_get_source_or_404` uses
  `ctx.bypass.allows(org_id=source.organization_id, owner_id=source.user_id, ...)`. At `:636-641`
  (assign to user), in MULTI mode require `tenant_sharing.user_in_tenant(target, ctx.org_id)`,
  else 400.
- `comments.py:221/331`, `transcript_segments.py:123`, `media_collections.py:1062`,
  `files/waveform.py:263/305/358`, `files/segments.py:66`, `files/management.py:111-448/1036-1079`:
  use `ctx.bypass.allows(...)` on the loaded row. `force_delete_file` (`:413`) keeps its
  `is_admin` precondition **and** goes through `get_deletable_file(bypass=...)`.
- §3.3 retention preview and quarantine list: in MULTI mode emit
  `PLATFORM_ADMIN_METADATA_ACCESS` (one event per response, `details={count, organization_ids}`,
  never filenames).

### 5.5 Support-access grants (new)

**Model** `models/support_access.py`. Table `support_access_grant`:

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `uuid` | UUID, uuid7, unique, indexed | only identifier on the wire |
| `target_kind` | String(20) NOT NULL | `organization` \| `personal` |
| `organization_id` | FK `organization` **SET NULL**, NULL | |
| `subject_user_id` | FK `user` **SET NULL**, NULL | |
| `grantee_user_id` | FK `user` **SET NULL**, NULL | actor convention since v387 |
| `access_level` | String(10) NOT NULL | `read` \| `write` |
| `grant_mode` | String(20) NOT NULL | `approved` \| `break_glass` |
| `reason` | Text NOT NULL | 10..2000 chars |
| `ticket_ref` | String(255) NULL | required for break-glass |
| `requested_duration_minutes` | Integer NOT NULL | **A3** |
| `requested_at` | timestamptz NOT NULL | |
| `decided_by_user_id` | FK `user` SET NULL, NULL | |
| `decided_at` | timestamptz NULL | |
| `decision` | String(10) NULL | `approved` \| `denied` |
| `starts_at`, `expires_at` | timestamptz NULL | set at approval / break-glass |
| `revoked_at` | timestamptz NULL | |
| `revoked_by_user_id` | FK `user` SET NULL, NULL | |

Why SET NULL rather than the draft's CASCADE: CASCADE on `organization_id`/`subject_user_id`
deleted the grant, the evidence that support touched the tenant, whenever the org or user was
erased. SET NULL keeps the row, and a grant whose target FK is NULL is permanently unusable
(`support_grant_invalid`).

CHECKs:
- `target_kind IN ('organization','personal')`
- `NOT (target_kind = 'organization' AND subject_user_id IS NOT NULL)`
- `NOT (target_kind = 'personal' AND organization_id IS NOT NULL)`
- `access_level IN ('read','write')`
- `grant_mode IN ('approved','break_glass')`
- `decision IS NULL OR decision IN ('approved','denied')`
- `requested_duration_minutes BETWEEN 15 AND 480`
- `grant_mode <> 'break_glass' OR (ticket_ref IS NOT NULL AND requested_duration_minutes <= 240)`
- `char_length(reason) BETWEEN 10 AND 2000`
- `expires_at IS NULL OR expires_at > starts_at`

Indexes: `(grantee_user_id, expires_at)`, `(organization_id, requested_at DESC)`,
`(subject_user_id, requested_at DESC)`.

Table `support_access_use` (append-only, §5.6):

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `grant_id` | FK `support_access_grant` **RESTRICT** | |
| `occurred_at` | timestamptz NOT NULL | |
| `method` | String(10) NOT NULL | |
| `route` | String(255) NOT NULL | route template, never the raw path |
| `resource_type` | String(40) NULL | |
| `resource_uuid` | UUID NULL | |
| `need` | String(5) NULL | `read` \| `write` |
| `organization_id` | int NULL, **no FK** | snapshot stamps that survive erasure, like `erasure_ledger` |
| `owner_user_id` | int NULL, **no FK** | same |

Index: `(grant_id, occurred_at)`. A request-level row has NULL `resource_*`.

Add both tables to `backend/tests/unit/test_user_deletion_fk_coverage.py` with their
dispositions. `gdpr_erasure_service.erase_user` / `erase_organization` leave both tables alone:
FKs SET NULL, and use rows hold ids only, no content, no names. Record that in the service
docstring.

**Migration.** `v431` is **already taken** on this branch (`v431_add_media_duration_provenance`).
**Do not hardcode a number.** The implementer derives the head at implementation time with
`python3 scripts/release-tests/lib/alembic-head.py backend` (it prints the single head and exits
non-zero on a fork), names the revision `v<next free number>_support_access_grant`, and sets
`down_revision` to the printed head. Then:
- Follow the 5-step procedure in `backend/app/db/CLAUDE.md`: idempotent `CREATE TABLE IF NOT
  EXISTS` / `DO $$` guards, the model, the schemas, a **detection arm at the top of
  `_detect_schema_version()`** (`app/db/migrations.py:53`) keyed on a CHECK unique to this
  revision (`ck_support_access_grant_target_kind`), and both test paths.
- Pair it with a consistency test modelled on `tests/unit/test_v37{7,8}_migration_consistency.py`,
  using `assert_detected_at_or_after`, never `== REVISION`.
- Never autogenerate inside the hot-reload container. Create the tables only, with no data
  migration.
- Re-run `alembic-head.py` immediately before committing, because a parallel lane may have
  taken the number in the meantime (`backend/alembic/CLAUDE.md`).

**Constants** (`core/constants.py`). These are code constants, not settings, on purpose: admins
must not be able to widen them.
- `SUPPORT_ACCESS_MIN_TTL_MINUTES = 15`
- `SUPPORT_ACCESS_MAX_TTL_MINUTES = 480`
- `SUPPORT_ACCESS_BREAK_GLASS_MAX_TTL_MINUTES = 240`
- `SUPPORT_ACCESS_DEFAULT_TTL_MINUTES = 60`
- `SUPPORT_ACCESS_PENDING_EXPIRY_HOURS = 72`
- `SUPPORT_ACCESS_PRESIGN_MAX_SECONDS = 300`

**Service** `services/support_access_service.py` (split into `_lifecycle.py` / `_queries.py` if
it passes 300 lines). Functions: `request_grant`, `break_glass`, `approve`, `deny`, `revoke`,
`resolve_active_grant`, `record_use`, `list_grants`, `list_uses`, `compute_status`, `to_out`.
- **Status** is computed, never stored:
  - `revoked` when `revoked_at` is set.
  - else `denied` when `decision = denied`.
  - else `active` when `decision = approved` or break-glass, and `starts_at ≤ now < expires_at`.
  - else `expired` when `now ≥ expires_at`.
  - else `lapsed` when undecided and `now ≥ requested_at + 72 h`.
  - else `pending`.
  `pending_expires_at` is `requested_at + 72 h` while pending, else null.
- **Decisions are atomic (TOCTOU).** `approve`/`deny` run
  `UPDATE ... SET decision=..., decided_at=now(), decided_by_user_id=... WHERE id=:id AND decision IS NULL AND revoked_at IS NULL AND requested_at > now() - interval '72 hours' RETURNING *`.
  Zero rows means a re-read, then 409 `support_grant_already_decided` or `support_grant_lapsed`.
- On approve: `starts_at = decided_at`, `expires_at = decided_at + min(body.duration_minutes or requested, requested)`.
  A `duration_minutes` above the request gives 422.
- **Self-approval:** approver id = grantee id gives 403 `support_grant_self_approval`. Whether a
  *different* platform admin who also holds `org:admin` in the target org may approve is
  decision D2. The default refuses it with the same code.
- **Revoke is idempotent (A8).** Revoking an already terminal grant returns 200 with the current
  grant and writes no new event.
- **Targets (A2):**
  - Exactly one of `organization_uuid` / `subject_user_uuid`, else 422.
  - An org target must exist and be active.
  - A personal target must be an active user who is not the caller.
  - Anything else gives 422 `support_grant_invalid_target`.
  - An org target in a deployment with no orgs is the same 422.
- **Rate limit:** `POST /support-access/grants` and `/break-glass` use the existing slowapi
  limiter at 10/hour per user, so request spam can't flood approvers.
- `record_use` inserts a `support_access_use` row in its **own short session** and commits.
  On failure it raises 503 (§5.6). It also calls `audit_logger.log(SUPPORT_ACCESS_USED, ...)`.

**Routes.** All the routes below sit in the new router `api/endpoints/support_access.py`,
mounted at `/support-access`, plus small routers mounted at `/org-admin/support-access` and
`/users/me/support-access`. Include the users one **before** `users.router`. It doesn't collide
with `GET /users/{user_uuid}` (one segment), but keep the order explicit anyway.
- Every route 404s in SINGLE mode through a `require_multi_tenant` dependency.
- **A13:** only `/org-admin/support-access*` sits under the `organizations` capability.
  `/support-access/*` and `/users/me/support-access*` must not, or forced-MULTI community has no
  working grant path.
- Authority per route is in §6.2.
- Add every route to `backend/tests/unit/test_route_privilege_tiers.py`.

**Audit** (`auth/audit.py` `AuditEventType`): `SUPPORT_ACCESS_REQUESTED`, `_APPROVED`, `_DENIED`,
`_REVOKED`, `_BREAK_GLASS`, `_USED`, plus `PLATFORM_ADMIN_CONTENT_ACCESS` (forced-single bypass
across tenants) and `PLATFORM_ADMIN_METADATA_ACCESS` (§5.4).
- Always set `user_id` = actor, `organization_id` = target org, and `target_user_id` =
  subject/owner. This is enforced by `tests/unit/test_audit_actor_target.py`.
- Details carry `grant_uuid`, `resource_type`, `resource_uuid`, `access_level`, the route template
  and the optional decision `note` (≤ 500 chars). They **never** carry content.

**WebSocket events (A10)** through `utils/websocket_notify.send_ws_event`:
- `support_access_requested` to the target's org:admins, or to the subject user.
  Payload: `{grant_uuid, grantee_name}`.
- `support_access_break_glass` to the same recipients.
  Payload: `{grant_uuid, grantee_name, target_name, expires_at}`.
- `support_access_decided` to the grantee. Payload: `{grant_uuid, status}`.
- `support_access_revoked` to the grantee. Payload: `{grant_uuid, status}`.

None name a `MediaFile`, so the raw primitive is allowed. Add all four to the allowlist in
`backend/tests/unit/test_ws_event_quarantine_discipline.py` with that reason.

**Cloud seam:** bump `CLOUD_SEAM_VERSION` 7 → 8 (`auth/constants.py:108`). Staff tooling in the
managed edition now needs a grant.

### 5.6 Grant-use record (fail-closed, tenant-visible)

The OpenSearch audit stream is best-effort by construction (§3.5). It is fine as the SIEM feed,
but Access Transparency means the tenant can rely on the log. So the authoritative record of
"support touched my data" is `support_access_use` in Postgres:
- **One request-level row per grant-authorized request**, written in `get_current_context` before
  the handler runs. This covers paths that rely on assumed-tenant membership and never call
  `allows()`.
- **One resource-level row per distinct `(resource_type, resource_uuid, need)` per request**,
  written by `allows()`.
- Write failure gives **503** `support_access_audit_unavailable`. The request is refused, never
  served unrecorded.
- Each row is mirrored to `audit_logger` as `SUPPORT_ACCESS_USED`, which is best-effort.
- Tenants read it through `GET /org-admin/support-access/{uuid}/uses` and
  `GET /users/me/support-access/{uuid}/uses`. Staff and super_admin read it through
  `GET /support-access/grants/{uuid}/uses`.
- No route updates or deletes use rows. Retention follows the audit retention policy, which is
  decision D5.

### 5.7 Docs

- `docs/security/tenant-boundaries.md:23`: replace the bullet with the §4 rules, and add an
  "Operator access" table row (grant routes, `PlatformBypass.allows`, test files).
- `backend/app/auth/CLAUDE.md` "Privilege tiers": add the sentence "admin/super_admin manage the
  platform; in multi-tenant mode they reach tenant content only through a support-access grant".
- `backend/app/api/CLAUDE.md`:
  - change the `uuid_helpers` docs from "admin bypass first" to "bypass evaluated on the loaded
    row";
  - document `get_base_context` vs `get_current_context` and `refuse_under_support_grant`.
- `backend/app/utils/CLAUDE.md`: the order note for `get_file_by_uuid_with_permission`.
- Fix the `/stream-url` docstring ("5 minutes"): the real default is 6 h.
- `docs-site/docs/` admin guide page "Support access & break-glass".
- CHANGELOG `Security` entry naming the behaviour change and the `TENANCY_MODE=single` escape
  hatch.

## 6. API contract

This is the single source of truth the frontend plan's §1 cites. All paths sit under `/api`. Every
request and response is JSON, and every timestamp is ISO-8601 UTC with `Z`. Every route in §6.2
returns **404** (plain `{"detail": "Not Found"}`) in SINGLE mode.

### 6.1 Schemas (`app/schemas/support_access.py`)

```jsonc
// UserRef: null when the referenced account was deleted (FK SET NULL)
{ "uuid": "…", "full_name": "Jane Doe" | null, "email": "jane@example.com" }
// OrgRef: null when the org was deleted
{ "uuid": "…", "name": "Acme", "slug": "acme" | null }

// SupportGrantOut
{
  "uuid": "…",
  "status": "pending" | "active" | "denied" | "expired" | "revoked" | "lapsed",   // server-computed (A4)
  "target_kind": "organization" | "personal",
  "grant_mode": "approved" | "break_glass",
  "access_level": "read" | "write",
  "organization": OrgRef | null,        // set iff target_kind == organization (null if deleted)
  "subject_user": UserRef | null,       // set iff target_kind == personal (null if deleted)
  "grantee": UserRef | null,            // null = grantee account deleted
  "reason": "…",
  "ticket_ref": "INC-123" | null,
  "requested_duration_minutes": 60,     // A3
  "requested_at": "2026-10-09T12:00:00Z",
  "pending_expires_at": "…" | null,     // requested_at + 72 h while pending, else null
  "decided_by": UserRef | null, "decided_at": "…" | null,
  "starts_at": "…" | null, "expires_at": "…" | null,
  "revoked_by": UserRef | null, "revoked_at": "…" | null
}

// GrantPage (A5): every list route
{ "items": [SupportGrantOut, …], "total": 17, "server_time": "2026-10-09T12:00:01Z" }

// SupportGrantUseOut / UsePage
{ "occurred_at": "…", "method": "GET", "route": "/api/files/{file_uuid}",
  "resource_type": "media_file" | null, "resource_uuid": "…" | null, "need": "read" | "write" | null }
{ "items": [SupportGrantUseOut, …], "total": 42, "server_time": "…" }

// CreateGrantBody (POST /support-access/grants), A2
{ "organization_uuid": "…" | null, "subject_user_uuid": "…" | null,   // exactly one non-null
  "access_level": "read" | "write",
  "reason": "10..2000 chars",
  "duration_minutes": 15..480 }

// BreakGlassBody (POST /support-access/grants/break-glass)
CreateGrantBody & { "ticket_ref": "1..255 chars", "duration_minutes": 15..240 (default 60) }

// ApproveBody: shorten-only
{ "duration_minutes": 15..requested | omitted (= requested) }

// DecisionNoteBody (deny / revoke): note goes to audit details only
{ "note": "≤ 500 chars" | omitted }

// OrgTargetOut (A6)
{ "uuid": "…", "name": "Acme", "slug": "acme" | null }
```

List query parameters, shared by every list route:
- `status`: one of the six statuses, or omitted for all. Filtered in SQL.
- `limit`: 1..100, default 25.
- `offset`: ≥ 0.

Ordering is `requested_at DESC`, newest first.

### 6.2 Endpoints

| Method + path | Authority | Body → Response | Notes |
|---|---|---|---|
| `GET /system/capabilities` | public (existing) | → existing dict + `"tenancy_mode"` | **A1**. Never 404 |
| `POST /support-access/grants` | `get_current_admin_user` | `CreateGrantBody` → **201** `SupportGrantOut` (`pending`) | WS `support_access_requested`; audit `REQUESTED` |
| `POST /support-access/grants/break-glass` | `get_current_active_superuser` | `BreakGlassBody` → **201** `SupportGrantOut` (`active`) | WS `support_access_break_glass`; audit `BREAK_GLASS` |
| `GET /support-access/grants` | admin | `?status&limit&offset&scope=mine\|all` → `GrantPage` | `scope=all` is super_admin only (403 for admin) |
| `GET /support-access/grants/{uuid}` | grantee, or super_admin | → `SupportGrantOut` | **A5**. 404 to anyone else (no enumeration) |
| `GET /support-access/grants/{uuid}/uses` | grantee, or super_admin | `?limit&offset` → `UsePage` | §5.6 |
| `POST /support-access/grants/{uuid}/revoke` | grantee, any super_admin, or `org:admin` of the grant's org (via base context), or the subject user of a personal grant | `DecisionNoteBody` → **200** `SupportGrantOut` | Idempotent on a terminal grant (A8). WS `support_access_revoked` to the grantee |
| `GET /support-access/targets/organizations` | admin | `?q&limit≤25` → `[OrgTargetOut]` | **A6**. Active orgs only; a P op |
| `GET /admin/users/search` | admin (existing, `admin.py:2383`) | unchanged | personal targets (A6) |
| `GET /org-admin/support-access` | `require_org_admin` + `organizations` capability | list params → `GrantPage` | grants whose `organization_id == ctx.org_id` |
| `GET /org-admin/support-access/{uuid}/uses` | same | → `UsePage` | 404 unless the grant targets `ctx.org_id` |
| `POST /org-admin/support-access/{uuid}/approve` | same; approver ≠ grantee | `ApproveBody` → **200** `SupportGrantOut` | WS `support_access_decided` |
| `POST /org-admin/support-access/{uuid}/deny` | same | `DecisionNoteBody` → **200** `SupportGrantOut` | WS `support_access_decided` |
| `GET /users/me/support-access` | any active user (subject) | list params → `GrantPage` | personal grants where `subject_user_id == me` |
| `GET /users/me/support-access/{uuid}/uses` | subject | → `UsePage` | |
| `POST /users/me/support-access/{uuid}/approve` | subject; approver ≠ grantee | `ApproveBody` → **200** `SupportGrantOut` | |
| `POST /users/me/support-access/{uuid}/deny` | subject | `DecisionNoteBody` → **200** `SupportGrantOut` | |

Any content route + header `X-Support-Access-Grant: <uuid>`: resolved per §5.2. The header is
**ignored** on every route above and on `/auth/*`.

### 6.3 Error body

Coded errors use the existing shape (`api/endpoints/auth/dependencies.py:247`):
`{"detail": {"code": "<code>", "message": "<human text>"}}`. Validation errors are FastAPI's
standard 422 list. When a 422 has a code, it is `support_grant_invalid_target`.

### 6.4 Error codes

| HTTP | `detail.code` | When | Ends the client session? |
|---|---|---|---|
| 400 | `support_access_unavailable` | grant header sent in SINGLE mode | yes |
| 403 | `support_grant_invalid` | unknown/malformed uuid, wrong grantee, grantee demoted or inactive, target org inactive/deleted, target user deleted | yes |
| 403 | `support_grant_not_active` | pending, denied, not started | yes |
| 403 | `support_grant_expired` | `now ≥ expires_at` | yes |
| 403 | `support_grant_revoked` | `revoked_at` set | yes |
| 403 | `support_grant_write_required` | read grant + non-safe method | no |
| 403 | `support_grant_action_not_permitted` | §4 rules 8-9 surface under a grant | no |
| 403 | `support_grant_self_approval` | approver is the grantee (or D2 default) | n/a |
| 409 | `support_grant_already_decided` | approve/deny on a decided or revoked grant | n/a |
| 409 | `support_grant_lapsed` | approve/deny after 72 h | n/a |
| 422 | `support_grant_invalid_target` | both/neither target, own workspace, inactive org, unknown user | n/a |
| 503 | `support_access_audit_unavailable` | use row could not be written | no (retry) |

An out-of-tenant resource under a valid grant keeps the helper's normal answer (403 for
file/collection, 404 for speaker/profile; §5.3), with no grant code. The client must not end
the session on it.

## 7. Back-compat / migration for existing deployments

| Deployment | Mode after upgrade | Change |
|---|---|---|
| Community, no `organization` rows | SINGLE (auto) | **none**: `allows()` returns `user.is_admin`, `sees_all_in_scope` = `is_admin`, reveal unchanged, presign TTLs unchanged, no new audit events, grant routes 404 |
| Community, `TENANCY_MODE=multi` | MULTI | admins lose implicit access to other users' personal workspaces; personal grants only |
| Self-hosted with orgs | MULTI (auto) | admins lose implicit content access; platform ops unchanged; must use grants. Escape hatch: `TENANCY_MODE=single` (startup WARNING plus `PLATFORM_ADMIN_CONTENT_ACCESS` audit on every cross-tenant use) |
| Cloud edition | MULTI | same, plus seam bump |

The only schema change is two new tables. Rolling back drops them and restores the old code.
The downgrade destroys the grant/use evidence, and the revision docstring must say so (the same
shape as `v389_add_erasure_ledger`). No existing row changes.

## 8. Test plan (red-first)

Prove each test red against the old code in a `git archive HEAD` tree (root `CLAUDE.md`), never
by swapping files in the checkout. Run against the stack (`./opentr.sh start dev`, or `--fresh`
when other agents are active), then run `python3 scripts/audit-tests.py backend/tests` to 0
findings. Creating an `Organization` row in a test flips auto mode to MULTI, because the cache is
bypassed under `TESTING=true`. Paths are relative to `backend/`.

`tests/unit/test_tenancy_mode.py`
1. `test_no_orgs_community_is_single`: control, green before and after.
2. `test_active_org_row_makes_auto_multi`: fails before (`ImportError`).
3. `test_db_error_fails_closed_to_multi_without_latching`: the first call raises and gives MULTI;
   the next call (DB healthy, no orgs) gives SINGLE.
4. `test_unknown_tenancy_mode_value_is_multi`.
5. `test_forced_single_with_orgs_audits_cross_tenant_bypass`.
6. `test_capabilities_exposes_tenancy_mode` (A1).

`tests/api/test_platform_admin_tenancy.py` (reuse `_org`/`_file` from
`tests/api/test_delete_permissions.py:30/38` and `org_context` from `tests/api/conftest.py:20`)
7. `test_admin_cannot_read_other_org_file_without_grant`: `GET /api/files/{uuid}` gives **403**
   (`crud.py` → helper tenant gate). *Before:* 200.
8. `test_admin_cannot_read_other_users_personal_file_in_multi_mode`: 403. *Before:* 200.
9. `test_admin_stream_url_is_tenant_gated`: `/files/{uuid}/stream-url` gives 403. *Before:* 200
   via the bare lookup at `crud.py:81-84`. The plan's first draft missed this.
10. `test_admin_thumbnail_is_tenant_gated`: non-public file of another tenant gives 403.
    *Before:* 200.
11. `test_admin_file_list_excludes_other_personal_workspaces`. *Before:* includes it.
12. `test_admin_cannot_delete_other_tenant_file`: 403, row survives. *Before:* 204. The existing
    `test_platform_admin_can_delete` stays green (community control).
13. `test_admin_cannot_reveal_unredacted_other_tenant`, parametrized over the detail
    (`crud.py:588`), segments, transcript export and subtitles: no unredacted text. *Before:* raw
    text.
14. `test_admin_profile_list_excludes_other_tenants` (`speaker_profiles.py:56`). *Before:*
    includes them.
15. `test_admin_task_lookup_and_retry_are_tenant_scoped` (`tasks.py:999`, `:867`). *Before:* 200.
16. `test_admin_reprocess_other_tenant_refused` (`reprocess.py:460`), with Celery `.delay`
    patched and asserted **not** called. *Before:* called.
17. `test_watch_source_assign_to_user_of_other_tenant_refused`: 400. *Before:* 200.
18. `test_community_admin_bypass_unchanged`: no org rows; tests 7/11/12 give 200/includes/204.
    Green before and after (control).

`tests/api/test_support_access.py` (all fail before with 404 on the new routes)
19. `test_pending_grant_does_not_authorize`: 403 `support_grant_not_active`.
20. `test_org_admin_approval_enables_read_and_records_use`: one `support_access_use`
    request-level row and one resource row with `organization_id == B` and `owner_user_id ==`
    the owner, plus a `SUPPORT_ACCESS_USED` audit call.
21. `test_read_grant_cannot_write_or_delete`: PUT and DELETE give 403
    `support_grant_write_required`, including a route that never calls `allows()` (an org tag
    create).
22. `test_grant_does_not_cross_to_another_org`: grant for B, file in C, gives 403 with no grant
    code.
23. `test_expired_and_revoked_grants_are_403_with_codes`.
24. `test_self_approval_refused` (`support_grant_self_approval`).
25. `test_demoted_admin_grant_stops_working` (`support_grant_invalid`).
26. `test_break_glass_requires_super_admin_reason_ticket_and_ttl_cap`:
    - admin gives 403;
    - a missing ticket gives 422;
    - 300 min gives 422;
    - a valid request is immediately usable, writes the `BREAK_GLASS` audit stamped with org B,
      and is listed for B's admin.
27. `test_header_in_single_mode_is_400`.
28. `test_grant_uuid_of_another_admin_is_403`.
29. `test_concurrent_approve_and_deny_one_wins`: two threads, one 200 and one 409
    `support_grant_already_decided`.
30. `test_approve_shorten_only`: above requested gives 422; shorter sets `expires_at` to match.
31. `test_lifecycle_routes_ignore_stale_header`: revoke / `GET /auth/session` /
    `GET /system/capabilities` with an expired grant header give 200 (A8).
32. `test_grant_refused_on_chat_search_upload_export` (`support_grant_action_not_permitted`).
33. `test_use_row_write_failure_is_503`: patch `record_use`'s session commit to raise. *Fails
    closed.*
34. `test_presign_ttl_clamped_under_grant`: `expires_in ≤ 300`.
35. `test_redaction_resolved_for_owner_under_grant`: owner has PII redaction on, admin off;
    under a grant the segments come back masked.
36. `test_lapsed_pending_is_409`, `test_revoke_idempotent`, and
    `test_grant_detail_404_for_non_grantee`.
37. `test_personal_break_glass_notifies_subject_and_refuses_own_workspace` (A11).

`tests/unit/test_platform_bypass_discipline.py` (structural guard, in the style of
`test_ws_event_quarantine_discipline.py`)
38. AST-scan `app/api` + `app/services` + `app/utils`, failing on any site not in an allowlist
    keyed `<file>::<function>` with a written reason. Flag:
    - `.is_admin` reads;
    - `is_admin=` / `allow_admin=` kwargs;
    - bare `get_file_by_uuid(` calls in `app/api`.

    Seed the allowlist with exactly the §3.3 (P) and §3.4 (Q) sites. *Before the migration* it
    lists every §3.1/§3.2 site. That listing is the red, and the work is done when it is empty.
39. Route walk (in the style of `test_lifecycle_gate_coverage.py`): every route whose
    dependency tree reaches `get_current_active_user` but **not** `get_current_context` must be
    in an allowlist with a reason, so the header can never be silently ignored on a content
    route.

## 9. Implementation order and worktrees

Backend first, then frontend. **Each phase is one commit**, made by the single writer in its
worktree (root `CLAUDE.md`: one writer commits). Each commit passes its gate before the next
phase starts.

**Worktree A: `feat/1122-tenancy-bypass-backend`** (`.claude/worktrees/1122-backend`, branched
from the active upstream). Use a `--fresh` stack (`./opentr.sh start dev --fresh t1122
--port-offset 200`, exporting the offset `*_PORT` vars) when other agents are active.

| Phase | Commit | Contents | Tests made green | Gate before committing |
|---|---|---|---|---|
| 1 | `feat(tenancy): add TENANCY_MODE and fail-closed mode detection` | §5.1, A1, A12 (compose + `.env.example`) | 1-6 | `pytest tests/unit/test_tenancy_mode.py -v`; pre-commit commit tier via `scripts/safe-precommit.sh run --all-files` |
| 2 | `security(tenancy): evaluate platform bypass on the loaded row` | §5.2 `PlatformBypass` + context split (grant resolution stubbed to "no grant"), §5.3 chokepoints, §5.4 inline sites, thumbnail, reveal, redaction subject, presign clamp, guards 38-39 | 7-18, 38, 39 | `./scripts/run-backend-tests.sh` (full unit/API) + `python3 scripts/audit-tests.py backend/tests` at 0 + commit tier |
| 3 | `feat(tenancy): support-access grants, use log and lifecycle API` | §5.5-5.6 (models, migration with derived number, detection arm, consistency test, service, routes, WS, audit types, seam bump), §6 contract | 19-37 | `./opentr.sh start dev` restart applies the migration cleanly over an existing DB **and** `./opentr.sh reset dev` on a `--fresh` stack (full chain); `alembic-head.py backend` prints exactly one head; full backend suite; audit-tests 0 |
| 4 | `docs(tenancy): document operator access and the TENANCY_MODE escape hatch` | §5.7 docs, CHANGELOG, docs-site page | none | `./scripts/run-integration-tests.sh` (the pre-merge gate), then both pre-commit tiers (`--all-files` and `--hook-stage pre-push`) |

Phase 2 must not merge without Phase 3. Without grants, MULTI operators would have only the
escape hatch. Phases 1-4 go on one branch and one PR.

**Worktree B: `feat/1122-support-access-frontend`** (`.claude/worktrees/1122-frontend`). It
branches from worktree A's Phase 3 commit, so it builds against the real contract. It is rebased
onto the merged backend before its own PR. It follows the frontend plan §7 phases **F0-F3**, one
commit each. Each commit passes `npm run check`, `npm run build`, `npm run test`,
`npm run test:audit` and `npm run check:i18n`, plus both pre-commit tiers before push. F3 runs
the `support_access` e2e marker on a `TENANCY_MODE=multi` `--fresh` stack and the unmarked
SINGLE control on the normal dev stack.

Merge order: the backend PR, then the frontend PR, both in the same release (v0.6.0). Use merge
commits, never squash. Never local-merge onto `master`.

## 10. Risks

- **A bypass site is missed.** Mitigated by guards 38-39, the method gate (rule 7), the
  request-level use row, and `refuse_under_support_grant` on whole routers. Any new `is_admin`
  read needs an allowlist reason.
- **Assumed-tenant membership.** An org grant makes the grantee look like a member of B to
  every `ctx.org_id`-scoped path: org tags (`tags/_common.py:69-72`), org collections, speaker
  profiles (`speaker_profiles.py:1003`). Reads there are within the grant's purpose and recorded
  at request level. Writes are blocked for read grants by method, and creation is refused for
  all grants (rule 8).
- **Revocation latency.** API access ends on the next request (no caching). The residuals are:
  - a presigned URL minted before revocation: ≤ 300 s, because of the clamp;
  - an in-flight request;
  - Celery work already dispatched under a write grant, which runs to completion (recorded at
    dispatch).

  All three are stated in the docs page.
- **Operator runbooks break on multi-org self-hosted installs** (by-UUID recovery,
  `/files/{uuid}/force`). Platform ops in §3.3 stay. By-UUID content verbs need a grant, and
  `TENANCY_MODE=single` restores the old behaviour, audited.
- **Use-log volume.** One request row plus one row per distinct resource per request. Grants are
  rare and short, so the volume is acceptable. It is indexed by `(grant_id, occurred_at)`.
- **Fail-closed use log.** A Postgres outage blocks support sessions. That is acceptable:
  Postgres down is an outage anyway.
- **Org-grant "assume tenant" sets `org_role=None`.** That stops a grant from conferring
  `org:admin` powers (member management, org GDPR erasure), and `/org-admin/*` uses the base
  context anyway. Test 21 and the existing `require_org_admin` tests cover it.
- **Personal-workspace grants are by-UUID only.** `sees_all_in_scope` is false, so lists don't
  widen. This is deliberate for v1.
- **Mode-detection cost.** A cached EXISTS query, latched once MULTI is positively seen.
- **Every `backend/app/*.py` edit hot-reloads the dev backend** and dispatches
  `search_index_maintenance`. Batch the edits per phase.

## Appendix A. Security review, 2026-10-09 (each finding verified in code)

| # | Sev | Finding | Evidence | Resolution in this plan |
|---|---|---|---|---|
| F1 | High | `get_media_file_by_uuid` short-circuits admins to a bare lookup that never reaches the chokepoint. The draft only said "pass bypass through", so `/stream-url` would have stayed cross-tenant | `api/endpoints/files/crud.py:81-84`; `files/__init__.py:647-649` | §5.3 deletes the branch; test 9 |
| F2 | High | Admin bare-lookup branches outside the helpers: reprocess (dispatches 8 Celery tasks incl. LLM), task retry, task by-id | `files/reprocess.py:460-462`; `tasks.py:867-870`, `:999-1000` | §5.3 rows; tests 15-16; guard 38 flags bare `get_file_by_uuid` |
| F3 | High | "Assume tenant" (`ctx.org_id = B`) gives the grantee member visibility on every `ctx.org_id`-scoped path, which never calls `allows()`. A read grant could then write (org tags, collections, uploads stamped into B), and nothing was audited | `api/deps_context.py:151-163`; `tags/_common.py:69-72`; `utils/db_helpers.py:25-58` | §4 rules 7-8, request-level use row (§5.6), test 21 |
| F4 | High | Unredacted reveal missed on the main transcript path: the draft fixed only subtitles and export | `files/crud.py:588` (used by detail and `files/segments.py:71`) | §5.4; test 13 parametrized |
| F5 | Medium | Under a grant, redaction is resolved with the **caller's** prefs, so a support viewer whose prefs are looser sees PII the owner's policy masks. That contradicts §4 rule 6 | `files/crud.py:574`; `services/redaction/config.py:228-247` | §5.4 resolves for the owner; test 35 |
| F6 | Medium | Presigned media URLs live 6 h, are not user-bound and survive revocation | `core/config.py:577`; `services/minio_service.py:143-213`; the docstring's "5 minutes" at `files/__init__.py:619` is wrong | §5.4 presign clamp ≤ 300 s; test 34 |
| F7 | Medium | Thumbnail route bypasses `get_current_context`, so neither the bypass removal nor grant resolution would reach it | `files/__init__.py:1103-1179` (`:1151`, `:1159`, `:1167`, `:1172`) | §5.2 shared resolver (A9); test 10 |
| F8 | Medium | `EventSource` can't send the grant header, so download-stream breaks under a grant. Making it work would need a query-string credential | frontend `lib/fileDetail/downloadStream.ts:91`; `routes/+page.svelte:963` | §4 rule 9 refuses downloads/exports under grants (D1) |
| F9 | Medium | Audit is best-effort: never raises, OpenSearch `replicas: 0`, a no-op when disabled. The draft's "every use is audited" was unenforceable (AU-9) | `auth/audit.py:233-258`, `:291-293`, `:365` | §5.6 Postgres use log, fail-closed 503; test 33 |
| F10 | Medium | Approve/deny race: the draft had no atomic decision, so two approvers (or approve vs revoke) could both "win" | design gap (no locking specified) | §5.5 conditional UPDATE; test 29 |
| F11 | Medium | `CASCADE` FKs on target org/user deleted the grant evidence when the tenant or user was erased | draft §5.5 | SET NULL + `target_kind`; use rows store ids without FKs |
| F12 | Low | Latching MULTI on any MULTI result flips a community install to MULTI for the process lifetime after one transient DB error | draft §5.1 | latch only on positive detection; test 3 |
| F13 | Low | `speaker_profiles.py:56-58` has no org filter at all for admins, not just a widening | `api/endpoints/speaker_profiles.py:56-58` | §5.4; test 14 |
| F14 | Low | Owner facet lists every owner in personal scope | `files/filtering.py:429` | §5.4 |
| F15 | Low | Retention preview and quarantine list return cross-tenant filenames + owner emails, classed as "metadata" | `admin.py:1326-1327`, `:2966-3044` | metadata-access audit (§5.4); masking is D3 |
| F16 | Low | Migration number `v431` is taken, and the draft omitted the detection arm and consistency test required by `app/db/CLAUDE.md` step 4 | `backend/alembic/versions/v431_add_media_duration_provenance.py:52-53` | §5.5 derived number + detection arm |
| F17 | Info | Test expectations of 404 contradict the helper (403 for file/collection) | `utils/uuid_helpers.py:313`, `:377` vs `:500`, `:540` | tests pin 403; unification is D4 |
| F18 | Info | `smart_speaker_suggestion_service._check_opensearch_profiles_exist` has no org clause (size-1 existence probe, owner-scoped) | `services/smart_speaker_suggestion_service.py:99-103` | not a cross-tenant read; noted only |

Checked and **not** a gap:
- **Search and chat** have no admin tenant bypass (`hybrid_search_service.py:1609-1653`,
  `context_resolver.py:105-111`).
- **Demotion takes effect on the next request**, because the role is re-read from the DB
  (`auth/dependencies.py:684-697`).
- **The header can't be forged by CSRF**: CORS `allow_headers` is an explicit list
  (`main.py:1067-1074`).
- **`require_org_admin` has no platform bypass** (`deps_context.py:143`).
- **There is no impersonation feature.**

## Appendix B. Decisions for a human

- **D1. Downloads/exports under a grant.** Default: refused in v1. Allowing them needs a
  grant-bound signed job id for the SSE leg, and a clear statement that support can exfiltrate
  media.
- **D2. Approver independence.** Default: a platform admin may not approve another platform
  admin's request, even when they hold `org:admin` in the target org. Lockbox expects the
  customer to approve, not staff. Relax this only if self-hosted orgs routinely make the operator
  an org admin.
- **D3. Cross-tenant filenames in P routes** (retention preview, quarantine list). Default:
  audited, not masked. Masking changes the admin UX.
- **D4. Cross-tenant 403 vs 404.** Default: keep the current per-helper codes. Uniform 404
  removes an existence oracle, but touches every tenant-gated test and the frontend's 403
  handling.
- **D5. Retention of `support_access_use` and of grant `reason` text** after a GDPR erasure.
  Default: retain ids-only use rows (they hold no content) and the grant row with `reason`. Legal
  should confirm that keeping the reason text is acceptable.
- **D6. Step-up authentication for break-glass.** There is no re-auth primitive for local
  accounts today (only the external `externalReauthenticate` seam, `auth/constants.py:54`).
  Recommended for FedRAMP-style deployments; out of scope for v1 unless required.
- **D7. Grantee identity shown to tenants.** The contract exposes the grantee's name and email to
  approvers (Lockbox does). Confirm that this is acceptable for staff privacy.
