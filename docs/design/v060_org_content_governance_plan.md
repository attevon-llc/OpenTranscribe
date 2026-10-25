# v0.6.0 organization content governance: restricted collections, org legal hold, content-access audit, grant proof

> **Owner decisions of 2026-10-25 supersede this plan's open questions:** see `docs/design/v060_owner_decisions.md` (open-source/commercial-licence rule, CJK font pack, governance defaults, gender notice, #1132 moved to v0.9.0, adjustable reranker threads, migration numbering).

**Status:** PLAN ONLY. Nothing here is implemented.
**Written:** 2026-10-25 against `master` @ `a236125a` (Alembic head `v444_add_takedown_notification`).
**Issues:** #1275 (restricted org collections), #1276 (org-admin legal hold), #1277 (content-access
audit + org-admin audit UI), #1281 (prove every platform act-as path needs a grant). All v0.6.0.
**Companions:** `docs/design/1122_platform_admin_tenancy_bypass_plan.md` (grants, `PlatformBypass`;
implemented), `docs/abuse-and-takedown.md` (quarantine), `docs/security/tenant-boundaries.md`.

Written for an implementation agent with **no** conversation history. Paths are relative to the
repo root; `app/` means `backend/app/`. Line numbers drift: search for the quoted symbol.

---

## 0. TL;DR

The four issues share three primitives. Build each **once**, then hang the features on them:

1. **One collection-visibility predicate** (`app/services/collection_visibility.py`) used by every
   surface that can name a collection or reach a file through one.
2. **One content-audit write path** (`app/services/content_audit.py`) with typed top-level fields,
   a fixed tenant-scope rule, and Redis de-dup for view-class events.
3. **One hold check** (`app/services/hold_guard.py`): "is this file deletion-blocked, and by whom",
   consulted by every delete/purge/retention/erasure path. Org holds live in their **own tables**;
   the platform takedown `media_file.legal_hold` column keeps its meaning, so quarantine, platform
   hold and org hold stay three distinct things.

Plus the **grant guard matrix** for #1281: a checked-in TSV of every admin-reachable route and
every `PlatformBypass.allows` site, with a test that fails on an unclassified or stale row.

Two verified findings change the issues' framing (section 1): org collections today give members
file access on **some** surfaces and not others (F1), and the org audit read leaks a member's
**personal-scope** events to their org admin (F5). Both are fixed here because the features sit
directly on them.

---

## 1. Verified current state

### 1.1 Collections (#1275)

- `org_collection_permission` (`app/services/permission_service.py:43-55`): any member = `editor`,
  creator / `org:admin` = `owner`. `get_collection_permission` (`:95-173`) and
  `get_accessible_collection_ids` (`:376-435`) apply it with no visibility notion.
- **"Named" people already exist:** `CollectionShare` (`app/models/sharing.py:30-60`, user or group
  target, `viewer|editor`). Share targets for org collections are already filtered to org members
  (`media_collections.py:_get_share_target_user_ids`, ~line 120). No new membership table is needed.
- Name uniqueness: `uq_collection_org_name (organization_id, name) WHERE organization_id IS NOT
  NULL` (`app/models/media.py:~985`). The create/rename 409 echoes the name
  (`media_collections.py:670`, `:796`).
- **F1, issue premise incomplete: org-collection membership is a file-read route on some surfaces
  only.**
  - **Grants read:** `GET /collections/{uuid}/media` shows every member's file
    (`media_collections.py:~1060`, "all org files are visible to every member"; pinned by
    `tests/api/test_collection_tenancy.py::test_second_org_member_sees_and_uses_the_orgs_collection`).
    Chat scope resolves a colleague's files through an org collection (`services/chat/context_resolver.py:120-161`,
    pinned by `test_chat_scope_resolves_a_colleagues_org_collection`).
  - **Does not:** `get_file_by_uuid_with_permission` (`app/utils/uuid_helpers.py:262-357`) asks
    `PermissionService.get_file_permission` (`permission_service.py:176-245`), which knows owner and
    `CollectionShare` only. So the member who listed the file gets **403** opening it. Gallery
    `shared`/`all` (`api/endpoints/files/__init__.py:310-345`) and search/chat retrieval
    (`accessible_user_ids` = `get_users_with_file_access`, `permission_service.py:438-476`, written by
    `tasks/search_indexing_task.py:101`) also exclude it. So a chat turn scoped to an org collection
    resolves the files and then retrieves **zero** excerpts from colleagues' files. That is exactly
    the "unexplained empty answer" the resolver's own docstring warns about.
- Collection-naming surfaces (each one can leak a restricted collection): list `mine|shared|all`
  (`media_collections.py:448`), `/shared-with-me` (`:300`), `/for-files` (`:396`), detail (`:707`),
  media list (`:1003`), shares (`:1169+`), file detail's collection list (`files/crud.py:get_file_collections`, ~193),
  upload-to-collection (`files/prepare_upload.py:~128-152`, `url_processing.py:82`, `tasks/youtube_processing.py:~300`),
  auto-label fuzzy match + auto-apply (`services/auto_label_service.py:92-130, 158, 326, 408`),
  topic suggestions (`services/topic_extraction_service.py`), chat scope (above), search's **raw
  integer** `collection_id` filter (`api/endpoints/search.py:159` → `hybrid_search_service.py:1673`
  `term collection_ids`), Redis list cache (`redis_cache_service.invalidate_collections`, `:252`),
  share WS events (`media_collections.py:_notify_share_event`, `send_ws_event` at `:172`, `:1561`).
- The prompt "used by" list (`api/endpoints/prompts.py:~197-214`) filters `Collection.user_id ==
  current_user.id` (creator only), so it cannot leak a restricted collection. No change.

### 1.2 Holds (#1276)

- Platform-only today. `takedown_service.quarantine_file(legal_hold=True)` sets
  `media_file.legal_hold` and best-effort S3 object lock (`takedown_service.py:304-343`).
  `release_file(clear_legal_hold=...)` clears both (`:488-506`) and calls
  `erasure_reconciliation.notify_hold_released` (`:571`). Org takedown hard-codes `legal_hold=False`
  (`api/endpoints/org_admin_takedown.py:260`). **Issue premise correct.**
- `takedown_authority.current_origin` (`:79-91`) treats `legal_hold=True` with no origin as
  **PLATFORM**. So **reusing the `legal_hold` column for org holds would lock the org out of its own
  file**: org quarantine would get 409, org release 403. A platform `release_file` would also
  silently clear an org hold. Org holds must not write that column.
- Hold guards that exist now (each reads `media_file.legal_hold`):
  - **Chokepoint** `file_cleanup_service.purge_media_file` (`:1187`, returns `refused_legal_hold`).
    Callers: `files/crud.py:1230` (409 `FILE_UNDER_LEGAL_HOLD`), `files/cancel_upload.py:105`,
    `file_cleanup_service.py:270` (orphan cleanup), `gdpr_erasure_service.py:308`,
    `scripts/corpus_injection/purge.py:103`.
  - `gdpr_erasure_service._purge_files` (`:299`, skip + ledger `deferred`).
  - Retention sweep query (`tasks/cleanup.py:480`).
  - Account delete `admin._assert_no_files_under_legal_hold` (`admin.py:491-550`, called at `:608`,
    `:1024`, `users.py:856`). It must run first because `admin.py:679` is a bulk
    `query(MediaFile).delete()` that bypasses the chokepoint.
  - Duration-repair writer (`tasks/recovery_tasks.py:303`). This is a mutation, not a delete.
  - Retention-clock stamp (`takedown_service.stamp_completion_time`, `:123-141`).
- **There is no trash yet.** #1101 is open, so "trash purge" means `hold_guard` must be the API that
  #1101 calls. The only other `db.delete(MediaFile)` sites are failed-upload rollbacks
  (`files/upload.py:761/767`, `complete_upload.py`, `prepare_upload.py:318`, `url_processing.py:609`)
  on rows that cannot be held yet.

### 1.3 Audit (#1277)

- Write path `auth/audit.py:AuditLogger.log` (~`:370`): stdout plus monthly OpenSearch index
  `audit-logs-YYYY.MM`, `replicas 0`, best-effort, JSONL fallback, no-op when `AUDIT_LOG_ENABLED` is
  false. Explicit mapping in `_ensure_index` (~`:300`). `details` is a dynamic object.
- Org read `GET /org-admin/audit-logs` (`api/endpoints/org_admin.py:56-117`) through
  `query_audit_logs` and `build_org_scope_clause` (`auth/audit.py`). The router is gated by
  capability `organizations` (`api/router.py:183`). **No UI** (the frontend has the super-admin
  `components/settings/AuditLogViewer.svelte`, 622 lines). Export exists only for super_admin
  (`admin.py:2536`), and it duplicates the query building instead of calling `query_audit_logs`.
- Already audited: `RESOURCE_SHARE/UNSHARE` (`media_collections.py:1381, 1464, 1542`;
  `tags/sharing.py`) and `transcript.view_unredacted` (`files/crud.py:audit_unredacted_reveal`
  ~617). The latter passes a **raw string** event type with `# type: ignore` instead of an enum
  member. Not audited: view, stream-URL issue, download, transcript/subtitle/summary/chat export,
  collection membership, visibility.
- **F5, a real leak today:** `build_org_scope_clause`'s legacy branch admits any event with **no**
  `organization_id` whose actor is an org member. A personal-scope event (for example
  `transcript.view_unredacted` on a member's personal file, which stamps `organization_id=None`) is
  therefore readable by every admin of every org the member belongs to. Adding personal-scope
  view/export events would multiply this.

### 1.4 Platform act-as (#1281)

#1122 is **implemented**: `services/platform_bypass.py` (`PlatformBypass.allows`, `record_use`
fail-closed 503), `api/deps_context.py` (`get_base_context`, `resolve_request_bypass`,
`refuse_under_support_grant`), `services/support_access_*`. Structural tests exist:
- `tests/unit/test_platform_bypass_discipline.py`: AST allowlist of `.is_admin` / `is_admin=` sites,
  classes N/P/Q.
- `test_platform_bypass_route_coverage.py`: routes that skip the context.
- `tests/api/test_support_access_enforcement.py`: pending, expired, revoked, demoted, cross-org and
  read-vs-write, on **one** file route.

**Gaps:**
- (a) Routes gated by `get_current_admin_user` / `get_current_super_admin_user` /
  `get_current_active_superuser` (53 in `admin.py` alone, about 180 repo-wide) are invisible to the
  AST scan because they never read `.is_admin`.
- (b) The no/expired/revoked/valid quartet is proven for `media_file` only, not per chokepoint
  (collection, speaker, profile, comment, watch source, task, thumbnail).
- (c) `require_capability(platform_admin_bypass=True)` (`core/capabilities.py:297-330`) lets
  `is_superuser` reach capability-hidden routers. It is a surface bypass, and nothing pins that the
  content check still applies behind it.
- (d) No committed path → guard → test matrix.

No impersonation or "login as" path exists (`rg impersonat|act_as` finds none). Celery tasks do not
re-authorize; the HTTP chokepoint is the gate (1122 §3.5).

---

## 2. Design decisions

**D-1. Collection visibility mode.**
- Add `collection.visibility` `organization` (default) | `restricted`. CHECK: `restricted` only when
  `organization_id IS NOT NULL`.
- A **restricted** collection is visible to: its creator (`user_id`); members whose org role is
  `org:admin`; and `CollectionShare` targets (users, and group members who are also org members).
- Permission inside a restricted collection: creator and org admin = `owner`, share target = its
  share level. The org-wide implicit `editor` does not apply.
- Rejected: a separate "allowed members" table (it duplicates `CollectionShare`); per-file ACLs (out
  of scope).

**D-2. File rule when a file is reachable by more than one route.** A file's readability is the
**union** of routes: owner, explicit share, and (under D-3) a visible collection. Restriction
removes the restricted collection as a route and **hides the membership fact**. It never revokes
another route. Consequences:
- The file detail's collection list and `/for-files` omit restricted collections the caller cannot
  see.
- Moving a file into a restricted collection does not hide it from its owner.
- Org admins see everything in their org.

**D-3. Fix F1: org-collection visibility is a real file-read route (recommended; owner decision Q1).**
The v422 model docstring and two pinned tests say members see and use the org's files through the
collection. Make the file chokepoint, gallery `shared`/`all`, search and chat retrieval agree.
- **Postgres:** `get_file_permission` / `get_accessible_file_ids_subquery` gain one arm:
  "member of a collection visible to me". The arm comes from the shared predicate.
- **OpenSearch:** a **query-time** arm, not an ACL fan-out. `_build_filters` becomes
  `bool.should[ terms accessible_user_ids:[me], terms collection_ids:<visible collection ids> ]`,
  computed per request in Postgres (`visible_collection_ids`, capped at
  `MAX_VISIBLE_COLLECTION_TERMS` = 10 000; above that, fall back to the current
  `accessible_user_ids` only and log WARNING).
- This avoids re-indexing every file on every org join/leave or visibility switch.
  `collection_ids` already exists on chunk docs (`indexing_service.py:282`, populated at
  `search_indexing_task.py:84`).
- `update_file_access_index` must refresh `collection_ids` on add/remove media. Today it is only
  dispatched when the collection has shares (`media_collections.py:~935`); make it unconditional
  for org collections.
- **Alternative** (if Q1 = narrow): restrict `/collections/{uuid}/media` and chat collection scope
  to files readable by owner/share, and rewrite the two v422 tests. Smaller, but members of an org
  collection would see only their own files in it.

**D-4. Restricted collections under a support-access grant are hidden (Q2).** Need-to-know is the
point. The visibility predicate runs **before** `bypass.allows` for collections, and an org grant
(`org_role=None`) is not a named member. Platform staff reach a restricted collection only if
explicitly shared, which they cannot be (shares are org-member-only).

**D-5. Org holds are separate rows; one hold check.**
- New `content_hold` plus `content_hold_file` tables (section 3.3).
- "Deletion-blocked" = `media_file.legal_hold` (platform takedown hold, unchanged) **OR** an active
  `content_hold_file` row.
- `hold_guard` is the only module that answers it, in Python and in SQL. Rejected: one shared
  boolean, because it breaks `current_origin` and lets either authority clear the other's hold
  (§1.2).
- Each authority releases only its own holds. An org admin cannot lift a platform hold. A platform
  admin cannot lift an org hold through the product (Q4). The S3 object lock is cleared only when
  **no** hold of either kind remains.

**D-6. A collection hold snapshots current members and extends to files added later** while it is
active (Q3). Rows are materialized in `content_hold_file` at placement and on every add while
active. While a collection hold is active:
- removing a held file from that collection is refused (409 `COLLECTION_UNDER_LEGAL_HOLD`);
- deleting the collection is refused;
- switching visibility is allowed (visibility is not destruction).

Release keeps the rows, with `released_at` set on the hold, for history.

**D-7. What a hold blocks.** Every irreversible destruction:
- delete and bulk delete, through `purge_media_file`;
- account delete, retention, GDPR user/member/org erasure, orphan cleanup;
- future trash purge (#1101);
- **reprocess / re-transcribe / re-diarize**, which replace transcript and speakers (`files/reprocess.py`).

Transcript **edits stay allowed** and are audited as membership/edit events (Q5). The content
stays visible and readable. Quarantine remains the only "hidden" state.

**D-8. One audit schema with typed top-level fields.** Following the #443 reasoning in
`AuditLogger.log`'s docstring, add top-level `resource_type` (keyword), `resource_uuid` (keyword)
and `tenant_scope` (`org` | `personal`, keyword).
- Every content and governance event sets all three plus `organization_id` (the resource's org).
- `build_org_scope_clause`'s legacy branch adds `must_not exists tenant_scope`, which **fixes F5**:
  a personal-scope resource event is never attributed to an org through its actor.
- Never in an event: transcript text, filenames, titles, collection names, hold reasons.

**D-9. Gating.** View, media-access and export events (high volume, the issue's "opt-in") record
only when **both** of these are true:
- capability `audit.content_access` (new, `AUDIENCE_PLATFORM`, community default `True`; a derived
  deployment's resolver may withhold it);
- `SystemSettings` `audit.content_access.enabled` (default `false`, super_admin toggle).

So a deployment opts in through the admin UI with no env var. Governance events (share,
visibility, membership, hold place/release/refusal) are **always** recorded: they are low volume,
and #1275/#1276 require them. The org-admin screen is behind new capability `org.audit_logs`
(`AUDIENCE_ORG_ADMIN`, community `False`, server-enforced on the new export route, no
platform-admin bypass, same shape as `org.takedown` in `api/router.py:192`). With content auditing
off, the screen still shows governance and auth events but no content-event filter.

**D-10. De-dup.** `content.file.view` and `content.media.access` (`kind=stream_url`) are written at
most once per `(event_type, actor, resource_uuid, surface)` per `CONTENT_AUDIT_DEDUP_SECONDS` (900).
- Implementation: Redis `SET NX EX` on `audit:dedup:<sha256 of the key>`.
- **Redis unavailable means write the event** (fail toward more audit, never less).
- Downloads and exports are never de-duped: each one is a distinct exfiltration.
- Thumbnails, captions, waveform and segment pagination are not audited; the detail view covers
  them.

**D-11. #1281 is proved by a machine-checked matrix, not prose.** Commit
`docs/security/platform-access-matrix.tsv` (columns `kind, key, class, guard, test`) and
`docs/security/platform-access-matrix.md` (explains the classes).

| class | meaning |
|---|---|
| `G` | grant-required content path (`PlatformBypass.allows` site) |
| `M` | platform metadata route, audited by `audit_metadata_access` |
| `P` | platform operation, no tenant content |
| `Q` | quarantine-review visibility |
| `S` | capability surface bypass, content still gated behind it |
| `L` | grant lifecycle |

A test enumerates (i) every route whose live dependency tree contains an admin/superuser
dependency and (ii) every `.allows(` call site by AST. It fails on any unlisted item or stale row.

---

## 3. Exact changes

### 3.1 Shared visibility predicate (WP-1)

New `app/services/collection_visibility.py` (≤ 300 lines):

```python
def visible_collection_pred(user_id: int, org_id: int | None, org_role: str | None) -> ColumnElement[bool]:
    """Collections of the active tenant this caller may see. Personal scope: own + shared-in.
    Org scope: visibility='organization', or creator, or org_role=='org:admin', or a
    CollectionShare to the user / one of their groups."""
def visible_collection_ids(db, ctx: RequestContext) -> set[int]          # request-cached on ctx
def collection_permission(db, collection, user_id, org_role) -> str | None  # replaces org_collection_permission's use
def readable_file_ids_via_collections(user_id, org_id, org_role) -> Select  # D-3 arm
```

Route these through it:
- `PermissionService.get_collection_permission` and `get_accessible_collection_ids` (keep the
  signatures; delegate).
- `get_file_permission` and `get_accessible_file_ids_subquery` (D-3 arm).
- `uuid_helpers.get_collection_by_uuid_with_permission`: a restricted collection the caller cannot
  see answers **404** (same tenant, so a 403 would be an existence oracle).
- `media_collections.py` list / shared-with-me / for-files / detail / media.
- `files/crud.get_file_collections`.
- `chat/context_resolver._resolve_collections`.
- `auto_label_service._get_user_collections_cached`: only visible collections the caller can edit.
  An LLM suggestion that matches a hidden name **creates nothing and adds nothing**, and is
  reported as a skipped suggestion.
- `topic_extraction_service` suggestions.
- `prepare_upload` / `url_processing` / `youtube_processing` targets (editor on a visible
  collection, else 404).
- `search.py`: the integer `collection_id` must be in `visible_collection_ids`, else an empty
  result with 200. The filter must not become a membership oracle.
- `_build_filters` (D-3).

A scan test `tests/unit/test_collection_visibility_discipline.py` AST-scans `app/` for
`query(Collection` / `select(Collection` outside an allowlist (with reasons): `gdpr_erasure_service`,
`admin.py` user delete, migrations and the predicate module itself. Pattern:
`test_platform_bypass_discipline.py`.

### 3.2 Restricted mode (WP-2)

- **Migration** `v<next>_add_collection_visibility`. Derive the number with
  `python3 scripts/release-tests/lib/alembic-head.py backend` immediately before committing (head
  today is v444; other v0.6.0 lanes are adding revisions).
  - Up: `ALTER TABLE collection ADD COLUMN IF NOT EXISTS visibility VARCHAR(20) NOT NULL DEFAULT 'organization'`.
  - Guarded `ck_collection_visibility` = `visibility IN ('organization','restricted') AND (visibility='organization' OR organization_id IS NOT NULL)`.
  - Index `ix_collection_org_visibility (organization_id, visibility)`.
  - Down: drop all three.
  - Follow the 5-step procedure in `app/db/CLAUDE.md`, including a detection arm at the top of
    `_detect_schema_version` (`app/db/migrations.py:105`) keyed on `ck_collection_visibility`, and
    `tests/unit/test_v<N>_migration_consistency.py` using `assert_detected_at_or_after`.
- Model `Collection.visibility`. Schemas `CollectionSchema` / `CollectionWithCount` /
  `CollectionResponse` gain `visibility: Literal["organization","restricted"]` and
  `can_change_visibility: bool` (server-computed). Create/update accept `visibility`; on a personal
  collection anything but `organization` is a 422.
- **`PATCH /collections/{uuid}/visibility`** body `{"visibility": "restricted" | "organization"}` →
  200 `CollectionResponse`.
  - Authority: creator or `org:admin` of the collection's org. Otherwise 404 if the caller cannot
    see the collection, 403 if they can.
  - Refused with `refuse_under_support_grant`.
  - Effects, in one transaction: update the row; invalidate `redis_cache_service.invalidate_collections`
    for **every** org member (one `SCAN`-free pattern: bump a per-org cache epoch key that is folded
    into the list cache key); dispatch `update_file_access_index` for member files (only the
    `collection_ids` freshness matters under D-3).
  - Audit `collection.visibility.change` (always on).
  - WS `collection_access_changed` `{collection_uuid}` to members who just lost visibility, so
    their UI drops it. They already knew the uuid, so the payload discloses nothing. Allowlist it in
    `tests/unit/test_ws_event_quarantine_discipline.py` (it names no MediaFile).
- **Name conflicts:** keep `uq_collection_org_name`. The 409 for a hidden collection reads "A
  collection with this name already exists in this organization", **with no uuid**. Residual
  oracle: name existence only (Q6).

### 3.3 Hold model and guard (WP-6)

**Migration** `v<next>_add_content_hold` (additive; down drops both tables and says in its
docstring that it destroys hold history, like `v389`).

| `content_hold` | type | notes |
|---|---|---|
| `id` | int PK | |
| `uuid` | UUID uuid7 unique | wire id |
| `organization_id` | FK organization **CASCADE**, NOT NULL | org holds only. Platform holds stay on `media_file.legal_hold` |
| `scope` | String(20) | CHECK `IN ('file','collection')` |
| `collection_id` | FK collection **SET NULL**, NULL | CHECK: set iff `scope='collection'` at insert (enforced in the service; SET NULL keeps history) |
| `reason` | Text NOT NULL | CHECK `char_length BETWEEN 10 AND 2000`. Org admins only; never in audit or WS |
| `matter_ref` | String(255) NULL | |
| `placed_by_user_id`, `released_by_user_id` | FK user SET NULL | v387 actor convention; add to `test_user_deletion_fk_coverage.py` |
| `placed_at` NOT NULL, `released_at` NULL | timestamptz | active = `released_at IS NULL` |
| `release_reason` | Text NULL | |

`content_hold_file(hold_id FK content_hold CASCADE, media_file_id FK media_file CASCADE, added_at,
PK(hold_id, media_file_id))`, plus index `(media_file_id)`.

`erase_organization` must not delete the org row while any active hold exists. It already defers
held files; add an explicit assert and a test.

New `app/services/hold_guard.py` (the **only** answer):

```python
@dataclass(frozen=True)
class HoldState: held: bool; platform: bool; org_hold_uuids: tuple[str, ...]
def hold_state(db, file_id: int) -> HoldState
def held_pred() -> ColumnElement[bool]        # MediaFile.legal_hold OR EXISTS(active content_hold_file)
def not_held_pred() -> ColumnElement[bool]
def assert_not_held(db, file_ids, *, actor_org_admin: bool) -> None   # raises HoldRefusal
def refusal_detail(state, *, reveal_uuids: bool) -> dict  # {"error":"FILE_UNDER_LEGAL_HOLD","held_by":["platform"|"organization"],"holds":[uuid...] if org admin}
```

**Replace every guard** (§1.2):
- `purge_media_file` `:1187`: `hold_state(...).held`, keep the return shape.
- `gdpr_erasure_service._purge_files` `:299`.
- `tasks/cleanup.py:480`: `not_held_pred()`.
- `admin._assert_no_files_under_legal_hold`: rename to `_assert_no_held_files`, use `held_pred()`,
  update the three callers.
- `recovery_tasks.py:303`.
- `stamp_completion_time`.
- `files/reprocess.py` entry points (D-7): 409 `FILE_UNDER_LEGAL_HOLD`.
- Collection delete and remove-media in `media_collections.py` (D-6).

Add scan test `tests/unit/test_hold_guard_single_source.py`: no `MediaFile.legal_hold` /
`.legal_hold` read outside `hold_guard.py`, `takedown_*`, schemas and the org takedown list.

**Platform side, kept correct:** in `takedown_service.release_file`, clear the S3 object lock only
when `not hold_state(...).held` after the column update. `quarantine_file` is unchanged.

Service `app/services/content_hold_service.py`: `place_file_hold`, `place_collection_hold`,
`release_hold`, `on_collection_media_added` (materialize rows), `list_holds`.
- Writes never commit inside helpers. The endpoint commits once.
- S3 lock best-effort on place, and on release only if no hold remains.
- On release, call `erasure_reconciliation.notify_hold_released(file)` for each file that is no
  longer held, the same hook platform release uses (`takedown_service.py:571`).
- Audit `content.hold.place` / `content.hold.release`. Refusals emit `content.hold.refused` from
  `assert_not_held` / `purge_media_file`.

### 3.4 Content-audit write path (WP-3)

- `auth/audit.py`: add enum members.

  | member | value |
  |---|---|
  | `CONTENT_FILE_VIEW` | `content.file.view` |
  | `CONTENT_MEDIA_ACCESS` | `content.media.access` (`details.kind` = `stream_url` \| `download_prepare` \| `download_stream`) |
  | `CONTENT_EXPORT` | `content.export` (`details.format`, `details.surface` = `transcript` \| `subtitles` \| `summary` \| `bulk` \| `chat`, `details.file_count`) |
  | `COLLECTION_VISIBILITY_CHANGE` | |
  | `COLLECTION_MEMBERSHIP_CHANGE` | `details.added`/`removed` counts + ≤100 file uuids |
  | `CONTENT_HOLD_PLACE` / `CONTENT_HOLD_RELEASE` / `CONTENT_HOLD_REFUSED` | |
  | `AUDIT_EXPORT` | |
  | `TRANSCRIPT_VIEW_UNREDACTED` | `transcript.view_unredacted`, replacing the raw string and its `type: ignore` |

  - `log()` gains keyword args `resource_type`, `resource_uuid`, `tenant_scope` (omitted when None,
    like `target_user_id`).
  - `_ensure_index` adds the three keyword mappings. For an index that already exists it issues
    `indices.put_mapping` for the three fields once per process. If dynamic mapping already typed
    them, log a WARNING; the next month's index is correct.
  - `build_org_scope_clause` adds `must_not exists tenant_scope` to the legacy branch (F5).
  - `query_audit_logs` gains `resource_type` / `resource_uuid` filters.
- New `app/services/content_audit.py`:

  ```python
  def content_audit_enabled(db, request) -> bool     # capability AND SystemSettings, settings_cache 30 s
  def record_content_event(db, request, *, event: AuditEventType, actor: User, file: MediaFile | None = None,
                           collection: Collection | None = None, details: dict, dedup: bool) -> bool
  ```

  It stamps `organization_id` from the **resource**, `tenant_scope`, `target_user_id` = resource
  owner when it differs from the actor, `details.grant_uuid` when `ctx.bypass.under_grant`, and
  source IP/UA. It never raises. View-class events check `content_audit_enabled` first.
- Constants (`core/constants.py`): `CONTENT_AUDIT_DEDUP_SECONDS = 900`,
  `MAX_VISIBLE_COLLECTION_TERMS = 10000`. Add a `SystemSettings` default row
  `audit.content_access.enabled=false`.
- `core/capabilities.py`:
  - `audit.content_access` (platform, True). Server-enforced in the sense that the writer reads it.
    Update the docstring tier list.
  - `org.audit_logs` (org_admin, False).
  - `org.legal_hold` (org_admin, False).
  - Update `CAPABILITY_AUDIENCE` and the frontend `cap:` contract
    (`tests/unit/test_capability_contract.py`).
- Super-admin toggle: extend the existing audit settings surface with `GET/PUT
  /admin/audit/content-access` (super_admin, audited `ADMIN_SETTINGS_CHANGE`).

**Storage estimate** (put it in the admin help text): ~0.8 KB/event. With de-dup, about 30 view +
10 media + 2 export events per active user-day ≈ 34 KB/user/day, so ~1 GB/month per 1,000 active
users at replicas 0. Monthly indices. Retention follows the audit retention policy (1122 D5); no
new policy here.

### 3.5 Emit sites (WP-4)

| Event | Site |
|---|---|
| `content.file.view` | `files/crud.py` detail GET (`MediaFileDetail` route, ~912) |
| `content.media.access stream_url` | `files/__init__.py:618` |
| `content.media.access download_*` | `files/__init__.py:920`, `:979` |
| `content.export` | `files/transcript_export.py:94`, `files/subtitles.py:94` and `:291` (prepare, `file_count`), `summarization.py:254`, `chat/export.py:201` (`resource_type=conversation`; `organization_id` from the conversation) |
| `COLLECTION_MEMBERSHIP_CHANGE` | `media_collections.py:870`, `:951`, `auto_label_service._add_file_to_collection` (actor = the file owner, `details.source=auto_label`) |
| `RESOURCE_SHARE/UNSHARE` | existing sites gain `resource_type/uuid/tenant_scope` |

The async bulk-export worker emits nothing: the event is the request.

### 3.6 Org-admin audit API (WP-5)

- `GET /org-admin/audit-logs` (existing) gains `resource_type`, `resource_uuid`, `user_uuid`,
  `target_user_uuid`. UUIDs resolve to ids with the existing membership 403. Keep the int params.
- Response items gain server-computed `actor_display` and `target_display` (name, or email only
  while still a member) and `resource_display` (title of an org file resolved in Postgres with
  `organization_id == ctx.org_id`; null otherwise or when deleted). `user_agent` stays.
- **New `GET /org-admin/audit-logs/export?format=csv|json`**: same filters, ≤ 10 000 rows,
  `StreamingResponse`. The router gets `require_capability("org.audit_logs",
  platform_admin_bypass=False)` on this route. Audit `AUDIT_EXPORT`.
- Refactor `admin.py:2536` to call `query_audit_logs` plus one shared CSV writer
  (`auth/audit_export.py`), so the two exports cannot drift.

### 3.7 Org-admin hold API (WP-7)

Router `api/endpoints/org_admin_holds.py`, mounted `/org-admin/holds`, capability `org.legal_hold`,
`platform_admin_bypass=False`, every route `require_org_admin`. `/org-admin/*` is already refused
under grants via `org_role=None`.

| Method + path | Body → response |
|---|---|
| `POST /org-admin/holds` | `{"scope":"file"\|"collection","file_uuid"?,"collection_uuid"?,"reason":"10..2000","matter_ref"?}` → 201 `HoldOut` |
| `GET /org-admin/holds?status=active\|released\|all&limit&offset` | → `{"items":[HoldOut],"total":n}` |
| `GET /org-admin/holds/{uuid}` | → `HoldOut` (+ `file_count`) |
| `POST /org-admin/holds/{uuid}/release` | `{"reason":"10..2000"}` → 200 `HoldOut`. 409 `hold_already_released` |

`HoldOut = {uuid, scope, status, file: {uuid,title}|null, collection: {uuid,name}|null, file_count,
reason, matter_ref, placed_by: UserRef|null, placed_at, released_by, released_at, release_reason}`.

Targets must belong to `ctx.org_id`; a cross-org uuid gets 404 (no oracle). Holding a restricted
collection is allowed (org admins see all). Rate-limit like `org_takedown` (shared bucket).

File/collection read schemas gain `hold: {held: bool, held_by: ["platform"|"organization"]}`. They
**never** carry a reason, and hold uuids only for org admins. Delete/reprocess refusals use
`hold_guard.refusal_detail`.

---

## 4. Security and tenant-isolation analysis

| Surface | Could leak / be abused | Guard | Negative test (must fail first) |
|---|---|---|---|
| Collection list mine/shared/all, shared-with-me | restricted name/count | predicate | `test_restricted_collection_absent_from_every_list_for_unnamed_member` |
| Detail / media / shares | existence, files | predicate, 404 | `test_restricted_detail_media_shares_404_for_unnamed_member` |
| File detail collection list, `/for-files` | membership fact | predicate | `test_file_owner_does_not_see_restricted_collection_holding_their_file` |
| Search `collection_id` | membership oracle via own files | visible-id check | `test_search_with_hidden_collection_id_returns_nothing` |
| Search/chat retrieval (D-3 arm) | hidden collection's files via `collection_ids` term | visible ids only | `test_search_does_not_return_files_reachable_only_via_restricted_collection` |
| Gallery shared/all, counts, facets, tag counts | files via collection arm | arm uses the predicate | `test_gallery_counts_and_tag_facets_exclude_restricted_only_files` |
| Chat scope by collection | files into the LLM | predicate | `test_chat_scope_refuses_restricted_collection_for_unnamed_member` |
| Auto-label / topic suggestions | name echo, auto-add into hidden collection | predicate + editor | `test_auto_label_never_matches_or_adds_to_hidden_collection` |
| Upload/URL/YouTube target | probing by uuid | predicate, 404 | `test_upload_into_hidden_collection_is_404` |
| Bulk export / exports | files via collection | exports authorize per file through the chokepoint | covered by the D-3 chokepoint test |
| Redis list cache | stale visibility after switch | org epoch bump | `test_switch_to_restricted_takes_effect_on_next_list_without_ttl_wait` |
| WS / notifications | share events to a former member | existing org-member filter, plus `collection_access_changed` carries the uuid only | `test_visibility_ws_payload_has_no_name` |
| Support grant | staff into need-to-know | D-4 | `test_org_grant_cannot_see_restricted_collection` |
| Org audit read | personal events via legacy branch (F5) | `tenant_scope` | `test_member_personal_file_view_invisible_to_org_admin` |
| Org audit read / export | other org's events, filter probing | scope clause, membership 403, capability | `test_org_admin_export_contains_only_own_org_events`, `test_export_404_when_org_audit_logs_cap_off` |
| Audit payload | content in index | schema rule | `test_content_events_never_carry_filename_title_or_text` |
| Hold reason | leaks to members/staff | reason only in `HoldOut` | `test_member_file_detail_shows_held_without_reason` |
| Hold release | wrong authority clears hold | per-authority release; S3 recount | `test_platform_release_keeps_org_hold_and_s3_lock`, `test_org_cannot_release_platform_hold` |
| Hold on another org's file | cross-tenant hold DoS | org check, 404 | `test_hold_on_other_org_file_is_404` |

Grants never bypass a hold: the check is inside `purge_media_file`, below authorization. Pinned by
`test_write_grant_cannot_delete_held_file`.

---

## 5. Frontend

- **Restricted collections (WP-8):**
  - `components/collections/CollectionFormModal.svelte`: a visibility radio, org collections only,
    shown when `can_change_visibility`.
  - `CollectionsList.svelte`: a lock badge for `restricted`.
  - `components/sharing/ShareCollectionModal.svelte`: explainer copy ("Only people named here, the
    creator and organization admins can see this collection").
  - API client `lib/api/collections`: add `setVisibility`.
  - Handle the `collection_access_changed` WS event in the collections store (drop the row, toast).
- **Hold UI (WP-7):**
  - Lazy settings section `org-legal-holds` (`lib/settings/sectionLoaders.ts`, gated with
    `orgAdminCapOn(capState, 'org.legal_hold')` in `SettingsModal.svelte` next to `org-takedown`
    at ~417).
  - New `components/settings/OrgLegalHoldsPanel.svelte` (list, place, release modals).
  - A "Held" badge on the file page header and collection header from `hold.held` (both in route
    chunks, not root).
  - The delete and reprocess dialogs show the refusal message from `held_by`.
- **Org audit screen (WP-5):** first extract `components/settings/audit/AuditLogFilters.svelte` and
  `AuditLogTable.svelte` from `AuditLogViewer.svelte` (it stays super-admin and becomes thinner).
  - New `components/settings/OrgAuditLogPanel.svelte`, lazy section `org-audit-logs`, gated by
    `org.audit_logs`.
  - Filters: user picker (org members), action (event-type list; content types only when the
    capability map's `audit.content_access` is true), resource uuid (prefilled from a file page
    "Access history" link for org admins), date range. Offset pagination, export button.
  - Client `lib/api/orgAdmin.ts`: `listAuditLogs`, `exportAuditLogs`.
- **Bundle:** every new panel goes through `sectionLoaders` dynamic imports. Badges add <1 KB to
  route chunks. Nothing touches `$lib/cloud` or the root layout (16.7/22.5 KB), and the closure
  growth is the lazy chunks only. `scripts/check-bundle-budget.mjs` must pass unchanged.
- **i18n:** about 55 new keys (`collections.visibility.*`, `holds.*`, `orgAudit.*`, refusal
  messages) in all 12 locales with real translations; `npm run check:i18n` only proves parity.
  Event types display through `orgAudit.event.<event_type>` with a raw-string fallback. **RTL
  (ar):** the lock and held badges use logical CSS (`margin-inline-start`). Check the audit table's
  date and uuid columns with `dir="ltr"` spans.
- **Light/dark:** use existing badge tokens (warning for held, neutral for restricted). Verify both
  themes in a browser and screenshot both.

---

## 6. Test plan

Prove every must-fail-first test red on HEAD with the root `CLAUDE.md` `git archive HEAD` recipe
(`mktemp -d -t ot-redcheck.XXXXXX`), by copying only the new test files into that tree.

| Test | Red on HEAD because |
|---|---|
| `tests/api/test_collection_visibility.py` (the 12 surface tests in §4) | no `visibility` column / PATCH route; `test_collection_member_can_open_colleague_file_in_org_collection` (D-3) is red today: 403 |
| `tests/api/test_chat_org_collection_retrieval.py::test_colleague_file_in_org_collection_returns_excerpts` | F1: zero excerpts today |
| `tests/unit/test_audit_org_scope_personal.py::test_member_personal_file_view_invisible_to_org_admin` | F5: legacy branch admits it |
| `tests/api/test_content_audit_events.py`: per event type, **one** event per action with correct `organization_id`/`tenant_scope`/`resource_uuid`; second view within 900 s writes none; download twice writes two; flag off writes none; Redis down still writes | events do not exist |
| `tests/api/test_org_audit_ui_api.py`: export scoping, capability 404, uuid filters, display fields | route absent |
| `tests/api/test_org_legal_hold.py`: one test per path. Interactive delete, bulk action, cancel-upload, account delete, retention sweep, GDPR user / org-member / org erasure, orphan cleanup, reprocess, collection delete, remove-from-held-collection. Each asserts the file survives and the documented refusal; release restores, and fires `notify_hold_released` | no org holds |
| `tests/unit/test_hold_guard_single_source.py`, `test_collection_visibility_discipline.py` | scanners new; each ships a must-fire and a must-stay-clean self-test case |
| `tests/unit/test_platform_access_matrix.py` (WP-0): unlisted admin route fails; stale row fails; every `.allows(` site has a `G` row; self-test with a synthetic route | matrix absent |
| `tests/api/test_support_access_matrix.py`: parametrized over one representative route per `G` chokepoint (file, collection, speaker, profile, comment, watch source, task, thumbnail): no grant → refused; expired → `support_grant_expired`; revoked → `support_grant_revoked`; valid → 200 + `support_access_use` row | today only `media_file` is covered |
| `test_superuser_capability_bypass_still_needs_grant_for_content` (class S) | not pinned |

Migration consistency tests per revision. Frontend: vitest for the three panels and the
visibility control (`npm run test`, `test:audit`), `npm run check`, build. E2E (`backend/tests/e2e`,
marker `settings`): create a restricted collection, verify a second member cannot see it, place
and release a hold. Clean up everything created, per the dev-data rule.

---

## 7. Work packages

| WP | Issue | Size | Depends | Files (disjoint lanes) |
|---|---|---|---|---|
| **WP-0** Grant matrix + tests | #1281 | M | none | `docs/security/platform-access-matrix.{tsv,md}`, `tests/unit/test_platform_access_matrix.py`, `tests/api/test_support_access_matrix.py`. If a path without a grant is found: **stop, file a private security report, no public detail**, then fix |
| **WP-1** Visibility predicate + F1 fix (D-3) | #1275 | L | Q1 | `services/collection_visibility.py`, `permission_service.py`, `uuid_helpers.py`, `chat/context_resolver.py`, `hybrid_search_service._build_filters`, `search.py`, `auto_label_service.py`, `topic_extraction_service.py`, `files/crud.py:get_file_collections`, upload targets, discipline test |
| **WP-2** Restricted mode | #1275 | M | WP-1 | migration, `models/media.py`, schemas, `media_collections.py`, cache epoch, WS allowlist |
| **WP-3** Audit schema + writer + F5 | #1277 | S | none | `auth/audit.py`, `services/content_audit.py`, `core/constants.py`, `core/capabilities.py`, admin toggle route |
| **WP-4** Emit sites | #1277 | M | WP-3 | the files in §3.5 (touches `media_collections.py`: land after WP-2) |
| **WP-5** Org audit API + UI | #1277 | M | WP-3 (API), WP-4 (content filter) | `org_admin.py`, `auth/audit_export.py`, `admin.py` export refactor, frontend audit components + i18n |
| **WP-6** Hold tables + `hold_guard` + guard replacement | #1276 | M | none | migration, `models/content_hold.py`, `hold_guard.py`, `content_hold_service.py`, `file_cleanup_service.py`, `gdpr_erasure_service.py`, `tasks/cleanup.py`, `recovery_tasks.py`, `admin.py` (hold assert only), `takedown_service.py` (S3 recount), `files/reprocess.py` |
| **WP-7** Org hold API + UI | #1276 | M | WP-6, WP-3 | `org_admin_holds.py`, `router.py`, read schemas, `media_collections.py` (delete/remove guard: after WP-2), frontend holds panel + badges + i18n |
| **WP-8** Restricted-collection UI | #1275 | S | WP-2 | `components/collections/*`, `ShareCollectionModal.svelte`, i18n |

**Parallel lanes:** A = WP-0. B = WP-1 → WP-2 → WP-8. C = WP-3 → WP-4 → WP-5. D = WP-6 → WP-7.
- `media_collections.py` is shared by WP-2, WP-4 and WP-7: serialize those edits (B first).
- `admin.py` is shared by WP-5 (export) and WP-6 (hold assert), in disjoint functions.
- The two migrations take their numbers at merge time.
- Per root `CLAUDE.md`, each lane uses **its own worktree**; one writer commits per checkout.
- i18n edits to the 12 locale files collide across lanes, so land them per WP and rebase.

---

## 8. Rollout and compatibility for derived deployments

- **Community (no orgs):** no visible change. `visibility` defaults to `organization` and is
  meaningless on personal collections. `org.*` capabilities are off. Content auditing is off until
  a super_admin enables it. F5 and D-3 only affect org scope. The `content_hold` tables stay empty.
- **Deployments that replace the capability resolver** must return the three new keys
  (`audit.content_access`, `org.audit_logs`, `org.legal_hold`), because omitted keys fail closed
  (`get_capabilities`). Per-tenant resolution works for all three: every emit site is an HTTP
  request (no Celery task reads `audit.content_access` with `request=None`), so tier-based
  resolution is safe.
- **Deployments that replace `$lib/cloud`:** no seam export changes. New panels are core components
  behind capabilities.
- **Deployments that register hooks** reading collection or file visibility must call
  `collection_visibility` / `hold_guard` rather than re-deriving them.
- Bump `CLOUD_SEAM_VERSION` (`auth/constants.py`) by one in WP-1, because file-read semantics
  change under D-3. Note it in the CHANGELOG `Security` section.
- **Index:** no reindex required. `collection_ids` already exists. The audit index's new fields
  reach the current month by `put_mapping`.

## 9. Verification and exit criteria

1. All the §6 tests are green; each must-fail-first test was observed red on an `ot-redcheck.`
   tree.
2. `scripts/safe-precommit.sh run --all-files` plus `--hook-stage pre-push`.
   `python3 scripts/audit-tests.py backend/tests` has 0 open findings.
3. `./scripts/run-integration-tests.sh` passes on a stack started with `./opentr.sh start dev`.
4. Frontend `npm run check`, `npm run check:i18n`, build (budget gate) all pass.
5. Manual pass in a browser on the dev stack, light and dark, en and ar:
   - restricted create/switch, seen as a second member;
   - place a hold, try to delete, release;
   - the org audit screen with content auditing on and off.
6. `docs/security/platform-access-matrix.md` is committed. `docs/security/tenant-boundaries.md`
   gains rows for restricted collections, org holds and content audit. `docs/abuse-and-takedown.md`
   gains a "hold vs quarantine vs org hold" table. The admin docs page covers the content-audit
   toggle and storage estimate.
7. Each issue closes from its own PR with one `Closes #N` per issue.

## 10. Open questions for the owner (recommended default in bold)

1. **Q1, F1:** should seeing an org collection let a member open its files everywhere (search,
   chat, file page)? **Yes (D-3), with the query-time `collection_ids` arm.** The alternative is to
   narrow the collection media list to files readable by owner or share.
2. **Q2:** should support-access grants reveal restricted collections? **No.**
3. **Q3:** does a collection hold cover files added later, and is removing a held file from that
   collection refused? **Yes to both.**
4. **Q4:** may platform staff release an org hold? **No, not through the product.** The platform
   uses its own takedown hold if it needs one.
5. **Q5:** what does a hold block beyond delete? **Reprocess too. Transcript edits stay allowed and
   are audited.**
6. **Q6:** should restricted collection names stay unique per org? **Keep uniqueness and accept the
   name-exists oracle** (generic 409, no uuid).
