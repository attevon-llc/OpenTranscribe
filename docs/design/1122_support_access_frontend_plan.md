# #1122: Support-access grants. Frontend plan

Status: **PLAN ONLY.** Written 2026-10-09 against `origin/feat/v0.6.0-frontend-ux`. Companion to
`docs/design/1122_platform_admin_tenancy_bypass_plan.md` (the backend design, "backend plan"
below; its §5.5 routes, §4 rules and §8 step 6 are what this implements). Start **after** backend
Phase 3 has merged **and** the amendments in §2 below have landed. Without them several screens
can't be built honestly.

Read first: root `CLAUDE.md`, `frontend/CLAUDE.md`, and the nested `CLAUDE.md` of
`src/components`, `components/settings`, `components/ui`, `lib/api`, `stores`, `routes`.
Codebase facts this plan relies on (verified 2026-10-09):

- All 182 `.svelte` components use **legacy syntax** (`export let`, `$:`). There are 0 rune
  components. Follow that; do not introduce `$state`/`$props` in this feature.
- Locale files are **flat dotted keys** (`"settings.quarantine.navLabel": "Quarantine"`), with
  `{{var}}` interpolation. `npm run check:i18n` checks key parity only.
- `SettingsModal.svelte` owns `SECTION_MIN_ROLE` and the sidebar. A section the *user* lacks
  privilege for renders locked. A surface the *deployment* lacks is omitted.
- `stores/capabilities.ts` is **fail-open** (an unknown key reads as enabled). This feature
  must be **fail-closed** (§4.4).
- Organizations exist only where an external IdP stamps an org claim (cloud). Community core
  never creates `Organization` rows, and `/org-admin/*` is mounted under
  `capability="organizations"` (404 in community). So in community, MULTI mode happens only
  when `TENANCY_MODE=multi` is forced, and then only **personal-workspace** grants are possible.

## 1. API contract consumed

Mirror the backend Pydantic schemas exactly (`lib/api/CLAUDE.md`). These types assume
amendments A2-A5.

```ts
// src/lib/api/supportAccess.ts
export type AccessLevel = 'read' | 'write';
export type GrantMode = 'approved' | 'break_glass';
export type GrantStatus = 'pending' | 'active' | 'denied' | 'expired' | 'revoked' | 'lapsed';
export interface UserRef { uuid: string; full_name: string | null; email: string }
export interface OrgRef { uuid: string; name: string; slug: string | null }
export interface SupportGrant {
  uuid: string;
  status: GrantStatus;                  // server-computed (A4); never derived client-side
  grant_mode: GrantMode;
  access_level: AccessLevel;
  organization: OrgRef | null;          // exactly one of organization / subject_user
  subject_user: UserRef | null;
  grantee: UserRef | null;              // null = grantee account deleted (FK SET NULL)
  reason: string;
  ticket_ref: string | null;
  requested_duration_minutes: number;   // A3
  requested_at: string;                 // ISO-8601 UTC
  pending_expires_at: string | null;    // requested_at + 72 h while pending
  decided_by: UserRef | null;
  decided_at: string | null;
  starts_at: string | null;
  expires_at: string | null;
  revoked_by: UserRef | null;
  revoked_at: string | null;
}
export interface GrantPage { items: SupportGrant[]; total: number; server_time: string } // A5
export interface CreateGrantBody {
  organization_uuid: string | null; subject_user_uuid: string | null; // exactly one
  access_level: AccessLevel; reason: string; duration_minutes: number; // 15..480
}
export interface BreakGlassBody extends CreateGrantBody { ticket_ref: string } // 15..240, default 60
export interface ApproveBody { duration_minutes?: number }  // <= requested (A3)
export interface DecisionNoteBody { note?: string }        // deny / revoke, <= 500 chars
export type SupportGrantErrorCode =
  | 'support_grant_expired' | 'support_grant_revoked' | 'support_grant_not_active'
  | 'support_grant_invalid' | 'support_access_unavailable'
  | 'support_grant_already_decided' | 'support_grant_lapsed' | 'support_grant_self_approval';
```

| Client method (`SupportAccessApi.*`) | HTTP | Who |
|---|---|---|
| `listMyGrants({status, scope:'mine'\|'all', limit, offset})` | `GET /support-access/grants` | admin (own), super_admin (`scope=all`) |
| `getGrant(uuid)` | `GET /support-access/grants/{uuid}` (A5) | grantee / super_admin |
| `requestGrant(body)` | `POST /support-access/grants` → `SupportGrant` | admin |
| `breakGlass(body)` | `POST /support-access/grants/break-glass` → `SupportGrant` | super_admin |
| `revokeGrant(uuid, body)` | `POST /support-access/grants/{uuid}/revoke` | grantee, super_admin, org:admin |
| `searchOrganizations(q, limit)` | `GET /support-access/targets/organizations` (A6) | admin |
| `AdminApi.searchUsers({query, limit})` (existing) | `GET /admin/users/search` | admin |
| `listOrgRequests({status, limit, offset})` | `GET /org-admin/support-access` | org:admin |
| `approveOrgRequest(uuid, body)` / `denyOrgRequest(uuid, body)` | `POST /org-admin/support-access/{uuid}/{approve,deny}` | org:admin |
| `listMyWorkspaceRequests(...)` | `GET /users/me/support-access` | subject user |
| `approveMyRequest` / `denyMyRequest` | `POST /users/me/support-access/{uuid}/{approve,deny}` | subject user |
| tenancy mode | `GET /system/capabilities` → `tenancy_mode` (A1) | everyone |

**Header rule.** `X-Support-Access-Grant: <uuid>` is sent **only** while a support session is
active in this tab, and **never** on the exempt prefixes `/auth/`, `/system/`, `/support-access/`,
`/org-admin/support-access`, `/users/me/support-access` or `/chat` (chat is owner-only and has no
tenant bypass, see backend plan §3.4). Raw `fetch` callers get it through the same helper
(`thumbnailCache.ts`). `chatStream.ts` is exempt.

## 2. Backend contract amendments

Amend `1122_platform_admin_tenancy_bypass_plan.md` with these before the frontend starts. Each
one is a gap or ambiguity in the backend plan that the UI would otherwise have to guess at.

- **A1. Expose the mode.** Add `tenancy_mode: "single" | "multi"` to `GET /system/capabilities`
  (resolved by `tenancy_mode(db)`). Nothing in the backend plan tells the client which mode it is
  in, and the routes 404 in SINGLE, so without this the UI would have to probe and guess.
- **A2. Request bodies, exactly.** Specify the schemas in §1: targets by **uuid** (never int
  ids), exactly one target (422 otherwise), `reason` 10..2000 chars, `duration_minutes`
  15..480 (break-glass 15..240, default 60), and `ticket_ref` 1..255 required for break-glass.
- **A3. Persist the requested duration.** The model has no column for it, but `approve` computes
  `expires_at = decided_at + requested duration`. Add `requested_duration_minutes INT NOT NULL`
  plus CHECK. The approve body takes an optional `duration_minutes` ≤ requested. The approver may
  **shorten** the duration but never extend it. Deny/revoke accept an optional `note` (≤ 500),
  recorded in the audit `details` only.
- **A4. Response schema with server-computed `status`.** Add `SupportGrantOut` as in §1. Embed
  `UserRef`/`OrgRef` objects (nullable for SET NULL actors) rather than ids. `status` and
  `pending_expires_at` are computed by the service, so the client never re-implements
  "pending but older than 72 h = lapsed".
- **A5. List envelope and detail route.** All three list routes return
  `{items, total, server_time}` and take `status`, `limit` (≤ 100) and `offset`, filtered and
  paginated in SQL, newest first. `server_time` lets the countdown correct for client clock skew.
  Add `GET /support-access/grants/{uuid}` (grantee or super_admin), which activation and reload
  restore need.
- **A6. Target lookup.** Add `GET /support-access/targets/organizations?q=&limit=` (admin;
  returns active orgs as `uuid,name,slug` only, a §3.3 platform op). Personal targets reuse the
  existing `/admin/users/search`. Add both to `test_route_privilege_tiers.py`.
- **A7. Machine-readable error codes.** Set `detail.code` from §1's `SupportGrantErrorCode` on:
  header resolution 403s (`expired`, `revoked`, `not_active` for pending/denied, `invalid` for
  wrong grantee, demoted grantee or inactive org); the SINGLE-mode 400
  (`support_access_unavailable`); 409 on a re-decision (`already_decided`) or a lapsed pending
  request (`lapsed`); and 403 on self-approval. Without codes the client can't tell "this grant
  is dead, end the session" apart from an ordinary 403 (for example a read grant attempting a
  write). Clearing the session on every 403 would be wrong, and so would ignoring them all.
- **A8. Lifecycle routes ignore the header.** The `/support-access/*`, `/org-admin/support-access*`,
  `/users/me/support-access*`, `/auth/*` and `/system/*` routes must not resolve
  `X-Support-Access-Grant`. Otherwise a stale header 403s the very "end/revoke" call that would
  clear it. Revoking a grant that is already expired or revoked returns **200** with the current
  grant (idempotent), not an error. The frontend strips the header there too (defence in depth).
- **A9. Thumbnail route honours the header.** The optional-auth thumbnail route uses
  `resolve_org_context` directly, not `get_current_context`. Unless it also resolves the grant,
  every gallery thumbnail 404s during an org session.
- **A10. WebSocket events.** Use snake-case `type` names like the existing ones:
  `support_access_requested` (to the target's org:admins or subject user:
  `{grant_uuid, grantee_name}`), `support_access_break_glass` (to org:admins or subject:
  `{grant_uuid, grantee_name, target_name, expires_at}`), and `support_access_decided` /
  `support_access_revoked` (to the grantee: `{grant_uuid, status}`). None name a `MediaFile`.
  Add all four to the `test_ws_event_quarantine_discipline.py` allowlist with that reason.
  Revocation then ends the grantee's banner in real time, not on the next 403.
- **A11. Break-glass on a personal workspace.** The backend plan only describes org targets.
  Decide this explicitly. **Recommended:** allow it, notify the subject user, and list it in
  `/users/me/support-access`. Refuse break-glass targeting the caller's own workspace (422).
- **A12. Compose passthrough.** Add `TENANCY_MODE: ${TENANCY_MODE:-auto}` to the backend and
  celery `environment:` in `docker-compose.yml`. Then a `--fresh` stack can run MULTI for e2e
  (§6.2) without anyone editing `.env`.
- **A13. Capability mounts.** Keep `/org-admin/support-access*` under the `organizations`
  capability, but `/support-access/*` and `/users/me/support-access*` must **not** be under it.
  Otherwise forced-MULTI community has no working grant path at all.

## 3. Screens, components, navigation

All new UI is absent unless `tenancyMode === 'multi'` (§4.4). That follows the "deployment lacks
it → omit" rule. Within MULTI, privilege gating uses `SECTION_MIN_ROLE` (locked + padlock, never
omitted).

### 3.1 Platform support staff: Settings → Administration → "Support access" (`support-access`, tier `admin`)

`SupportAccessStaffPanel.svelte` is the coordinator. It owns load, polling and actions.
- Header actions: **Request access** (opens `RequestAccessModal`) and **Break glass**. For a plain
  admin the Break glass button renders `disabled` with a padlock and the
  `supportAccess.breakGlass.superAdminOnly` tooltip. It is not omitted (settings convention).
- For super_admin, `Tabs` shows "My grants" | "All grants" (`scope=all`). Admins see "My grants"
  with no tabs.
- `GrantList` (perspective `staff`) has these columns: target, access level, mode, reason,
  ticket, requested, expires, status, actions. Row actions depend on status:
  - `active`: **Start session** (§4.2) and **Revoke**.
  - `pending`: **Revoke** (withdraws the request).
  - Terminal statuses: none.
  - While a session for that grant is active, the row shows "In use" instead of Start session.
- Active personal grant: `OpenFileByUuid` (input + "Open file") appears under the row, because
  personal grants are by-UUID only (backend plan §9). It validates the uuid shape client-side
  (UX only) and then `goto('/files/<uuid>')`.
- Polls `listMyGrants` every 30 s while mounted, so a pending request turns active without a
  reload. The `support_access_decided` WS event triggers an immediate reload. Clean up in
  `onDestroy`.
- Empty and error states are separate (`LockedAccountsPanel` precedent). "No grants" is not
  "load failed".

`RequestAccessModal.svelte` (`BaseModal`) has a target-type radio (Organization / User's
personal workspace), a `SearchableSelect` whose `fetchFn` is `searchOrganizations` or
`AdminApi.searchUsers`, an access-level radio (default `read`), a duration `<select>`
(15/30/60/120/240/480 min, default 60), and a reason textarea with a live counter. The
`minlength` of 10 is a UX hint only; server 422s render through `getErrorMessage`. On submit the
returned pending grant is prepended to the list and a toast shows.

`BreakGlassModal.svelte` is a two-step `BaseModal`. **Step 1** has the request fields plus a
required **Ticket reference**, durations 15-240 (default 60), and a warning callout
(`--warning-*` tokens). **Review** moves to **step 2**, which shows `breakGlass.confirmSummary`
(target, level, duration). The user must type the target's display name exactly to enable
**Start break-glass access**. **Back** keeps the values. Focus moves to the step heading on each
step change. On success: toast, close, and an offer to **Start session now** (`activate()`).

### 3.2 Banner while acting under a grant

`SupportAccessBanner.svelte` is mounted in `routes/+layout.svelte` directly after `<Navbar />`,
only when `$supportSession.active`. It reads `banner.org` / `banner.personal`, then
`banner.expires`, where `{{time}}` comes from a new `formatTimeOfDay(iso, $locale)` in
`lib/utils/formatting.ts` and `{{remaining}}` from the existing `formatClock(seconds)`. Chips:
Break-glass (`Badge variant="error"`) and Read only / Read and write (`warning` / `info`).
**End session** stops sending the header and leaves the grant valid until expiry. **Revoke
access** shows a `ConfirmationModal`, calls `revokeGrant`, then ends the session. The banner is
`position: sticky` under the fixed navbar (`--support-banner-height: 40px`); `+layout.svelte`
adds it to the content offset the same way `.app.has-banner` handles the 28 px classification
banner, so verify both together (§6.3). At narrow widths the text wraps and buttons stay ≥ 44 px.

### 3.3 Approvers: Settings → Account → "Support access requests" (`support-access-requests`, no role tier)

`SupportAccessApprovalsPanel.svelte` is the coordinator.
- Tabs: **My workspace** (`/users/me/support-access`, every user) and, only when
  `orgAdminCapOn(capState,'organizations') && $user.org_role === 'org:admin'`,
  **Organization** (`/org-admin/support-access`). That gate is cosmetic. `require_org_admin` is
  the authority.
- Each tab has a "Pending" `GrantList` (perspective `approver`, rows Approve / Deny), followed by
  a "History" `GrantList` with a status filter `<select>`, plus offset paging
  (Load more, `limit` 25). Active rows offer **Revoke**.
- Break-glass rows are highlighted (left border `--error-color`) and show the ticket reference.
- **Approve** opens `ApproveGrantModal`. It shows the requester, reason and requested duration,
  with a duration `<select>` limited to options ≤ requested, defaulting to requested.
- **Deny** and **Revoke** open `GrantDecisionNoteModal` (optional note).
- A 409 `support_grant_already_decided` or `lapsed` shows the `supportAccess.alreadyDecided`
  toast and reloads. Never retry (`isAlreadyDecided` precedent in `userApprovals.ts`).
- A 403 `self_approval` shows `supportAccess.selfApproval`.
- Sidebar badge = pending total across both tabs. `SettingsModal` owns it, like
  `pendingApprovalCount`. It is fetched with `limit=1&status=pending` on modal open and bumped by
  `support_access_requested`.
- WS `support_access_break_glass` raises a persistent `toastStore.error`-style warning toast with
  a "Review" action that opens this section (`settingsModalStore.open('support-access-requests')`).

## 4. State, header injection, expiry

### 4.1 `src/lib/supportAccess/headers.ts` (no imports from stores or api, so no cycles)
```ts
export const SUPPORT_GRANT_HEADER = 'X-Support-Access-Grant';
let activeGrantUuid: string | null = null;              // set only by $stores/supportSession
export function setActiveSupportGrant(uuid: string | null): void;
export function isSupportAccessExempt(url: string): boolean; // §1 prefixes; works for '/api/...' and relative
export function getSupportAccessHeaders(url: string): Record<string, string>; // {} when none/exempt
```
`src/lib/supportAccess/errors.ts` exports `supportGrantErrorCode(err): SupportGrantErrorCode | null`
(reads `response.data.detail.code`) and `isSessionEndingCode(code)` (true for `expired`,
`revoked`, `not_active`, `invalid`, `support_access_unavailable`).

### 4.2 `src/stores/supportSession.ts`
State: `{ active: boolean; grantUuid; targetLabel; targetKind: 'organization'|'personal';
level; mode; expiresAtMs; skewMs; remainingSeconds }`.
- `activate(grantUuid)`:
  1. Refuse unless `get(capabilities).tenancyMode === 'multi'`.
  2. `getGrant(uuid)` (no header, exempt). Require `status === 'active'`, otherwise
     toast `session.activateFailed` and stop.
  3. Compute `skewMs = Date.parse(server_time) - Date.now()`.
  4. **Purge tenant data caches**: `apiCache.clear()`, `clearThumbnailCache()`,
     `clearMediaUrlCache()`, `galleryStore.resetFilters()`, `searchStore.reset()`,
     `transcriptStore.clear()`, `chatStore.reset()`. This is the same set as `clearUserState`
     minus auth, toast, uploads and recording. Put it in an exported `purgeTenantDataCaches()` in
     `clearUserState.ts` so the list has one home.
  5. `setActiveSupportGrant(uuid)`, persist to **`sessionStorage`** key `opentr:supportSession`.
     That key is tab-scoped on purpose: a grant must not become ambient in other tabs (backend
     plan §4 rejects ambient authority).
  6. Start a 1 s interval, then `goto('/')`.
- `end(reason: 'user'|'expired'|'revoked'|'invalid'|'logout')`:
  1. Clear the interval.
  2. `setActiveSupportGrant(null)`, remove the storage key.
  3. `purgeTenantDataCaches()`, toast `session.<reason>` (except for `logout`).
  4. `goto('/')` unless the reason is `logout`. End is idempotent: a second call is a no-op.
- Tick: `remainingSeconds = max(0, ceil((expiresAtMs - (Date.now()+skewMs))/1000))`. At 0, call
  `end('expired')`. The tick also feeds the announcer (§6.1).
- `restore()` is called once from `+layout.svelte` after capabilities load. It reads storage and
  re-validates through `activate` minus the redirect. If the grant is not active or the mode is
  not multi, it clears silently.
- `clearUserState` calls `end('logout')` and removes the storage key. Add both to
  `clearUserState.completeness.test.ts`.

### 4.3 `lib/axios.ts`
- Request interceptor: after the auth headers, merge `getSupportAccessHeaders(config.url)`.
- Response interceptor: before the 401 branch, if the failed request carried the header and
  `isSessionEndingCode(supportGrantErrorCode(error))` is true, lazily import
  `$stores/supportSession` and call `end(code === 'support_grant_revoked' ? 'revoked' : code
  === 'support_grant_expired' ? 'expired' : 'invalid')`, then reject.
- Other 403/404s pass through untouched.
- A 401 keeps today's refresh path. If refresh fails, `authStore.reset()` → `clearUserState`
  ends the session.
- `thumbnailCache.fetchAndCache` passes `{ headers: getSupportAccessHeaders(url) }`.
- WS: `support_access_revoked` / `decided` with a status other than `active` for the active
  grant calls `end('revoked')`.

### 4.4 SINGLE mode and fail-closed visibility
`capabilities.ts` gains `tenancyMode: 'single' | 'multi' | undefined`. It is `'multi'` only when
the response field is exactly `"multi"`, and `undefined` before load, on fetch failure, or on an
older backend. `resetCapabilities` resets it. The two nav rows, the banner, `activate`/`restore`
and the WS handlers all check `tenancyMode === 'multi'`. This deliberately does **not** use `isCapabilityEnabled`, because that is fail-open and would
show the UI on a community install whose capabilities fetch failed. In SINGLE mode no request
can carry the header, because the store can never become active. A vitest pins this (§6.2).

## 5. i18n

All keys are flat and English is given below. All 12 locales (`ar de en es fr it ja ko nl pt ru zh`)
get **real translations** in the same commit as the key. Parity alone is not translation
(root `CLAUDE.md`).
- Avoid plural keys. Durations render as `{{count}} min` / `{{count}} h`, because `ar` needs six
  plural forms and these units sidestep that.
- Keep `{{var}}` tokens untranslated.
- Checks:
  - `cd frontend && npm run check:i18n`. Run it explicitly, because a locale-only commit does not
    fire the hook.
  - Grep that no non-`en` file has a value byte-identical to `en` for any `supportAccess.*` key,
    except pure tokens.
  - View the banner, request modal and approvals panel in `ar`: `dir="rtl"`, chips and buttons
    mirror, the countdown stays LTR (wrap `{{remaining}}`/`{{time}}` in `<bdi>`), and padding
    uses logical properties (`padding-inline-start`, `margin-inline-end`).

| Key | English |
|---|---|
| `settings.supportAccess.navLabel` / `.title` | Support access |
| `settings.supportAccess.description` | Request time-limited, audited access to a tenant's content. Every use is logged where the tenant can see it. |
| `settings.supportAccessRequests.navLabel` | Support access requests |
| `settings.supportAccessRequests.title` | Support access to your data |
| `settings.supportAccessRequests.description` | Platform staff must ask before they can open your content. Approve, deny or revoke their requests here. |
| `supportAccess.tabs.mine` / `.all` / `.personal` / `.organization` | My grants / All grants / My workspace / Organization |
| `supportAccess.section.pending` / `.history` | Pending / History |
| `supportAccess.status.pending` / `.active` / `.denied` / `.expired` / `.revoked` / `.lapsed` | Pending / Active / Denied / Expired / Revoked / Request lapsed |
| `supportAccess.status.inUse` | In use |
| `supportAccess.level.read` / `.write` | Read only / Read and write |
| `supportAccess.mode.approved` / `.break_glass` | Approved / Break-glass |
| `supportAccess.col.target` / `.grantee` / `.level` / `.mode` / `.reason` / `.ticket` / `.requested` / `.expires` / `.status` / `.actions` | Target / Requested by / Access / Type / Reason / Ticket / Requested / Expires / Status / Actions |
| `supportAccess.personalWorkspace` | Personal workspace of {{name}} |
| `supportAccess.deletedUser` | Deleted user |
| `supportAccess.filter.status` / `.all` | Status / All |
| `supportAccess.loadMore` | Load more |
| `supportAccess.empty.staff` / `.pending` / `.history` | You have no support-access grants. / No pending requests. / No past requests. |
| `supportAccess.loadFailed` | Could not load support-access grants. |
| `supportAccess.action.request` / `.breakGlass` / `.activate` / `.approve` / `.deny` / `.revoke` / `.endSession` / `.openFile` | Request access / Break glass / Start session / Approve / Deny / Revoke / End session / Open file |
| `supportAccess.duration.minutes` / `.hours` | {{count}} min / {{count}} h |
| `supportAccess.request.title` | Request support access |
| `supportAccess.request.targetType` / `.targetOrganization` / `.targetUser` | Target / Organization / User's personal workspace |
| `supportAccess.request.searchOrganization` / `.searchUser` | Search organizations / Search users |
| `supportAccess.request.level` / `.duration` / `.reason` | Access level / Duration / Reason |
| `supportAccess.request.reasonHint` | At least 10 characters. The tenant can read this. |
| `supportAccess.request.reasonCount` | {{count}} / 2000 |
| `supportAccess.request.submit` / `.sent` / `.failed` | Send request / Request sent. Waiting for approval. / Could not send the request. |
| `supportAccess.breakGlass.title` | Emergency break-glass access |
| `supportAccess.breakGlass.warning` | Break-glass access starts immediately, without approval. The tenant's administrators are notified and it appears in their audit log. |
| `supportAccess.breakGlass.ticket` / `.ticketHint` | Ticket reference / Required. The incident or support ticket that justifies this access. |
| `supportAccess.breakGlass.maxDuration` | Maximum 4 hours. |
| `supportAccess.breakGlass.review` / `.back` | Review / Back |
| `supportAccess.breakGlass.confirmTitle` | Confirm break-glass access |
| `supportAccess.breakGlass.confirmSummary` | You are about to open {{level}} access to {{target}} for {{duration}}. |
| `supportAccess.breakGlass.confirmTypeName` | Type {{target}} to confirm |
| `supportAccess.breakGlass.confirm` | Start break-glass access |
| `supportAccess.breakGlass.created` / `.failed` | Break-glass access is active. / Could not start break-glass access. |
| `supportAccess.breakGlass.startNow` | Start session now |
| `supportAccess.breakGlass.superAdminOnly` | Break-glass requires super admin |
| `supportAccess.approve.title` | Approve support access |
| `supportAccess.approve.requestedDuration` | Requested duration: {{duration}} |
| `supportAccess.approve.grantFor` / `.shortenOnly` | Grant access for / You can shorten the requested duration, not extend it. |
| `supportAccess.approve.approved` / `.failed` | Access approved. / Could not approve the request. |
| `supportAccess.deny.title` / `.denied` / `.failed` | Deny request / Request denied. / Could not deny the request. |
| `supportAccess.note.label` | Note (optional) |
| `supportAccess.revoke.title` / `.body` / `.revoked` / `.failed` | Revoke access / Access ends immediately for {{name}}. / Access revoked. / Could not revoke access. |
| `supportAccess.alreadyDecided` | Someone already decided this request. The list has been refreshed. |
| `supportAccess.selfApproval` | You cannot approve your own request. |
| `supportAccess.banner.region` | Support access session |
| `supportAccess.banner.org` / `.personal` | Support access: acting in {{org}} / Support access: acting in {{name}}'s workspace |
| `supportAccess.banner.expires` | Expires at {{time}} ({{remaining}} left) |
| `supportAccess.announce.started` | Support access session started. It expires at {{time}}. |
| `supportAccess.announce.fiveMinutes` / `.oneMinute` | Support access ends in 5 minutes. / Support access ends in 1 minute. |
| `supportAccess.session.user` / `.expired` / `.revoked` / `.invalid` | Support access session ended. / Support access expired. / Support access was revoked. / Support access is no longer valid. |
| `supportAccess.session.activateFailed` | This grant is not active. |
| `supportAccess.notify.requested` | New support-access request from {{grantee}}. |
| `supportAccess.notify.breakGlass` | {{grantee}} started break-glass access to {{target}} until {{time}}. |
| `supportAccess.notify.review` | Review |
| `supportAccess.openFile.label` / `.placeholder` / `.invalid` | File ID / Paste a file ID from the ticket / Not a valid file ID. |

## 6. Theming, accessibility, tests

### 6.1 Light/dark and a11y
- Colours come from tokens only (`--warning-bg`, `--error-color`, `--surface-color`,
  `--border-color`, …). Dark overrides use `:global([data-theme='dark'])`, never `.dark`
  (`theme-parity.test.ts` enforces this).
- Focus is an `outline` on `:focus-visible`. Never repaint `button:focus` (#746).
- Banner semantics:
  - `<section role="region" aria-label={$t('supportAccess.banner.region')}>`. The visible
    countdown has `aria-hidden="true"` on the ticking digits.
  - A separate visually-hidden `<div aria-live="polite" aria-atomic="true">` announces only
    `announce.started`, `fiveMinutes`, `oneMinute` and `session.*`. Announcing every second
    would spam screen readers.
  - Break-glass uses `aria-live="assertive"` for the start announcement.
- Modals rely on `BaseModal` focus trap, Esc and focus return. The break-glass confirm button is
  `disabled` until the typed name matches, with `aria-describedby` pointing at the instruction.
- `GrantList` is a real `<table>` with `<th scope="col">` and a `<caption>` (visually hidden).
  Action buttons carry `aria-label`s naming the target ("Revoke {{target}}" via the existing
  label + target text).
- Keyboard: every flow is completable with Tab/Shift-Tab/Enter/Space/arrows (`Tabs` has
  roving tabindex).
- axe: §6.2 scans the panels, both modals and the banner in light and dark with
  `a11y_lib`. Expect no serious/critical findings, and add no allowlist entries.

### 6.2 Tests (each must be seen red first: run the test before writing the code)

vitest (jsdom). Copy the mocking idiom of `ActiveSessionsPanel.test.ts` (mock `$lib/axios`,
identity `t`). Assertions must name exact URLs, bodies and rendered keys. `npm run test:audit`
must report 0 findings.

| Test file | Falsifiable assertions |
|---|---|
| `lib/api/supportAccess.test.ts` | each method hits the exact path/verb/body/params of §1; `revokeGrant` posts `{note}`; uuids are path-encoded |
| `lib/supportAccess/headers.test.ts` | no header when inactive; header present for `/files/x`; **absent** for each exempt prefix (table-driven, one case per prefix including `/api/`-prefixed forms); cleared after `setActiveSupportGrant(null)` |
| `lib/supportAccess/errors.test.ts` | each code is extracted; a plain 403 without a code returns `null`; `isSessionEndingCode` is false for `already_decided` |
| `lib/axios.supportAccess.test.ts` | real interceptor via `axios-mock-adapter` or the existing `axios.test.ts` pattern: header sent on `/files` while active, not on `/auth/me`; a 403 with `support_grant_revoked` calls `end('revoked')`; a 403 **without** a code does not call `end` |
| `stores/supportSession.test.ts` | `activate` refuses when `tenancyMode !== 'multi'` (no `getGrant` call); refuses a `pending` grant; fake timers: countdown uses `server_time` skew (server +120 s → remaining is 120 s shorter); at 0 calls `end('expired')` and clears `sessionStorage`; `end` twice → one toast; `restore` with a revoked grant clears silently; `purgeTenantDataCaches` invoked on activate and end |
| `stores/capabilities.test.ts` (extend) | `tenancy_mode:"multi"` → `'multi'`; missing, `"MULTI"`, or fetch failure → `undefined` |
| `lib/session/clearUserState.completeness.test.ts` (extend) | `end('logout')` called; `opentr:supportSession` removed |
| `components/supportAccess/SupportAccessBanner.test.ts` | renders `supportAccess.banner.org` + `supportAccess.level.read`; Break-glass chip only for `break_glass`; live region text changes at 300 s and 60 s, not at 299 s; End session calls `end('user')`; Revoke confirms, then posts revoke, then ends |
| `.../RequestAccessModal.test.ts` | submit disabled until a target is picked and the reason is ≥ 10 chars; posts `organization_uuid` XOR `subject_user_uuid` (switching target type nulls the other); a 422 detail is rendered |
| `.../BreakGlassModal.test.ts` | no 480 option; ticket required; Review → step 2 shows the summary; confirm disabled until the exact name is typed (a case-different name stays disabled); Back keeps values; posts to `/break-glass` |
| `.../ApproveGrantModal.test.ts` | duration options are ≤ requested only (requested 60 → no 120); posts `{duration_minutes}` |
| `.../GrantList.test.ts` | per-status action matrix for both perspectives (table-driven); null `grantee` renders `supportAccess.deletedUser` |
| `components/settings/SupportAccessStaffPanel.test.ts` | admin: Break glass disabled with tooltip; super_admin: enabled + All tab calls `scope=all`; empty vs load-failed render different keys |
| `components/settings/SupportAccessApprovalsPanel.test.ts` | Organization tab absent for a non-org-admin; 409 `already_decided` → toast + reload (2nd GET), no retry POST; self-approval key shown |
| `components/SettingsModal.supportAccess.test.ts` | `tenancyMode` undefined → neither nav row exists; multi + admin → both; multi + user → only the requests row |

**E2E** (`backend/tests/e2e/test_support_access.py`; follow `backend/tests/CLAUDE.md` safety rules).

- **Control, unmarked:** `test_single_mode_hides_support_access` runs on the normal SINGLE dev
  stack. It asserts no nav rows and no banner, and a `page.on('request')` recorder over a gallery
  + file visit sees zero `x-support-access-grant` headers.
- **Marker `support_access`** covers everything else. Register it in `tests/e2e/pytest.ini` and
  deselect it in both `scripts/e2e/run-e2e.sh` phase expressions
  (`... and not support_access`): deselect, never skip. A module preflight **fails** if
  `tenancy_mode != "multi"`. Run it on an isolated stack (needs A12):
  `TENANCY_MODE=multi ./opentr.sh start dev --fresh support --port-offset 200`, export the
  offset `*_PORT` vars, then `pytest backend/tests/e2e/test_support_access.py -m support_access -v`.
- **Fixtures:** subject user `support-e2e-<uuid4>@example.com` created via the admin API, plus
  one small upload by that user (prefix `support-e2e-`; the assertions need only the file
  detail page, not a finished transcript, so no GPU). A finalizer revokes every grant created,
  deletes the file (falling back to `/force`) and deletes the user. Add `support-e2e-` to
  `scripts/cleanup-test-users.py` `ORPHAN_PATTERNS` and `scripts/cleanup-test-data.py` in the
  same commit.
- **Flows:**
  1. Request → deny (the subject uses a second browser context, with a note). Admin's row shows
     Denied.
  2. Request → approve 15 min → admin Start session. The banner shows the subject name + Read
     only. `/files/<uuid>` renders the file header. The recorder sees the header on
     `/api/files/<uuid>*` and **not** on `/api/auth/*` or `/api/support-access/*`. End session
     removes the banner, and `/files/<uuid>` then shows the not-found state.
  3. Subject revokes during a session. Within 5 s (WS, A10) the admin banner is gone with the
     revoked toast.
  4. Break-glass as `admin@example.com` (seeded super_admin): Review fails without a ticket, the
     typed confirmation is completed, and the session starts at once. The subject's History
     shows Break-glass + ticket.
  5. axe light/dark scan of both panels, both modals and the banner via `a11y_lib`.
- **Not covered by e2e, stated in the PR:**
  - Expiry is never waited on in e2e. Fake-timer vitest + backend test 19 cover it.
  - The org-grant flow can't run in community (no IdP-mirrored orgs). Vitest + backend tests
    15-22 cover it.

### 6.3 Browser verification checklist (`DISPLAY=:11`, `--fresh` MULTI stack, then the SINGLE dev stack)
- [ ] SINGLE: no nav rows, no banner, DevTools shows no header.
- [ ] MULTI admin: Request modal, then the subject approves, then start session. Banner in light
  **and** dark, plus banner stacked under the classification banner (enable it in the fresh
  stack's admin UI).
- [ ] Countdown matches the server: the banner's absolute time equals the grant's `expires_at`,
  and the remaining time drifts < 2 s from it. Disappearance at 0 is covered by the fake-timer
  vitest (minimum TTL is 15 min), but watch one 15-min grant run out once.
- [ ] Break-glass: plain admin sees a padlocked button with a tooltip. super_admin sees both
  steps, Esc/Back keep values, and focus moves to the step heading.
- [ ] `ar`: RTL layout, `<bdi>` keeps `12:34` LTR, buttons mirror. Then `ja`/`de`: long strings
  wrap without overflow at 375 px width.
- [ ] Keyboard-only run of request → approve → start → end. Orca (or the axe report) confirms
  the started, 5-min and ended announcements fire once each.
- [ ] Reload mid-session: the banner restores in the same tab. A **new tab** has no banner (tab
  scope). Logout clears it.
- [ ] Gallery thumbnails load during an org session (A9). Chat sends no header.

## 7. Files, sequencing, risks

| File | New/Edit | Budget |
|---|---|---|
| `src/lib/api/supportAccess.ts` (+ test) | new | 170 |
| `src/lib/supportAccess/headers.ts` / `errors.ts` (+ tests) | new | 60 / 50 |
| `src/stores/supportSession.ts` (+ test) | new | 200 |
| `src/components/supportAccess/SupportAccessBanner.svelte` | new | 190 |
| `.../GrantList.svelte` · `GrantStatusBadge.svelte` | new | 200 · 50 |
| `.../RequestAccessModal.svelte` · `BreakGlassModal.svelte` | new | 240 · 270 |
| `.../ApproveGrantModal.svelte` · `GrantDecisionNoteModal.svelte` · `OpenFileByUuid.svelte` | new | 150 · 110 · 80 |
| `src/components/supportAccess/CLAUDE.md` | new | 40 (header rule, exempt list, fail-closed gate, tab-scoped storage) |
| `src/components/settings/SupportAccessStaffPanel.svelte` · `SupportAccessApprovalsPanel.svelte` | new | 260 · 280 |
| `src/components/SettingsModal.svelte` | edit | +45 (2 rows, `SECTION_MIN_ROLE['support-access']='admin'`, render blocks, badge) |
| `src/stores/settingsModalStore.ts` | edit | +6 (2 section ids, dirtyState) |
| `src/stores/capabilities.ts` | edit | +12 (`tenancyMode`) |
| `src/lib/axios.ts` | edit | +25 |
| `src/lib/thumbnailCache.ts` | edit | +3 |
| `src/stores/websocket.ts` | edit | +30 (4 events, lazy store import) |
| `src/lib/session/clearUserState.ts` | edit | +25 (`purgeTenantDataCaches` export + logout end) |
| `src/lib/utils/formatting.ts` | edit | +15 (`formatTimeOfDay(iso, locale)` via `Intl.DateTimeFormat`) |
| `src/routes/+layout.svelte` | edit | +15 (banner mount, offset var, `restore()`) |
| `src/lib/i18n/locales/*.json` ×12 | edit | ≈95 keys each |
| `components/settings/CLAUDE.md`, `lib/api/CLAUDE.md`, `stores/CLAUDE.md` | edit | 1 bullet each |
| `backend/tests/e2e/test_support_access.py`, `tests/e2e/pytest.ini`, `scripts/e2e/run-e2e.sh`, cleanup scripts | new/edit | 300 / +1 / +2 / +2 |

`SettingsModal.svelte` is already 1,786 lines. Add only the rows and render blocks there. All
logic lives in the two panels.

**Sequencing.** One worktree, `feat/1122-support-access-frontend`, one PR into the active
upstream, and one commit per phase. Each commit passes `scripts/safe-precommit.sh run --all-files`
(both tiers before push), `npm run check`, `npm run build`, `npm run test`, `npm run test:audit`
and `npm run check:i18n`, and carries its own 12-locale strings.
1. **F0, plumbing:** api client, headers, errors, `supportSession`, capabilities, axios,
   thumbnail, `clearUserState`, `formatting.ts`, plus their tests. No visible UI.
2. **F1, staff UI:** `GrantList`, badge, both request modals, the staff panel, the banner, and
   the SettingsModal row.
3. **F2, approver UI:** approvals panel, approve/note modals, WS events, sidebar badge.
4. **F3, e2e + docs:** e2e file + marker + cleanup prefixes, the folder `CLAUDE.md`s, and a
   docs-site screenshot refresh (`docs-screenshots` skill) of the "Support access & break-glass"
   page the backend plan §5.6 adds.

Merge only after backend Phase 3 + A1-A13. The backend plan §8 says Phases 1-2 can't merge
without Phase 3. This PR should land in the same release, so MULTI operators have a UI rather
than curl.

**Risks.**
- **Stale tenant data shown after a scope switch.** Mitigated by `purgeTenantDataCaches()` on
  both edges plus `goto('/')`. A cache added later that is not registered there leaks across the
  switch, so the completeness test must cover it.
- **Header leaking onto lifecycle or chat calls.** Mitigated by the exempt list, the
  table-driven test and A8 server-side.
- **WS gaps.** The grantee's socket is not tenant-scoped, so live file-status updates for the
  assumed tenant don't arrive. Accepted for v1 (the gallery refetches on navigation). Document
  it in the folder `CLAUDE.md`.
- **Clock skew.** Handled with `server_time`. The server stays authoritative, and a late client
  gets an A7 403 that ends the session anyway.
- **Fail-open capability habit.** A future refactor that swaps the explicit `tenancyMode` check
  for `isCapabilityEnabled` would show the UI on community. `SettingsModal.supportAccess.test.ts`
  pins this.
- **Org path not browser-verified in community.** Stated in the PR. Verify it in the managed
  edition's staging before the release note claims it.

## 8. Summary

1. The frontend consumes the backend plan's grant routes through `$lib/api/supportAccess.ts`,
   with types mirroring an amended `SupportGrantOut`.
2. Thirteen backend amendments (A1-A13) are required first. The main ones are the exposed
   `tenancy_mode`, persisted requested duration, server-computed status, error codes, a header-free
   lifecycle, thumbnail header support and WS events.
3. Staff get Settings → Administration → Support access: request, break-glass (super_admin, two
   steps with typed confirmation), start/end session, revoke.
4. Approvers get Settings → Account → Support access requests: My workspace plus an Organization
   tab for org:admins, with approve (shorten-only), deny, revoke and history.
5. A tab-scoped `supportSession` store drives the `X-Support-Access-Grant` header through the
   axios interceptor, with an exempt list. A sticky banner shows a skew-corrected countdown.
6. The session auto-ends on expiry, on grant-specific 403 codes, on WS revoke and on logout.
   Tenant caches are purged on both edges.
7. Visibility is fail-closed: everything is hidden unless the backend says `tenancy_mode ===
   "multi"`.
8. About 95 new flat i18n keys, really translated in all 12 locales, with no plurals. `ar` is
   checked for RTL with `<bdi>` around times.
9. Tests: about 15 vitest files with falsifiable assertions, an unmarked SINGLE-mode e2e
   control, and a `support_access` e2e marker run on a `--fresh` `TENANCY_MODE=multi` stack, with
   axe in light and dark.
10. Delivery is one PR with four phase commits (F0-F3) after backend Phase 3. New files stay
    under 300 lines, and SettingsModal gets only routing rows.

Doc path: `docs/design/1122_support_access_frontend_plan.md`.
