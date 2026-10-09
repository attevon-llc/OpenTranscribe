# #1122: Platform-admin bypass vs. organization scoping. Design

Status: **PLAN ONLY.** Nothing here is implemented. Written 2026-10-09 against `master` @ `c3a84221`.
Issue: [#1122](https://github.com/attevon-llc/OpenTranscribe/issues/1122) (milestone v0.6.0,
`epic:compliance`). Found during #1103 / PR #1119. Related: `docs/security/tenant-boundaries.md`.

## 1. Problem

`User.is_admin` (`role in {admin, super_admin}`, `backend/app/models/user.py:258`) works as a
**global content bypass**. On every file, speaker, comment, collection and tag path it is checked
*before* the tenant gate, so a platform admin can read, edit and delete any tenant's content by
UUID. Lists also widen to "every user in the active scope", and in personal scope that covers
every user's personal workspace. On a community install there is one tenant, so this is correct
there. On a multi-tenant deployment (cloud edition, or a self-hosted install with organizations)
it means one admin credential gives implicit, unaudited read/write access to every tenant.

`docs/security/tenant-boundaries.md:23` currently says this is by design ("Instance administrators
keep instance-wide operational access"). This plan narrows that statement. Operational access
stays. Access to content becomes explicit, separately authorized, time-boxed and audited.

## 2. Industry baseline this design follows

- **Least privilege / tenant-scoped admin by default.** OWASP Multi-Tenancy Cheat Sheet: enforce
  tenant context on every data access, and don't let a privileged role skip it implicitly. Tenant
  administration belongs to the tenant (`org:admin`), and platform staff hold no standing access.
- **Customer-approved operator access.** Azure *Customer Lockbox* and Google Cloud *Access
  Approval*: an operator requests access with a justification, the customer approves it, and it
  expires automatically.
- **Break-glass, visible to the customer.** Google *Access Transparency* and AWS break-glass
  guidance: emergency access is possible without approval, but it is limited to a few
  principals, short-lived, carries a mandatory reason, and is logged where the customer can read
  it.
- **Fail closed.** If the system can't tell whether a deployment is multi-tenant, or a grant
  can't be validated, access is denied.
- **Audit every cross-tenant use** (FedRAMP AU-2/AU-3, which `app/auth/audit.py` already targets).

## 3. Current-state inventory

Paths are relative to `backend/app/`. Classes: **X** = cross-tenant content exposure (must be
gated). **P** = legitimate platform operation (keep; metadata/ops only). **Q** = quarantine-review
visibility only, not a tenant bypass (keep on `is_admin`).

### 3.1 Chokepoints (X): fixing these covers most routes

| Site | Behaviour today |
|---|---|
| `utils/uuid_helpers.py:294` `get_file_by_uuid_with_permission` | `if is_admin: return file`, **before** the tenant gate (`:313`). Used by ~48 `is_admin=current_user.is_admin` call sites in `api/endpoints/{files/*,comments,speakers,speaker_clusters,summarization,topics,tags/*,tasks,transcript_segments,user_files,search,chat/*}.py` |
| `utils/uuid_helpers.py:372` / `:436` | collection lookup bypass; `_with_sharing` returns effective `"owner"` |
| `utils/uuid_helpers.py:490` `require_speaker_access` | unconditional `user.is_admin` bypass |
| `utils/uuid_helpers.py:535` `require_profile_in_scope(allow_admin=)`, `:225` `require_resource_owner(allow_admin=)` (caller `api/endpoints/comments.py:285`) | owner/tenant gate bypass |
| `services/delete_permissions.py:55`, `:110` | admin may delete any file in any tenant (#1103) |
| `api/endpoints/files/crud.py:96` `get_media_file_by_id` (`is_admin` skips `owned_in_tenant`) and `crud.py:912/1056/1151/1240/1354` (detail, update, delete, segment edit, stream info) | X |
| `services/tag_bulk.py:159` `_require_editor` | X (write) |

### 3.2 Inline admin branches (X)

| Site | Exposure |
|---|---|
| `api/endpoints/files/__init__.py:292` list | admin sees every user's files in scope; in personal scope (`org_pred = organization_id IS NULL`) that is **every personal workspace** |
| `files/__init__.py:424, 501, 581, 861, 927, 1069/1084, 1201, 1245, 1268` | filters, info, stream URL, download prepare/stream, thumbnail, cache clear, analytics |
| `files/subtitles.py:73`, `files/transcript_export.py:78` | `can_reveal = owner or is_admin`: the admin can **reveal unredacted PII** of any tenant |
| `files/management.py:111, 211, 263, 367, 413, 448, 1063` | status detail, cancel, retry, recover, force-delete, stuck list, bulk action |
| `files/waveform.py:263, 305, 358`; `files/segments.py:66`; `files/reprocess.py:443` | per-file reads/writes |
| `tasks.py:100, 143, 273, 867-898, 979, 999` | task list / progress / retry / by-id lookups ignore tenant entirely for admin |
| `speakers.py:180, 275, 840, 1150-1208, 1777, 2624, 2668` | speaker lists, cross-media debug (`/debug/cross-media-by-name` returns every user's speakers), rename-everywhere (`restrict_to_user_id=None`) |
| `speaker_profiles.py:56` (list **all** profiles of all tenants), `:369` | X |
| `comments.py:221, 331`; `transcript_segments.py:123`; `media_collections.py:1062` | read/delete/edit |
| `watch_sources.py:210` (explicit "admins operate every tenant's sources"), `:585` `scope=all`, `:637` `assign_to_user_uuid` (can assign to a user of **another tenant**) | X |
| `user_files.py:267, 388` | X |

### 3.3 Platform operations (P), which stay admin-gated and unchanged

`api/endpoints/admin.py`: user/role/session/MFA management, settings, retention (preview lists
filenames, `:1320`), data-integrity, embedding-consistency, imohash, gpu-profiles, stats (recent
tasks, `:775`), audit logs, `POST /admin/gdpr/erase-user`, quarantine list/quarantine/release
(`:2824-2915`). Also `users.py` list/detail, `search.py:261/1138` (instance counts),
`tags/crud.py:190` cleanup, `tags/operations.py:82` promote, `files/waveform.py:413/477` bulk
backfill, `files/management.py:1174` bulk stuck recovery, and `core/capabilities.py:315`
(superuser sees capability-hidden *surfaces*, never data; `require_org_admin` still 403s them).
These expose account metadata and filenames, not transcript/media content. They are the
operator's job and keep working in every mode.

### 3.4 Quarantine visibility (Q), unchanged

`exclude_quarantined(include_quarantined=is_admin)` and `is_hidden_for(..., is_admin=)` in
`files/__init__.py`, `tasks.py:107/149`, `user_files.py:102/150/199`, `media_collections.py:336/479/1072`,
`speaker_clusters.py:168/230`, `speaker_profiles.py:771`, `search.py:555`,
`services/search/hybrid_search_service.py:1409/1541/1731`, `services/tag_collisions.py:543`,
`api/endpoints/chat/citation_takedown.py:61/91`, `services/takedown_service.py:311`. These only
decide whether a taken-down row inside an already tenant-scoped result is shown. They never widen
tenant scope. Search (`hybrid_search_service._build_filters`) and chat (`chat/common.py:get_owned_conversation`)
already have **no** tenant bypass.

## 4. Decision

1. **Tenancy mode** is explicit and fails closed (§5.1). In **single-tenant** mode the bypass
   keeps today's behaviour exactly (community invariance).
2. In **multi-tenant** mode, `role in {admin, super_admin}` grants **no implicit content access**.
   An admin's content rights inside a tenant are whatever their membership gives them
   (`org:admin` / `org:member` / owner / share). Platform operations (§3.3) are unaffected.
3. Operator access to tenant content requires a **support-access grant**:
   - **Approved grant** (Lockbox model). An `admin`/`super_admin` requests access with a reason,
     access level (`read` | `write`) and duration. An `org:admin` of the target org approves it
     (or the subject user, for a personal workspace). TTL ≤ 8 h. Self-approval is refused.
   - **Break-glass grant**. `super_admin` only, active immediately, reason **and** ticket reference
     required, TTL ≤ 4 h (default 60 min). The tenant's org admins are notified, and the grant
     appears in their audit log and support-access list.
4. A grant is **never ambient**. It applies only to requests carrying
   `X-Support-Access-Grant: <grant uuid>`. The grant is bound to the grantee's user id, so a
   leaked uuid is useless to anyone else. With an org grant, the request *assumes* that tenant
   (`ctx.org_id = grant.organization_id`, `org_role = None`), so every existing scoped query works
   unchanged, and the bypass applies only to resources of that tenant.
5. **Every** authorization a grant makes emits `support_access.used`, stamped with the tenant's
   `organization_id` and `target_user_id` = resource owner. That makes it visible in
   `GET /org-admin/audit-logs` (`auth/audit.py:618` `build_org_scope_clause` includes
   org-stamped events).
6. Unredacted reveal (`?redact=false`) is **owner-only** in multi-tenant mode. A grant never
   unlocks it. Support works on what the tenant's redaction policy shows.

Rejected alternatives: "> 1 org" detection (a personal workspace plus one org is already two
tenants); an admin-UI (`SystemSettings`) toggle for the mode (the control constrains admins, so
admins must not be able to switch it off: separation of duties); a session-wide "support mode"
cookie (gives ambient authority that leaks into ordinary browsing).

## 5. Exact changes

### 5.1 Tenancy mode: `core/config.py`, new `services/platform_access.py`

- `config.py`: `TENANCY_MODE: str = os.getenv("TENANCY_MODE", "auto")`. Add it to `.env.example`
  (advanced section) with a one-line explanation.
- `services/platform_access.py` (new, under 300 lines):

```python
class TenancyMode(StrEnum): SINGLE = "single"; MULTI = "multi"

def tenancy_mode(db: Session) -> TenancyMode:
    """auto: MULTI iff DEPLOYMENT_EDITION != 'community' OR any active Organization row exists.
    'single'/'multi' force. Unknown value -> MULTI + logger.error. DB error -> MULTI (fail closed).
    Cached in-process for SETTINGS_CACHE_TTL; once MULTI is observed it latches for the process.
    Cache fully bypassed when TESTING=true (same rule as core/settings_cache.py)."""
```

- On startup (`main.py` lifespan): if `TENANCY_MODE=single` is forced while active orgs exist, log
  a WARNING once. In that state every cross-tenant bypass use is audited (§5.3).

### 5.2 `PlatformBypass` and `RequestContext`: `services/platform_access.py`, `api/deps_context.py`

```python
@dataclass(frozen=True)
class PlatformBypass:
    user: User | None
    mode: TenancyMode
    grant: SupportGrantView | None        # validated, frozen snapshot (uuid, org_id, subject_user_id, level, mode)
    _audited: set = field(default_factory=set, compare=False)  # per-request dedupe

    @classmethod
    def none(cls) -> "PlatformBypass": ...
    def allows(self, *, org_id: int | None, owner_id: int, need: Literal["read", "write"],
               resource_type: str, resource_uuid: str) -> bool:
        # single: return user.is_admin (audit only when forced-single + cross-tenant)
        # multi, no grant: False
        # multi, grant: tenant match (org grant: org_id == grant.org_id;
        #   personal grant: org_id is None and owner_id == grant.subject_user_id)
        #   and (need == "read" or grant.level == "write") -> audit SUPPORT_ACCESS_USED once per
        #   (resource_type, resource_uuid, need) per request -> True
    @property
    def user_is_admin(self) -> bool:      # quarantine-review visibility (Q) only, never tenant scope
        return bool(self.user and self.user.is_admin)
    @property
    def sees_all_in_scope(self) -> bool:
        # single: user.is_admin; multi: grant is an ORG grant (lists of the assumed tenant)
```

`api/deps_context.py`:
- `RequestContext` gains `bypass: PlatformBypass = PlatformBypass.none()` and
  `support_grant_uuid: str | None = None`.
- `get_current_context`: after `resolve_org_context`, read header `X-Support-Access-Grant`.
  If present: `mode` must be MULTI, otherwise **400**. Call
  `support_access_service.resolve_active_grant(db, uuid, current_user)`, which returns the grant
  or raises **403** (wrong grantee, not approved, revoked, expired, not yet started, grantee no
  longer `is_admin`, or target org inactive). Never fall back silently to normal scope. For an org
  grant, override `org_id = grant.organization_id` and `org_role = None`, and set
  `request.state.org_id`. Always build `bypass = PlatformBypass(user, mode, grant)`.
- `scope_to_context` is unchanged.

### 5.3 Chokepoint signatures (mechanical migration)

Replace the `is_admin: bool` / `allow_admin: bool` parameter with `bypass: PlatformBypass`
(keyword-only, default `PlatformBypass.none()`), and evaluate it **after** loading the row, with
the row's tenant:

| Function | New check (replaces the early `if is_admin: return`) |
|---|---|
| `uuid_helpers.get_file_by_uuid_with_permission` | `if bypass.allows(org_id=file.organization_id, owner_id=file.user_id, need="write" if min_permission != "viewer" else "read", resource_type="media_file", resource_uuid=str(file.uuid)): return file`. The takedown check keeps `is_hidden_for(file, is_admin=bypass.user_is_admin)` (Q) |
| `get_collection_by_uuid_with_permission` / `_with_sharing` | same, on `collection.organization_id/user_id`; `_with_sharing` returns `"owner"` only for a write-capable bypass, else `"viewer"` |
| `require_speaker_access`, `require_profile_in_scope`, `require_resource_owner` | same, on the speaker's file / profile / resource |
| `services/delete_permissions.can_delete_file` / `get_deletable_file` | `bypass.allows(..., need="write")` replaces `user.is_admin` at `:55` and `:110` |
| `files/crud.get_media_file_by_id`, `get_media_file_by_uuid` | pass `bypass` through |
| `services/tag_bulk._require_editor` | take `bypass` |

Call sites: `is_admin=current_user.is_admin` becomes `bypass=ctx.bypass`. A route that depends
only on `get_current_active_user` must add `ctx: RequestContext = Depends(get_current_context)`
(it already sits behind the lifecycle gate, see `api/CLAUDE.md`).

### 5.4 Inline sites (§3.2)

- "Admin sees all" list branches (`files/__init__.py:292`, `tasks.py:100/143/979/999`,
  `speakers.py:180/275/840/1150-1208/2624/2668`, `speaker_profiles.py:56`, `watch_sources.py:585`,
  `user_files.py`): `if current_user.is_admin` becomes `if ctx.bypass.sees_all_in_scope`, and the
  tenant predicate (`org_scope_pred` / `owned_in_tenant` with `ctx.org_id`) **must still apply**
  in that branch. `tasks.py:273` (Redis progress) filters to file uuids in scope unless
  `sees_all_in_scope`.
- `speakers.py:1777`, `speaker_profiles.py:369`: `restrict_to_user_id=None` only when
  `ctx.bypass.sees_all_in_scope`. Also pass the tenant so a rename never crosses tenants.
- `speakers.py:1138` `/debug/cross-media-by-name`: in MULTI mode, scope the queries with
  `org_scope_pred(MediaFile.organization_id, ctx.org_id)`.
- `files/subtitles.py:73`, `files/transcript_export.py:78`:
  `can_reveal = owner or (ctx.bypass.mode is SINGLE and current_user.is_admin)`.
- `watch_sources.py:210/213`: `_get_source_or_404` uses
  `ctx.bypass.allows(org_id=source.organization_id, owner_id=source.user_id, ...)`. At `:637`
  (assign to user), in MULTI mode require `tenant_sharing.user_in_tenant(target, ctx.org_id)`,
  else 400.
- `comments.py:221/331`, `transcript_segments.py:123`, `media_collections.py:1062`,
  `files/waveform.py:263/305/358`, `files/segments.py:66`, `files/reprocess.py:443`,
  `files/management.py:111-448/1063`: use `ctx.bypass.allows(...)` on the loaded row.
  `force_delete_file` (`:413`) keeps its `is_admin` precondition **and** goes through
  `get_deletable_file(bypass=...)`.

### 5.5 Support-access grants (new)

**Model** `models/support_access.py`, table `support_access_grant`:
`id`, `uuid` (uuid7), `grantee_user_id` FK user **SET NULL** (actor convention since v387; add a
disposition to `tests/unit/test_user_deletion_fk_coverage.py`), `organization_id` FK organization
CASCADE NULL, `subject_user_id` FK user CASCADE NULL, `access_level`, `grant_mode`
(`approved`|`break_glass`), `reason` Text, `ticket_ref` String(255) NULL, `requested_at`,
`decided_by_user_id` FK SET NULL, `decided_at`, `decision` (`approved`|`denied`) NULL, `starts_at`,
`expires_at`, `revoked_at`, `revoked_by_user_id` FK SET NULL.
CHECKs: `(organization_id IS NULL) <> (subject_user_id IS NULL)`;
`access_level IN ('read','write')`; `grant_mode IN (...)`; `expires_at > starts_at`;
`char_length(reason) >= 10`; `grant_mode <> 'break_glass' OR ticket_ref IS NOT NULL`.
Indexes: `(grantee_user_id, expires_at)` and `(organization_id)`.

**Migration** `alembic/versions/v431_support_access_grant.py`. Read `backend/alembic/CLAUDE.md`
first, and confirm the current head with `scripts/release-tests/lib/alembic-head.py` (v430 at
time of writing). Create the table only, with no data migration. Never autogenerate inside the
hot-reload container.

**Constants** (`core/constants.py`): `SUPPORT_ACCESS_MAX_TTL_MINUTES = 480`,
`SUPPORT_ACCESS_BREAK_GLASS_MAX_TTL_MINUTES = 240`, `SUPPORT_ACCESS_DEFAULT_TTL_MINUTES = 60`,
`SUPPORT_ACCESS_PENDING_EXPIRY_HOURS = 72`. These are code constants, not settings, on purpose.

**Service** `services/support_access_service.py`: `request_grant`, `break_glass`, `approve`,
`deny`, `revoke`, `resolve_active_grant`, `list_for_org`, `list_for_grantee`. Each lifecycle
call writes its audit event (below). After `approve`, an approved grant starts at `decided_at`,
and its expiry is `decided_at + requested duration`. A pending grant older than
`SUPPORT_ACCESS_PENDING_EXPIRY_HOURS` can't be approved.

**Routes** (new router `api/endpoints/support_access.py`, mounted at `/support-access`, plus
additions to `api/endpoints/org_admin.py`). Every route 404s in SINGLE mode through a
`require_multi_tenant` dependency:

| Route | Authority |
|---|---|
| `POST /support-access/grants` | `get_current_admin_user` |
| `POST /support-access/grants/break-glass` | `get_current_active_superuser` |
| `GET /support-access/grants` | admin: own; super_admin: all |
| `POST /support-access/grants/{uuid}/revoke` | grantee, any super_admin, or `org:admin` of the grant's org |
| `GET /org-admin/support-access` | `require_org_admin` (grants targeting `ctx.org_id`) |
| `POST /org-admin/support-access/{uuid}/{approve,deny}` | `require_org_admin`, grant org == `ctx.org_id`, approver != grantee |
| `GET /users/me/support-access`, `POST /users/me/support-access/{uuid}/{approve,deny}` | subject user of a personal-workspace grant |

Add the routes to `tests/unit/test_route_privilege_tiers.py` expectations, and add the
`/org-admin/*` additions to the existing `require_capability("organizations")` mount.

**Audit** (`auth/audit.py` `AuditEventType`): `SUPPORT_ACCESS_REQUESTED`, `_APPROVED`, `_DENIED`,
`_REVOKED`, `_BREAK_GLASS`, `_USED`, plus `PLATFORM_ADMIN_CONTENT_ACCESS` (forced-single bypass
across tenants). Always set `user_id` = actor, `organization_id` = target org,
`target_user_id` = subject/owner. Details carry `grant_uuid`, `resource_type`, `resource_uuid`,
`access_level` and route template, **never** content. Break-glass also calls `send_ws_event` to
every `org:admin` of the target org (the event names no `MediaFile`, so the raw primitive is
allowed; add it to the allowlist in `tests/unit/test_ws_event_quarantine_discipline.py` with
that reason).

**Cloud seam:** bump `CLOUD_SEAM_VERSION` 7 → 8 (`auth/constants.py:108`). Staff tooling in the
managed edition now needs a grant.

### 5.6 Docs

- `docs/security/tenant-boundaries.md:23`: replace the bullet with the §4 rule, and add an
  "Operator access" table row (grant routes, `PlatformBypass.allows`, test file).
- `backend/app/auth/CLAUDE.md` "Privilege tiers": add the sentence "admin/super_admin manage the
  platform; in multi-tenant mode they reach tenant content only through a support-access grant".
- `backend/app/api/CLAUDE.md`: change `uuid_helpers` docs from "admin bypass first" to "bypass
  evaluated on the loaded row".
- `backend/app/utils/CLAUDE.md` (order note for `get_file_by_uuid_with_permission`).
- `docs-site/docs/` admin guide page "Support access & break-glass", plus a CHANGELOG `Security`
  entry naming the behaviour change and the `TENANCY_MODE=single` escape hatch.

## 6. Back-compat / migration for existing deployments

| Deployment | Mode after upgrade | Change |
|---|---|---|
| Community, no `organization` rows | SINGLE (auto) | **none**: `allows()` returns `user.is_admin`, `sees_all_in_scope` = `is_admin`, reveal unchanged, no new audit events |
| Self-hosted with orgs | MULTI (auto) | admins lose implicit content access; platform ops unchanged; must use grants. Escape hatch: `TENANCY_MODE=single` (startup WARNING plus `PLATFORM_ADMIN_CONTENT_ACCESS` audit on every cross-tenant use) |
| Cloud edition | MULTI | same, plus seam bump |

The only schema change is a new table. Rolling back drops the table and restores the old code.
No existing row changes.

## 7. Test plan (red-first)

Prove each test red against the old code in a `git archive HEAD` tree (root `CLAUDE.md`), never
by swapping files in the checkout. Run against the stack (`./opentr.sh start dev`, or `--fresh`
when other agents are active), then run `python3 scripts/audit-tests.py backend/tests` to 0
findings. Creating an `Organization` row in a test flips auto mode to MULTI, because the cache is
bypassed under `TESTING=true`.

`tests/unit/test_tenancy_mode.py`
1. `test_no_orgs_community_is_single`: control, green before and after.
2. `test_active_org_row_makes_auto_multi`: fails before (`ImportError`: no `tenancy_mode`).
3. `test_db_error_fails_closed_to_multi`: monkeypatch the org query to raise and assert MULTI.
4. `test_unknown_tenancy_mode_value_is_multi`.
5. `test_forced_single_with_orgs_audits_cross_tenant_bypass`.

`tests/api/test_platform_admin_tenancy.py` (reuse `_org`/`_file` helpers from
`tests/api/test_delete_permissions.py` and the `org_context` fixture from `tests/api/conftest.py`)
6. `test_admin_cannot_read_other_org_file_without_grant`: org B file, admin
   `GET /api/files/{uuid}` → **404**. *Before:* 200 (bypass at `uuid_helpers.py:294`).
7. `test_admin_cannot_read_other_users_personal_file_in_multi_mode` → 404. *Before:* 200.
8. `test_admin_file_list_excludes_other_personal_workspaces`: `GET /api/files` doesn't contain
   `normal_user`'s personal file. *Before:* contains it (`files/__init__.py:292`).
9. `test_admin_cannot_delete_other_tenant_file`: 404 and the row still exists. *Before:* 204
   (`delete_permissions.py:110`). The existing `test_platform_admin_can_delete` stays green
   (community control).
10. `test_admin_cannot_reveal_unredacted_other_tenant`: `?redact=false` → 404. *Before:* 200
    with raw text.
11. `test_admin_profile_list_excludes_other_tenants` (`speaker_profiles.py:56`). *Before:*
    includes them.
12. `test_admin_task_lookup_is_tenant_scoped` (`tasks.py:999`). *Before:* 200.
13. `test_watch_source_assign_to_user_of_other_tenant_refused` → 400. *Before:* 200.
14. `test_community_admin_bypass_unchanged`: no org rows; tests 6/8/9 return 200/includes/204.
    Green before and after (control).

`tests/api/test_support_access.py` (all fail before with 404 on the new routes)
15. `test_pending_grant_does_not_authorize`: header with a pending grant → 403.
16. `test_org_admin_approval_enables_read_and_audits`: patch `audit_logger.log` and assert one
    `SUPPORT_ACCESS_USED` with `organization_id == B`, `target_user_id == owner`.
17. `test_read_grant_cannot_write_or_delete`: PUT → 403, DELETE → 403.
18. `test_grant_does_not_cross_to_another_org`: grant for B, file in C → 404.
19. `test_expired_and_revoked_grants_are_403`: set `expires_at` in the past, then revoke.
20. `test_self_approval_refused`: grantee who is also `org:admin` of B → 403.
21. `test_demoted_admin_grant_stops_working`.
22. `test_break_glass_requires_super_admin_reason_ticket_and_ttl_cap`: admin → 403; missing
    ticket → 422; 300 min → 422; valid → immediately usable, `SUPPORT_ACCESS_BREAK_GLASS` stamped
    with org B, listed in `GET /org-admin/support-access` for B's admin.
23. `test_header_in_single_mode_is_400`.
24. `test_grant_uuid_of_another_admin_is_403` (binding to grantee).

`tests/unit/test_platform_bypass_discipline.py` (structural guard, in the style of
`test_ws_event_quarantine_discipline.py`)
25. AST-scan `app/api` + `app/services` + `app/utils` for `.is_admin` reads and `is_admin=` /
    `allow_admin=` kwargs. Fail on any site not in an allowlist keyed `<file>::<function>` with a
    written reason, seeded with exactly the §3.3 (P) and §3.4 (Q) sites. *Before the migration:*
    lists every §3.1/§3.2 site. That listing is the red, and the work is done when it is empty.

## 8. Rollout order (one PR, one commit per phase, worktree → branch → PR)

1. Phase 0: `TENANCY_MODE`, `tenancy_mode()`, tests 1-5. No behaviour change.
2. Phase 1: `PlatformBypass`, `RequestContext.bypass`, chokepoints (§5.3), call-site rename;
   tests 6, 7, 9, 10, 14.
3. Phase 2: inline sites (§5.4) and the structural guard; tests 8, 11-13, 25.
4. Phase 3: grant model, migration, service, routes, header resolution, audit types, seam bump;
   tests 15-24.
5. Phase 4: docs (§5.6), `.env.example`, CHANGELOG. Gate: `./scripts/run-dev-tests.sh --full` and
   `./scripts/run-integration-tests.sh`, then both pre-commit tiers through
   `scripts/safe-precommit.sh`.
6. Follow-up PR, same epic: frontend. An org-admin "Support access" panel (approve/deny/revoke),
   an admin "Request access / break-glass" dialog, a persistent banner while a grant is in use,
   and header injection in the API client **only** for requests made inside a support session.
   All copy in all 12 locales (check `ar` RTL), with light/dark parity. Phases 1-2 must not merge
   without Phase 3. Without grants, multi-tenant operators would be left with only the escape
   hatch.

## 9. Risks

- **A bypass site is missed.** Mitigated by the AST guard (test 25) and the
  `test_lifecycle_gate_coverage`-style route walk. Any new `is_admin` read needs an allowlist
  reason.
- **Operator runbooks break on multi-org self-hosted installs** (by-UUID recovery,
  `/files/{uuid}/force`). Platform ops in §3.3 stay. By-UUID content verbs need a grant, and
  `TENANCY_MODE=single` restores the old behaviour, audited.
- **Audit volume** from per-resource `SUPPORT_ACCESS_USED`. Deduped per request. Grants are
  rare, so the volume is acceptable.
- **Org-grant "assume tenant" sets `org_role=None`.** That stops a grant from conferring
  `org:admin` powers (member management, org GDPR erasure). Test 17 together with the existing
  `require_org_admin` tests covers this.
- **Personal-workspace grants are by-UUID only.** `sees_all_in_scope` is false, so lists don't
  widen. This is deliberate for v1.
- **Mode-detection cost.** A cached EXISTS query, latched once MULTI is seen. `TESTING=true`
  bypasses the cache, so tests see fresh state.
- **Every `backend/app/*.py` edit hot-reloads the dev backend** and dispatches
  `search_index_maintenance`. Batch the edits per phase.
