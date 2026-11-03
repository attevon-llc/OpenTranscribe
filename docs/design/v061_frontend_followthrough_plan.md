# 0.6.1 frontend follow-through: implementation plan

Status: PLAN. Written 2026-11-02 against `release/v0.6.1` @ `d267f879a`. Input was
`~/ot-cleanup-work/audit-v061/frontend-impact.md`; every claim used below was re-checked in the
code, and the corrections are marked **(verified)** or **(corrected)**.

**Who does this: ONE Sonnet implementer, one worktree, one branch.** All items are small and touch
different files. The one place two items meet is `routes/+page.svelte` (items 4 and 7 both edit the
bulk handlers), so do those two together, in the order given.

## 0. Setup (do this first)

```bash
# The backend part builds on the last-super-admin block, which is not on release/v0.6.1 yet.
git worktree add .claude/worktrees/v061-fe -b fix/v061-frontend-followthrough fix/audit-v061-holds-and-last-admin
cd .claude/worktrees/v061-fe
git log --oneline -3   # must show 4b5f9dd9a and 2b5a4693a on top of d267f879a
```

- If `fix/audit-v061-holds-and-last-admin` has been merged into `release/v0.6.1` by then, branch
  from `release/v0.6.1` instead. Check: `git merge-base --is-ancestor 4b5f9dd9a release/v0.6.1`.
- Item 3 depends on `fix/audit3-scope-purge-holds` (see the CONTRACT-TO-VERIFY block in §3). Do
  items 1, 2, 4, 7, 8, 10 first; do item 3 last, after reading that branch.
- Read before editing: `frontend/CLAUDE.md`, `frontend/src/components/privacy/CLAUDE.md`, and the
  `CLAUDE.md` of every other `frontend/src/**` folder you touch (`git ls-files 'frontend/src/**/CLAUDE.md'`),
  `backend/tests/CLAUDE.md`, `backend/app/api/CLAUDE.md`, and the root `CLAUDE.md` i18n rule.
- No `node_modules` in a fresh worktree: `cd frontend && npm ci`. Backend venv: use
  `/mnt/nvm/repos/transcribe-app/backend/venv/bin/` (3.12; it is not inside the worktree).
- You are the only writer in your worktree, so you may commit. Use `scripts/safe-precommit.sh`.

### i18n rules for every new key

- Locale files are FLAT JSON (`"a.b.c": "text"`) in `frontend/src/lib/i18n/locales/{ar,de,en,es,fr,it,ja,ko,nl,pt,ru,zh}.json`.
  Interpolation is `{{name}}`. Put each new key next to its siblings (same prefix).
- The translations in §11 are written out for you. Before pasting, compare the legal-hold word in
  each locale with that locale's existing `holds.refusal.organization` string, the "taken down"
  word with its `settings.quarantine.*` strings, and "super administrator" with
  `settings.accountErasure.errorLastSuperAdmin`. If the existing file uses a different term, USE
  THE EXISTING TERM, so the app uses one word for one thing.
- `ar` is RTL: no hard-coded `dir`, no left/right CSS (use `inline-start`/`inline-end`), and check it
  in the browser (VERIFICATION, manual checklist).
- `npm run check:i18n` checks only that every key exists in all 12 files, not that it is translated. The
  pre-commit hook does NOT fire on a commit that only touches locale JSON, so run it yourself.

### Seeing a new test fail first (repo rule)

Never revert files in your checkout. Use a HEAD archive:

```bash
RED="$(mktemp -d -t ot-redcheck.XXXXXX)" && git archive HEAD | tar -x -C "$RED"
ln -s "$PWD/frontend/node_modules" "$RED/frontend/node_modules"
cp frontend/src/<path>/<New>.test.ts "$RED/frontend/src/<path>/"      # new tests, old source
(cd "$RED/frontend" && NODE_OPTIONS=--no-experimental-webstorage npx vitest run src/<path>/<New>.test.ts)  # expect RED
rm -rf "$RED"
```

Do this before writing the fix for each item (commit the tests separately or keep them uncommitted;
`HEAD` must be the pre-fix code). Record the red test names in the commit body. A test that passes on
the old code is not proving anything: rewrite it.

---

## 1. FileVoiceprintConsent: separate messages for hold, quarantine, 503 and protected purge

**Backend shape (verified, `git show 2b5a4693a:backend/app/api/endpoints/files/voiceprint_consent.py`):**
- 404 `detail: "File not found"`: not the owner OR the file is quarantined.
- 409 `detail: {error: "FILE_UNDER_LEGAL_HOLD", message, held_by: [...]}` (`hold_guard.refusal_detail`).
- 503 string detail: the state WAS saved as declined, but the purge could not be queued.
- 403 support-grant `{code: "support_grant_action_not_permitted", message}`.
- Purge task: if every file in a files-scope is held or quarantined it aborts with
  `reason: "files_protected"`. On d267 `GET /privacy/purge/{task_id}` (`schemas/privacy.py:322 PurgeStatus`)
  has **no reason field**, so the frontend sees just `failed`. Item 3's contract adds what you read here.

**File:** `frontend/src/components/privacy/FileVoiceprintConsent.svelte`
- `change()` (≈ lines 57-73), replace the catch:
  ```ts
  } catch (error) {
    console.error('Could not change the voice identification choice:', error);
    const status = getErrorStatus(error);           // from $lib/utils/apiError (exists? if not, read error.response?.status)
    const detail = (error as {response?: {data?: {detail?: unknown}}})?.response?.data?.detail;
    if (status === 409 && (detail as {error?: unknown})?.error === 'FILE_UNDER_LEGAL_HOLD') {
      toastStore.error($t('file.voiceprintConsent.heldRefusal'));
    } else if (status === 404) {
      toastStore.error($t('file.voiceprintConsent.unavailable'));
    } else if (status === 503 && next === 'declined') {
      state = 'declined';            // the server saved it; show the truth
      resetEffectivePrivacy();
      toastStore.error($t('file.voiceprintConsent.purgePending'));
    } else {
      toastStore.error(getErrorMessage(error, $t('file.voiceprintConsent.saveFailed')));
    }
  }
  ```
  The last branch gives the support-grant refusal its own sentence (item 4) and keeps `saveFailed` as
  the fallback. Check `rg -n "export function getErrorStatus" frontend/src/lib/utils/apiError.ts`;
  if it does not exist, read `error.response?.status` inline. Do not add a helper used by one caller.
- `follow()` (≈ lines 38-60): terminal `failed` whose reason is the protected-files abort →
  `file.voiceprintConsent.purgeProtected` (key D). How to recognise it: the field from the §3
  contract. Until §3 is pinned, write it against that field and do this sub-step together with §3.
  Terminal `skipped` → `purgeDone` is NOT right if skipped can mean "held"; use what the §3 contract
  says `skipped` means. If it still means only "nothing to delete", show `purgeDone`, not `purgeFailed`.
- Edge cases: a 409 without `error` (e.g. another conflict) goes to the generic branch; a 503 on
  `granted` (cannot happen today) also goes to the generic branch; `state` is never set before
  success except in the 503 branch.

**Tests:** `frontend/src/components/privacy/FileVoiceprintConsent.test.ts` (copy the existing
'keeps the old state and reports an error when the save fails' setup). New `it(...)`:
1. `'says a held recording cannot change, without suggesting a retry'`: reject with 409 + hold detail;
   assert toast error called with the `heldRefusal` text, and the summary still shows the old state.
2. `'says a quarantined or missing recording is no longer available'`: 404.
3. `'shows the opt-out as saved when only the purge could not be queued (503)'`: assert the state
   text is `stateDeclined` AND toast `purgePending`.
4. `'surfaces the support-grant refusal sentence'`: 403 `{code:'support_grant_action_not_permitted', message:'X'}` → toast `'X'`.
5. `'says voiceprints were kept when the purge aborts for protected files'`: (with §3) `getPurgeStatus`
   resolves terminal failed + the protected reason → toast `purgeProtected`, not `purgeFailed`.

---

## 2. Self-erasure: show a request that the system blocked; specific cancel errors; show the time

### 2a. Backend (the one backend addition; small)

**Why a backend change is needed (verified):** `get_open_request`
(`backend/app/services/self_erasure_service.py:86`) returns only `deferred` + `grace_period` rows, so a
blocked request just disappears. `block_request` (`erasure_ledger_service.py`, commit `4b5f9dd9a`)
sets `status="cancelled"`, `deferred_reason=None`, and does NOT stamp `cancelled_at`. Telling it apart
by "`cancelled_at` is null" breaks as soon as anyone allows that column in the row guard, so store
the reason on the row:

- `backend/app/services/erasure_ledger_service.py::block_request`: change `entry.deferred_reason = None`
  to `entry.deferred_reason = reason` (`"last_super_admin"`, 16 chars; column is `String(20)`). Update
  the docstring. `deferred_reason` is already in the append-only guard's allowed lifecycle columns
  (`backend/app/db/audit_protection.py:91`), so no migration. Check: `rg -n "deferred_reason" backend/app`:
  every reader filters on `== "grace_period"` or `"error"`, and journal replay
  (`erasure_ledger_service.py:328`) only revives `grace_period`, so a `cancelled`+`last_super_admin`
  row is never revived or retried. Also add a module-level constant `BLOCKED_LAST_SUPER_ADMIN = "last_super_admin"`
  in `self_erasure_service.py` and pass it at the call site in `_execute_one`.
- `backend/app/services/self_erasure_service.py`: add
  ```python
  def get_last_blocked_request(db: Session, user_id: int) -> ErasureLedgerEntry | None:
      """The caller's most recent self-service request, if the system closed it without erasing."""
      latest = (
          db.query(ErasureLedgerEntry)
          .filter(ErasureLedgerEntry.subject_type == "user", ErasureLedgerEntry.subject_user_id == user_id)
          .order_by(ErasureLedgerEntry.requested_at.desc(), ErasureLedgerEntry.id.desc())
          .first()
      )
      if latest is None or latest.status != "cancelled" or latest.deferred_reason != BLOCKED_LAST_SUPER_ADMIN:
          return None
      return latest
  ```
  Only the LATEST row counts: a newer request (open, cancelled by the user, or executed) hides an
  older block. Check that operator-initiated erasures of a user do not use `subject_type == "user"`
  with a different meaning (read `gdpr_erasure_service.py`); if they do, also filter on the
  self-service actor kind the request path writes (`rg -n "actor_kind" backend/app/services/self_erasure_service.py`).
- `backend/app/schemas/self_erasure.py`: add
  ```python
  class SelfErasureOutcome(BaseModel):
      """A request the system closed without erasing anything. Codes only, no personal data."""
      status: Literal["blocked"]
      reason: Literal["last_super_admin"]
      ledger_uuid: UUID
      at: datetime | None = None   # scheduled_for of the blocked request (when it fell due)
  ```
  and on `SelfErasureStatus`: `last_outcome: SelfErasureOutcome | None = None`. Update the module
  docstring's list.
- `backend/app/api/endpoints/self_erasure.py::get_self_erasure_status` (line 79): fill `last_outcome`
  only when `open_request is None`.
- **Backend tests** (new file `backend/tests/api/test_self_erasure_blocked_outcome.py`; reuse the
  fixtures from `test_self_erasure_api.py`, i.e. `client`, `super_admin_token_headers`, `enabled`):
  1. `test_status_reports_a_request_blocked_as_last_super_admin`: create a request as a regular
     user (or insert via the service), close it through `erasure_ledger_service.block_request(db, entry, reason="last_super_admin")`,
     GET → `request is None`, `last_outcome == {"status":"blocked","reason":"last_super_admin","ledger_uuid":..., "at": ...}`.
  2. `test_a_user_cancelled_request_is_not_reported_as_blocked`: cancel via DELETE → `last_outcome is None`.
  3. `test_a_newer_request_hides_an_older_block`: block, then POST a new request → `request` set, `last_outcome is None`.
  4. Extend `backend/tests/unit/test_self_erasure_last_super_admin.py` (from `4b5f9dd9a`): assert the
     blocked row has `deferred_reason == "last_super_admin"`.
  Red first: same `git archive` trick with the backend tree, copying the new test files in.

### 2b. Frontend

**Files:** `frontend/src/lib/api/selfErasure.ts`, `frontend/src/components/settings/AccountErasureCard.svelte`,
`frontend/src/components/ErasureRequestBanner.svelte`, `frontend/src/stores/selfErasure.ts`.

- `selfErasure.ts`: add `export interface SelfErasureOutcome { status: 'blocked'; reason: 'last_super_admin'; ledger_uuid: string; at: string | null; }`
  and `last_outcome?: SelfErasureOutcome | null` on `SelfErasureStatus` (optional: an older backend omits it).
  Add to `ERROR_KEYS`: `not_cancellable: 'settings.accountErasure.errorNotCancellable'`, `not_found: 'settings.accountErasure.errorNotFound'`.
  (Today both fall to `errorGeneric`: verified.)
- `AccountErasureCard.svelte`: when `status.request === null && status.last_outcome?.reason === 'last_super_admin'`,
  render above the request form a `<p class="notice" role="status" data-testid="erasure-blocked">` with
  `settings.accountErasure.blockedLastSuperAdmin`. The form stays (they may request again after
  appointing another super admin). Use existing warning colours from the card, with a dark-mode rule.
  Cancel's catch already reloads (`void load()`), so `errorNotFound` reads correctly.
- Time display: the card's `formatWhen` already uses `dateStyle:'long', timeStyle:'short'` (verified). The banner
  uses date only (`ErasureRequestBanner.svelte` ≈ line 16): add `timeStyle: 'short'`. Do NOT add a ticking
  countdown; date + time is enough ("trivial" scope).
- Banner: (1) re-read status on `visibilitychange` to visible (add the listener in `onMount`, remove it
  in the returned cleanup), so a block that happens while the tab is open shows up when the person returns;
  (2) export a store `lastErasureOutcome` from `stores/selfErasure.ts`, set it in both the banner and the card's `load()`;
  (3) when there is no request and `lastErasureOutcome` exists and `at` is within the last 7 days, show
  `banner.accountErasure.blocked` with the existing `banner.accountErasure.manage` button. After 7 days the
  card still shows the notice; the banner does not.
- Tests:
  - `AccountErasureCard.test.ts`: `'tells the person their request was blocked as the last super administrator'`
    (status with `request:null, last_outcome:{...}` → `erasure-blocked` text present and the form present);
    `'says the request can no longer be cancelled on 409 not_cancellable'`; `'says the request no longer exists on 404 not_found and reloads'`.
  - `ErasureRequestBanner.test.ts`: `'shows the time as well as the date'` (assert the formatted string equals
    `Intl.DateTimeFormat('en',{dateStyle:'long',timeStyle:'short'})` of the fixture); `'shows a blocked notice for a recent block'`;
    `'does not show the blocked notice after seven days'` (use `vi.setSystemTime`); `'re-reads the status when the tab becomes visible'`.
  - `lib/api/selfErasure.test.ts`: the two new error-key mappings.

---

## 3. PurgeConfirmDialog / purge status: report held and protected files

### CONTRACT-TO-VERIFY (read before coding item 3 and the §1 `follow()` step)

At plan time `fix/audit3-scope-purge-holds` had **no commit of its own** (its tip was `4b5f9dd9a`, the
same as `fix/audit-v061-holds-and-last-admin`) and its worktree
(`/mnt/nvm/repos/transcribe-app/.claude/worktrees/agent-aac08cc3ee8878ac7`) had no change to
`backend/app/schemas/privacy.py`. So the field names below are PLACEHOLDERS. Before coding:

```bash
git fetch origin 2>/dev/null; git log --oneline d267f879a..fix/audit3-scope-purge-holds
git show fix/audit3-scope-purge-holds:backend/app/schemas/privacy.py | sed -n '/class PurgeStatus/,/^class /p'
git diff d267f879a fix/audit3-scope-purge-holds -- backend/app/tasks/privacy_purge_task.py backend/app/services/privacy/
```

Pin, in your commit body, the exact answers to:
1. The name and type of the skipped count on `PurgeStatus` (placeholder: `skipped: {held: int, quarantined: int} | null`).
2. How the "everything was protected" abort shows: `status` value (`failed`? `skipped`?) and the reason
   field name and value (placeholder: `reason: "files_protected"`).
3. Whether `residual` is measured on the narrowed scope (held files excluded) or the whole scope.
4. Whether user/org/instance scopes report skipped counts too, or only `files` scopes.

If that branch is still empty when you get here, finish everything else, push, and report item 3 as
blocked on the contract. Do not guess the field names.

### Change (with placeholders, rename to the real fields)

- `frontend/src/lib/api/privacy.ts` `PurgeStatus` (line 87): add `skipped?: { held: number; quarantined: number } | null;`
  and `reason?: string | null;` (optional, because an older backend omits them).
- `frontend/src/lib/settings/privacyPolicy.ts` (where `purgeVerification` lives): add
  `export function purgeProtectedTotal(s: PurgeStatus): number` (sum of skipped, 0 if absent) and make
  `purgeVerification` return a new state `'verified_except_protected'` when it would return `'verified'`
  and `purgeProtectedTotal > 0`. Copy is chosen through literal-key maps (privacy `CLAUDE.md` rule).
- `frontend/src/components/privacy/PurgeConfirmDialog.svelte` (result block ≈ lines 196-236):
  - if the abort reason is the protected one: show `privacy.purge.allProtected` INSTEAD of `privacy.purge.failed` / `skipped`.
  - when `finished && purgeProtectedTotal(status) > 0`: add `<li data-testid="purge-protected">` with
    `privacy.purge.protectedSkipped` `{count}` to the results list.
  - verification `'verified_except_protected'` → `privacy.purge.residualOkExceptProtected`, never `residualOk`.
- Tests:
  - `PurgeConfirmDialog.test.ts`: `'lists recordings kept for a legal hold or takedown'`;
    `'never says nothing remains when recordings were kept'` (assert `residualOk` text ABSENT and
    `residualOkExceptProtected` present); `'says nothing was deleted when every recording was protected'`.
  - `lib/settings/privacyPolicy.test.ts`: `purgeProtectedTotal` with absent / null / mixed values and the new verification state.
- Edge: `skipped` present with zeros → behave exactly as today. Residual non-zero AND skipped → keep the
  existing `residualFound` error and still list the kept count.

---

## 4. Support-grant refusals and other reasons that are thrown away

All via the existing `getErrorMessage(error, fallback)` (`frontend/src/lib/utils/apiError.ts:69`), which
already translates holds and legal-gate codes and shows `detail.message` for coded errors. Verified call sites:

| Site | Today | Change |
|---|---|---|
| `routes/files/[id]/+page.svelte` `on:regenerate` catch (≈ 2684-2688) | fixed `fileDetail.failedToStartSummaryReprocess` | `toastStore.error(getErrorMessage(error, $t('fileDetail.failedToStartSummaryReprocess')), 5000)` AND restore `file.has_summary` / `file.summary_opensearch_id` to the values saved before they were cleared (they are cleared at ≈ 2676; save them in locals first) |
| same file `on:regenerateWithPrompt` catch (≈ 2708-2712) | same | same, same restore |
| same file `triggerRedaction` catch (≈ 540-548) | only string detail | `toastStore.error(getErrorMessage(err, $t('settings.contentRedaction.redactTriggerFailed')))` (keeps the 409 string, adds object details) |
| `routes/+page.svelte` `bulkSummarize` catch (≈ 853-856) | count only | `toastStore.error(getErrorMessage(err, $t('gallery.bulk.summarizeFailed', { count: completed.length })))` |
| `routes/+page.svelte` bulk `identify_speakers` catch (≈ 1003-1006) | count only | same pattern with `speakerIdFailed` |
| `routes/+page.svelte` `bulkRedact` catch | count only | same pattern (read the handler; keep its key) |
| `components/settings/AutoLabelSettings.svelte` `triggerRetroactiveApply` catch (≈ 128-131) | fixed `autoLabel.retroactiveFailed` | `getErrorMessage(err, $t('autoLabel.retroactiveFailed'))` |

`getErrorMessage` falls back to `e.message` ("Request failed with status code 500") before the
fallback for a bare error (`apiError.ts:104`), so a 500 with no detail shows "Request failed with status
code 500" instead of the translated fallback. Every other `getErrorMessage` caller already accepts this, so
keep it; do not add a per-site helper.

No new i18n keys. Tests (vitest, mock axios like the files already do):
- `AutoLabelSettings.test.ts`: `'shows the server reason when retroactive auto-label is refused'`.
- Route components are heavy. If `routes/files/[id]/+page.svelte` has no test harness, do not build one.
  Pull the regenerate catch body into a small exported function in a new
  `frontend/src/lib/summary/regenerateFailure.ts` (`restoreAfterFailedRegenerate(file, saved)` + the
  message) and test that, or cover it in the item 7 bulk helper test below. Do not test what you cannot reach.

---

## 5. Not in scope (verified): delete speaker/summary/topic hold 409s have no frontend caller.

## 6. Not in scope: quarantine media revocation is already handled.

---

## 7. "LLM configuration unusable" is a different message from "none configured"

**What the backend does today (verified):**
- `LLMService.create_from_settings` (`backend/app/services/llm_service.py` ≈ 2321-2343) CATCHES
  `LLMConfigurationError` and returns `None`, so "unusable" looks exactly like "none configured" everywhere
  it is used: the chat default path (`chat/messages.py:224-232` → 400 "No LLM is configured..."), `/llm/status`,
  and the bulk `summarize` / `identify_speakers` (`files/management.py` ≈ 868-884 and 930-947 → `error="LLM_NOT_AVAILABLE"`).
- Only the explicit `config_id` path (`create_from_config_id`) raises, giving a 400 with a string detail.
- So the backend does NOT tell the cases apart in a machine-readable way. Minimal, neutral exposure (fixed
  reason code, no secrets: the enum values in `LLMConfigFailure` are already safe to show):

**Backend (same unit as 2a):**
- `backend/app/api/endpoints/chat/messages.py::_resolve_chat_llm` (line 600): call
  `LLMService.create_from_user_settings(user_id=user_id)` instead of `create_from_settings` (same result
  except that it raises; read both to confirm, and confirm `create_from_user_settings` returns `None` when
  there is no config), and replace the `except` with
  `raise coded_error(400, "llm_config_unusable", str(unusable)) from None` (import
  `coded_error` from `app.services.support_access_service`). The two "No LLM is configured" string details
  stay unchanged (tests `backend/tests/test_chat_endpoints.py:810, 928` pin them).
- `backend/app/api/endpoints/files/management.py` both LLM checks: same swap; add
  `except LLMConfigurationError as unusable:` BEFORE the broad `except Exception`, returning
  `BulkActionResult(file_uuid=..., success=False, message=str(unusable), error="LLM_CONFIG_UNUSABLE")`.
- Tests: in `backend/tests/unit/test_llm_unusable_config_no_system_fallback.py` (or a new
  `backend/tests/api/test_llm_unusable_config_detail.py`): a user whose active config points at a deleted
  row → `POST` a chat message (or `/context/estimate`) returns 400 with `detail.code == "llm_config_unusable"`;
  bulk `identify_speakers` and `summarize` per-file `error == "LLM_CONFIG_UNUSABLE"`. Read that existing test file
  first for how it builds an unusable config. Run `backend/tests/unit/test_chat_sse_contract.py` too: it
  lists chat error codes and may need the new code.

**Frontend:**
- `frontend/src/lib/types/chat.ts:498` `ChatErrorCode`: add `'llm_config_unusable'`.
- `frontend/src/lib/utils/chatErrors.ts`: add `llm_config_unusable: true`.
- `frontend/src/lib/i18n/keyMaps.ts:70` area: add `llm_config_unusable: 'chat.errors.llm_config_unusable'`.
- `frontend/src/lib/api/chatStream.ts::errorFromResponse` (≈ 171-194): when `body.detail` is an object, take
  `message = String(detail.message ?? ...)` and, if `detail.code === 'llm_config_unusable'`, return
  `{type:'error', code:'llm_config_unusable', message}` before the `/llm/i` test. (Today an object detail would become
  `"[object Object]"`.)
- Wherever the chat UI shows the "Open AI settings" button for `llm_unconfigured`
  (`rg -n "llm_unconfigured|openLlmSettings" frontend/src/components/chat`), show it for `llm_config_unusable` too.
- `routes/+page.svelte` bulk handlers (edit together with item 4): in `bulkSummarize` check
  `LLM_CONFIG_UNUSABLE` first → `gallery.bulk.llmConfigUnusable`, then the existing `LLM_NOT_AVAILABLE`. In the bulk
  `identify_speakers` handler add the same two checks (today it only counts). To keep it testable and not
  duplicate, add `frontend/src/lib/utils/bulkLlmOutcome.ts`:
  `export function bulkLlmFailureKey(results: {success:boolean; error?:string|null}[]): string | null` returning
  `'gallery.bulk.llmConfigUnusable' | 'gallery.bulk.llmNotConfigured' | null`, and call it from both handlers.
- `components/SummaryActions.svelte:89-90`: next to the `llm.featuresUnavailable` hint, add a small link button
  `chat.setup.openSettings` ("Open AI settings", exists in all 12 locales) calling `settingsModalStore.open('llm-provider')`.
  `/llm/status` cannot tell the two cases apart, so the hint wording stays; the link is the fix.
- New keys: `chat.errors.llm_config_unusable`, `gallery.bulk.llmConfigUnusable` (§11 L, M).
- Tests: `chatStream.test.ts` `'maps a coded llm_config_unusable 400 to its own code'` and
  `'still maps the plain "No LLM is configured" 400 to llm_unconfigured'`; `bulkLlmOutcome.test.ts` (unusable wins over
  not-available; null when all succeed; null for unrelated errors); `SummaryActions.test.ts`
  `'offers a link to AI settings when AI features are unavailable'`; the existing chat-errors i18n test must still pass (it
  checks every `ChatErrorCode` has a key in all locales).

---

## 8. WebSocket: stop reconnecting on auth/origin close codes

**Verified:** `backend/app/api/websockets.py` closes with 4403 (origin, line 426), 4001 (auth required/timeout,
457/467), 4003 (invalid token / auth error, 464/475; also revoked, inactive, pending approval), 4002 (invalid message, 471).
`frontend/src/stores/websocket.ts` `socket.onclose` (≈ 351-366) reconnects for every code except 1000/1001.

**Change in `websocket.ts`:**
```ts
const TERMINAL_CLOSE_CODES = new Set([4001, 4002, 4003, 4403]);
// in onclose, before shouldReconnect:
if (TERMINAL_CLOSE_CODES.has(event.code)) {
  s.status = 'error';               // check the ConnectionStatus union (line ~152); use the existing error value
  if (event.code !== 4403 && !terminalProbeDone) {
    terminalProbeDone = true;
    void probeSessionAfterRefusal();
  }
  return s;
}
```
- `probeSessionAfterRefusal()`: `axiosInstance.get('/auth/me')` once, swallow errors. Its 401/403 then go through
  the existing axios interceptors (session-expired → login; account lifecycle → blocking/pending-approval screen),
  which is "route via the existing handler". If it answers 200 (the token was refreshed meanwhile), call
  `tryReconnect()` ONE time. `terminalProbeDone` is reset in `onopen` and in `connect()` called after login, so a new
  session gets a fresh chance. Never loop.
- `/auth/me` not `/auth/session`: `/auth/session` answers 200 for anonymous callers and would route nowhere.
- Tests in `frontend/src/stores/websocket.test.ts` (use the existing `FakeWebSocket.simulateServerClose`):
  `'does NOT reconnect after 4003 and probes the session once'` (advance timers 60 s, assert one socket, one `/auth/me`
  call); same for 4001 and 4002 (`it.each`); `'does not probe the session after 4403'`; `'reconnects once if the probe
  succeeds, and not again on a second 4003'`. Mock `$lib/axios` the way the file already does.

---

## 9. Admin UI for storage-keys / erasure receipts: NOT included

Decision: **no UI in 0.6.1.** Reasons: (1) these are operator/forensic routes
(`GET /admin/data-integrity/storage/keys`, `GET /admin/gdpr/erasures`, erasure receipts, ignored-indices) used
rarely, from scripts or support; (2) a correct viewer needs paging, filters, a receipt view and 12-locale copy:
that is a feature, not a follow-through, and it would double this unit; (3) nothing in the UI is wrong without it,
it is just missing. Do instead: add one line to `CHANGELOG.md` under `## [Unreleased]` → `### Changed` (or
the existing 0.6.1 section): "The storage-key uniqueness check (`GET /api/admin/data-integrity/storage/keys`) and the
erasure ledger and receipts (`/api/admin/gdpr/erasures`, `/api/admin/gdpr/erasure-receipt/...`) are API-only in this
release; there is no admin screen for them yet." Optionally file a follow-up issue (labels `enhancement`, `frontend`,
`security`, add to Roadmap project per root `CLAUDE.md`).

## 10. configService.ts host matching: never more permissive than the backend

**Verified backend rule** (`backend/app/services/protected_media_plugins/mediacms.py:36-52`, `_host_identity`):
`urlparse("//" + host.strip())`, `None` if no hostname or `"@" in host`; else `(hostname.lower(), port or default)`.
Measured with Python's `urlparse`: `user@m.example` → None; `m.example\@evil` → None; `m.example\evil` →
`('m.example\\evil', 443)` (never equal to a real host); `m%2eexample` → kept literally; `bücher.de` → kept as
unicode; `[::1]:8080` → `('::1', 8080)`; `m.example:0` → port default. WHATWG `URL` (frontend) would instead
normalise `\` → `/`, decode `%2e`, punycode IDN, and drop userinfo, so the frontend matches things the backend refuses.

**Change** in `frontend/src/lib/services/configService.ts::hostIdentity` (≈ line 79), before `new URL`:
```ts
// The backend compares the raw string; WHATWG URL normalises these, so the UI would match a host the server refuses.
if (/[@\\%]/.test(host) || /[^\x21-\x7e]/.test(host.trim())) return null;
```
(The second test rejects non-ASCII and inner whitespace/control characters after trimming.)
Apply it to the configured host only? No: apply it inside `hostIdentity`, which is used for both; a real `URL.host` never
contains these characters, so the candidate side is unaffected.

**Shared vectors:** new file `backend/tests/fixtures/mediacms_host_vectors.json`:
```json
{ "_comment": "Shared by backend/tests/unit/test_mediacms_host_matching.py and frontend/src/lib/services/configService.test.ts. expected = both sides agree.",
  "vectors": [
    {"configured": "m.example.com", "candidate": "m.example.com", "scheme": "https", "expected": true},
    {"configured": "M.Example.com", "candidate": "m.example.com", "scheme": "https", "expected": true},
    {"configured": "m.example.com:443", "candidate": "m.example.com", "scheme": "https", "expected": true},
    {"configured": "m.example.com:8443", "candidate": "m.example.com", "scheme": "https", "expected": false},
    {"configured": " m.example.com ", "candidate": "m.example.com", "scheme": "https", "expected": true},
    {"configured": "user@m.example.com", "candidate": "m.example.com", "scheme": "https", "expected": false},
    {"configured": "m.example.com\\@evil.example", "candidate": "m.example.com", "scheme": "https", "expected": false},
    {"configured": "m.example.com\\evil", "candidate": "m.example.com", "scheme": "https", "expected": false},
    {"configured": "m%2eexample.com", "candidate": "m.example.com", "scheme": "https", "expected": false},
    {"configured": "bücher.example", "candidate": "xn--bcher-kva.example", "scheme": "https", "expected": false}
  ] }
```
(Copy the rows already in the backend parametrize list (`test_mediacms_host_matching.py:19-35`) into this file too,
and make that test read the file instead of an inline list, so there is one list.) Candidates are always what a
real URL's `host` looks like (ASCII, punycoded). Backend test: `@pytest.mark.parametrize` over the JSON in
`test_one_identity_rule`. Frontend test (`configService.test.ts`): read it with
`readFileSync(new URL('../../../../backend/tests/fixtures/mediacms_host_vectors.json', import.meta.url))`, and for
each vector load `{protected_media_configs:[{hosts:[configured], ...}]}` through the mocked axios and assert
`getAuthConfigForUrl(new URL(`${scheme}://${candidate}`)) !== null` equals `expected`. Red first: the userinfo,
backslash, `%` and IDN rows fail on the old frontend code. If vitest refuses a path outside `frontend/` (check
`vite.config`/`vitest` `server.fs`), read with `fs` instead of `import`, which is not affected by that setting.

---

## 11. i18n table: 13 new keys (paste into all 12 files)

Keys: A `file.voiceprintConsent.heldRefusal`, B `file.voiceprintConsent.unavailable`, C `file.voiceprintConsent.purgePending`,
D `file.voiceprintConsent.purgeProtected`, E `settings.accountErasure.blockedLastSuperAdmin`,
F `settings.accountErasure.errorNotCancellable`, G `settings.accountErasure.errorNotFound`, H `banner.accountErasure.blocked`,
I `privacy.purge.protectedSkipped` (`{{count}}`), J `privacy.purge.residualOkExceptProtected`, K `privacy.purge.allProtected`,
L `chat.errors.llm_config_unusable`, M `gallery.bulk.llmConfigUnusable`.
Keys D, I, J, K are used only once item 3's contract is pinned; if item 3 is blocked, leave them out (the parity
gate only checks keys that exist; an unused key is dead copy).

### en
- A: This recording is under a legal hold, so its voice identification setting cannot be changed.
- B: This recording is no longer available, so this setting cannot be changed.
- C: Voice identification is now off for this recording, but deleting its existing voiceprints could not be started. Try again later.
- D: Voice identification is off, but existing voiceprints were kept because this recording is under a legal hold or has been taken down.
- E: Your account erasure was not carried out because you are the last active super administrator. Make another account a super administrator, then request erasure again.
- F: This request can no longer be cancelled.
- G: This request no longer exists. The status below has been refreshed.
- H: Your account erasure was not carried out.
- I: Kept because of a legal hold or takedown: {{count}} recording(s)
- J: Checked afterwards: nothing from this deletion remains, except the recordings kept above.
- K: Nothing was deleted: every recording in scope is under a legal hold or has been taken down.
- L: Your selected AI provider configuration can't be used (for example, its credentials or region are missing, or it was deleted). Check it in Settings.
- M: Your AI provider configuration can't be used. Check it in Settings.

### de
- A: Diese Aufnahme unterliegt einer rechtlichen Sperre (Legal Hold), daher kann die Einstellung zur Stimmerkennung nicht geändert werden.
- B: Diese Aufnahme ist nicht mehr verfügbar, daher kann diese Einstellung nicht geändert werden.
- C: Die Stimmerkennung ist für diese Aufnahme jetzt deaktiviert, aber das Löschen der vorhandenen Stimmabdrücke konnte nicht gestartet werden. Versuchen Sie es später erneut.
- D: Die Stimmerkennung ist deaktiviert, aber vorhandene Stimmabdrücke wurden beibehalten, da diese Aufnahme einer rechtlichen Sperre unterliegt oder entfernt wurde.
- E: Die Löschung Ihres Kontos wurde nicht durchgeführt, da Sie der letzte aktive Super-Administrator sind. Ernennen Sie ein anderes Konto zum Super-Administrator und beantragen Sie die Löschung dann erneut.
- F: Dieser Antrag kann nicht mehr storniert werden.
- G: Dieser Antrag existiert nicht mehr. Der Status unten wurde aktualisiert.
- H: Die Löschung Ihres Kontos wurde nicht durchgeführt.
- I: Wegen rechtlicher Sperre oder Entfernung beibehalten: {{count}} Aufnahme(n)
- J: Anschließend geprüft: Von dieser Löschung ist nichts übrig, außer den oben genannten beibehaltenen Aufnahmen.
- K: Es wurde nichts gelöscht: Jede betroffene Aufnahme unterliegt einer rechtlichen Sperre oder wurde entfernt.
- L: Die ausgewählte KI-Anbieterkonfiguration kann nicht verwendet werden (z. B. fehlen Zugangsdaten oder Region, oder sie wurde gelöscht). Prüfen Sie sie in den Einstellungen.
- M: Ihre KI-Anbieterkonfiguration kann nicht verwendet werden. Prüfen Sie sie in den Einstellungen.

### es
- A: Esta grabación está sujeta a una retención legal, por lo que no se puede cambiar su configuración de identificación de voz.
- B: Esta grabación ya no está disponible, por lo que no se puede cambiar esta configuración.
- C: La identificación de voz ya está desactivada para esta grabación, pero no se pudo iniciar la eliminación de sus huellas de voz existentes. Inténtelo de nuevo más tarde.
- D: La identificación de voz está desactivada, pero se conservaron las huellas de voz existentes porque esta grabación está sujeta a una retención legal o ha sido retirada.
- E: No se llevó a cabo el borrado de su cuenta porque usted es el último superadministrador activo. Convierta otra cuenta en superadministrador y vuelva a solicitar el borrado.
- F: Esta solicitud ya no se puede cancelar.
- G: Esta solicitud ya no existe. Se ha actualizado el estado que aparece abajo.
- H: No se llevó a cabo el borrado de su cuenta.
- I: Conservadas por retención legal o retirada: {{count}} grabación(es)
- J: Comprobado después: no queda nada de esta eliminación, salvo las grabaciones conservadas indicadas arriba.
- K: No se eliminó nada: todas las grabaciones del ámbito están sujetas a una retención legal o han sido retiradas.
- L: No se puede usar la configuración del proveedor de IA seleccionada (por ejemplo, faltan las credenciales o la región, o se eliminó). Revísela en Ajustes.
- M: No se puede usar su configuración del proveedor de IA. Revísela en Ajustes.

### fr
- A: Cet enregistrement fait l'objet d'une conservation légale : son réglage d'identification vocale ne peut pas être modifié.
- B: Cet enregistrement n'est plus disponible : ce réglage ne peut pas être modifié.
- C: L'identification vocale est désormais désactivée pour cet enregistrement, mais la suppression de ses empreintes vocales existantes n'a pas pu démarrer. Réessayez plus tard.
- D: L'identification vocale est désactivée, mais les empreintes vocales existantes ont été conservées, car cet enregistrement fait l'objet d'une conservation légale ou a été retiré.
- E: L'effacement de votre compte n'a pas été effectué, car vous êtes le dernier super-administrateur actif. Désignez un autre compte comme super-administrateur, puis demandez à nouveau l'effacement.
- F: Cette demande ne peut plus être annulée.
- G: Cette demande n'existe plus. L'état ci-dessous a été actualisé.
- H: L'effacement de votre compte n'a pas été effectué.
- I: Conservés en raison d'une conservation légale ou d'un retrait : {{count}} enregistrement(s)
- J: Vérifié ensuite : rien de cette suppression ne subsiste, hormis les enregistrements conservés ci-dessus.
- K: Rien n'a été supprimé : chaque enregistrement concerné fait l'objet d'une conservation légale ou a été retiré.
- L: La configuration du fournisseur d'IA sélectionnée est inutilisable (par exemple, identifiants ou région manquants, ou configuration supprimée). Vérifiez-la dans les paramètres.
- M: Votre configuration du fournisseur d'IA est inutilisable. Vérifiez-la dans les paramètres.

### it
- A: Questa registrazione è soggetta a un blocco legale, quindi l'impostazione di identificazione vocale non può essere modificata.
- B: Questa registrazione non è più disponibile, quindi questa impostazione non può essere modificata.
- C: L'identificazione vocale è ora disattivata per questa registrazione, ma non è stato possibile avviare l'eliminazione delle impronte vocali esistenti. Riprova più tardi.
- D: L'identificazione vocale è disattivata, ma le impronte vocali esistenti sono state conservate perché questa registrazione è soggetta a un blocco legale o è stata rimossa.
- E: La cancellazione del tuo account non è stata eseguita perché sei l'ultimo super amministratore attivo. Rendi super amministratore un altro account, poi richiedi di nuovo la cancellazione.
- F: Questa richiesta non può più essere annullata.
- G: Questa richiesta non esiste più. Lo stato qui sotto è stato aggiornato.
- H: La cancellazione del tuo account non è stata eseguita.
- I: Conservate per blocco legale o rimozione: {{count}} registrazione/i
- J: Verificato in seguito: non rimane nulla di questa eliminazione, tranne le registrazioni conservate indicate sopra.
- K: Non è stato eliminato nulla: ogni registrazione interessata è soggetta a un blocco legale o è stata rimossa.
- L: La configurazione del provider IA selezionata non può essere usata (ad esempio mancano le credenziali o la regione, oppure è stata eliminata). Controllala nelle Impostazioni.
- M: La configurazione del provider IA non può essere usata. Controllala nelle Impostazioni.

### ja
- A: この録音はリーガルホールドの対象のため、音声識別の設定を変更できません。
- B: この録音は利用できなくなったため、この設定を変更できません。
- C: この録音の音声識別はオフになりましたが、既存の声紋の削除を開始できませんでした。後でもう一度お試しください。
- D: 音声識別はオフですが、この録音はリーガルホールドの対象か削除措置中のため、既存の声紋は保持されました。
- E: あなたが最後の有効なスーパー管理者であるため、アカウントの消去は実行されませんでした。別のアカウントをスーパー管理者にしてから、もう一度消去を申請してください。
- F: このリクエストはもう取り消せません。
- G: このリクエストは存在しません。下の状態を更新しました。
- H: アカウントの消去は実行されませんでした。
- I: リーガルホールドまたは削除措置のため保持: {{count}} 件の録音
- J: 事後確認: 上記の保持された録音を除き、この削除の対象は何も残っていません。
- K: 何も削除されませんでした。対象のすべての録音がリーガルホールドの対象か削除措置中です。
- L: 選択した AI プロバイダー設定は使用できません（認証情報やリージョンがない、または削除されたなど）。設定で確認してください。
- M: AI プロバイダー設定を使用できません。設定で確認してください。

### ko
- A: 이 녹음은 법적 보존 조치 대상이므로 음성 식별 설정을 변경할 수 없습니다.
- B: 이 녹음은 더 이상 사용할 수 없으므로 이 설정을 변경할 수 없습니다.
- C: 이 녹음의 음성 식별이 꺼졌지만 기존 성문 삭제를 시작할 수 없습니다. 나중에 다시 시도하세요.
- D: 음성 식별은 꺼져 있지만, 이 녹음이 법적 보존 조치 대상이거나 게시 중단되어 기존 성문은 유지되었습니다.
- E: 귀하가 마지막 활성 최고 관리자이므로 계정 삭제가 수행되지 않았습니다. 다른 계정을 최고 관리자로 지정한 후 다시 삭제를 요청하세요.
- F: 이 요청은 더 이상 취소할 수 없습니다.
- G: 이 요청은 더 이상 존재하지 않습니다. 아래 상태를 새로 고쳤습니다.
- H: 계정 삭제가 수행되지 않았습니다.
- I: 법적 보존 조치 또는 게시 중단으로 유지됨: 녹음 {{count}}개
- J: 사후 확인: 위에 표시된 유지된 녹음을 제외하고 이 삭제 대상은 남아 있지 않습니다.
- K: 삭제된 항목이 없습니다. 범위 내 모든 녹음이 법적 보존 조치 대상이거나 게시 중단되었습니다.
- L: 선택한 AI 공급자 구성을 사용할 수 없습니다(예: 자격 증명이나 리전이 없거나 삭제됨). 설정에서 확인하세요.
- M: AI 공급자 구성을 사용할 수 없습니다. 설정에서 확인하세요.

### nl
- A: Deze opname valt onder een juridische bewaarplicht (legal hold), dus de instelling voor stemherkenning kan niet worden gewijzigd.
- B: Deze opname is niet meer beschikbaar, dus deze instelling kan niet worden gewijzigd.
- C: Stemherkenning staat nu uit voor deze opname, maar het verwijderen van de bestaande stemafdrukken kon niet worden gestart. Probeer het later opnieuw.
- D: Stemherkenning staat uit, maar bestaande stemafdrukken zijn bewaard omdat deze opname onder een juridische bewaarplicht valt of is verwijderd.
- E: Het wissen van uw account is niet uitgevoerd, omdat u de laatste actieve superbeheerder bent. Maak een ander account superbeheerder en vraag het wissen daarna opnieuw aan.
- F: Dit verzoek kan niet meer worden geannuleerd.
- G: Dit verzoek bestaat niet meer. De status hieronder is vernieuwd.
- H: Het wissen van uw account is niet uitgevoerd.
- I: Bewaard vanwege juridische bewaarplicht of verwijdering: {{count}} opname(n)
- J: Achteraf gecontroleerd: van deze verwijdering is niets over, behalve de hierboven bewaarde opnamen.
- K: Er is niets verwijderd: elke betrokken opname valt onder een juridische bewaarplicht of is verwijderd.
- L: De geselecteerde AI-providerconfiguratie kan niet worden gebruikt (bijvoorbeeld omdat inloggegevens of regio ontbreken, of de configuratie is verwijderd). Controleer deze in Instellingen.
- M: Uw AI-providerconfiguratie kan niet worden gebruikt. Controleer deze in Instellingen.

### pt
- A: Esta gravação está sob retenção legal, por isso a configuração de identificação de voz não pode ser alterada.
- B: Esta gravação não está mais disponível, por isso esta configuração não pode ser alterada.
- C: A identificação de voz agora está desativada para esta gravação, mas não foi possível iniciar a exclusão das impressões de voz existentes. Tente novamente mais tarde.
- D: A identificação de voz está desativada, mas as impressões de voz existentes foram mantidas porque esta gravação está sob retenção legal ou foi removida.
- E: A exclusão da sua conta não foi realizada porque você é o último superadministrador ativo. Torne outra conta superadministradora e solicite a exclusão novamente.
- F: Esta solicitação não pode mais ser cancelada.
- G: Esta solicitação não existe mais. O status abaixo foi atualizado.
- H: A exclusão da sua conta não foi realizada.
- I: Mantidas por retenção legal ou remoção: {{count}} gravação(ões)
- J: Verificado depois: nada desta exclusão permanece, exceto as gravações mantidas acima.
- K: Nada foi excluído: todas as gravações no escopo estão sob retenção legal ou foram removidas.
- L: A configuração do provedor de IA selecionada não pode ser usada (por exemplo, faltam credenciais ou região, ou foi excluída). Verifique-a nas Configurações.
- M: Sua configuração do provedor de IA não pode ser usada. Verifique-a nas Configurações.

### ru
- A: Эта запись находится под юридическим удержанием, поэтому настройку идентификации голоса изменить нельзя.
- B: Эта запись больше недоступна, поэтому изменить эту настройку нельзя.
- C: Идентификация голоса для этой записи отключена, но удаление существующих голосовых отпечатков запустить не удалось. Повторите попытку позже.
- D: Идентификация голоса отключена, но существующие голосовые отпечатки сохранены, так как запись находится под юридическим удержанием или была изъята.
- E: Удаление вашей учётной записи не выполнено, так как вы последний активный суперадминистратор. Назначьте суперадминистратором другую учётную запись и запросите удаление снова.
- F: Этот запрос больше нельзя отменить.
- G: Этот запрос больше не существует. Статус ниже обновлён.
- H: Удаление вашей учётной записи не выполнено.
- I: Сохранено из-за юридического удержания или изъятия, записей: {{count}}
- J: Проверено после: от этого удаления ничего не осталось, кроме сохранённых записей, указанных выше.
- K: Ничего не удалено: каждая запись в области действия находится под юридическим удержанием или изъята.
- L: Выбранную конфигурацию поставщика ИИ нельзя использовать (например, отсутствуют учётные данные или регион, либо она удалена). Проверьте её в настройках.
- M: Конфигурацию поставщика ИИ нельзя использовать. Проверьте её в настройках.

### zh
- A: 此录音处于法律保留状态，因此无法更改其语音识别设置。
- B: 此录音已不可用，因此无法更改此设置。
- C: 此录音的语音识别现已关闭，但无法开始删除其现有声纹。请稍后重试。
- D: 语音识别已关闭，但由于此录音处于法律保留状态或已被下架，现有声纹已被保留。
- E: 由于您是最后一位活跃的超级管理员，您的账户删除未执行。请将另一个账户设为超级管理员，然后重新申请删除。
- F: 此请求已无法取消。
- G: 此请求已不存在。下方状态已刷新。
- H: 您的账户删除未执行。
- I: 因法律保留或下架而保留：{{count}} 个录音
- J: 事后检查：除上方保留的录音外，此次删除的内容均未残留。
- K: 未删除任何内容：范围内的每个录音都处于法律保留状态或已被下架。
- L: 所选的 AI 提供商配置无法使用（例如缺少凭据或区域，或已被删除）。请在设置中检查。
- M: 您的 AI 提供商配置无法使用。请在设置中检查。

### ar (RTL)
- A: هذا التسجيل خاضع لتجميد قانوني، لذا لا يمكن تغيير إعداد التعرّف على الصوت الخاص به.
- B: لم يعد هذا التسجيل متاحًا، لذا لا يمكن تغيير هذا الإعداد.
- C: تم إيقاف التعرّف على الصوت لهذا التسجيل، لكن تعذّر بدء حذف بصمات الصوت الموجودة. حاول مرة أخرى لاحقًا.
- D: تم إيقاف التعرّف على الصوت، لكن تم الاحتفاظ ببصمات الصوت الموجودة لأن هذا التسجيل خاضع لتجميد قانوني أو تمت إزالته.
- E: لم يتم تنفيذ محو حسابك لأنك آخر مسؤول أعلى نشط. اجعل حسابًا آخر مسؤولًا أعلى، ثم اطلب المحو مرة أخرى.
- F: لم يعد بالإمكان إلغاء هذا الطلب.
- G: لم يعد هذا الطلب موجودًا. تم تحديث الحالة أدناه.
- H: لم يتم تنفيذ محو حسابك.
- I: تم الاحتفاظ بها بسبب تجميد قانوني أو إزالة: {{count}} تسجيل
- J: تم التحقق لاحقًا: لم يتبقَّ شيء من عملية الحذف هذه، باستثناء التسجيلات المحتفَظ بها أعلاه.
- K: لم يُحذف أي شيء: كل تسجيل ضمن النطاق خاضع لتجميد قانوني أو تمت إزالته.
- L: لا يمكن استخدام إعداد مزوّد الذكاء الاصطناعي المحدد (مثلًا: بيانات الاعتماد أو المنطقة مفقودة، أو تم حذفه). تحقّق منه في الإعدادات.
- M: لا يمكن استخدام إعداد مزوّد الذكاء الاصطناعي لديك. تحقّق منه في الإعدادات.

When pasting into JSON, escape `"` (none above) and keep the `'` as is. A good way to avoid typos in 12 files is
a throwaway Node script in `mktemp -d` that inserts the keys after a named sibling key, run once, then deleted (not committed).

---

## Sequencing and commits

1. Tests for items 10, 8, 2b, 1 (minus `follow()`), 4, 7 → see each red in an `ot-redcheck.` tree.
2. Backend 2a + 7 backend part + their tests (red first). One commit: `fix(privacy): report a self-erasure the system blocked, and name an unusable LLM configuration`.
3. Frontend items 10, 8, 2b, 1, 4, 7 + locale keys E-H, A-C, L, M. Commits by concern, e.g.
   `fix(frontend): stop reconnecting the WebSocket after an auth or origin refusal`,
   `fix(frontend): give voiceprint opt-out and erasure refusals their own messages`,
   `fix(frontend): surface the server reason on summary, bulk and auto-label failures`,
   `fix(frontend): match MediaCMS hosts no more loosely than the backend`.
4. Item 3 (+ §1 `follow()`, keys D, I-K) after pinning the contract: `fix(frontend): report recordings kept by a legal hold or takedown after a purge`.
5. CHANGELOG `[Unreleased]` entries for every user-visible change and the API-only note (§9).
6. Push the branch; do not open a PR or merge (the coordinator does).

## VERIFICATION

### Automated (all must pass; save outputs with `tee` into `~/ot-cleanup-work/audit-v061/` and quote them)

```bash
cd frontend
npm run check && npm run check:i18n && npm run test && npm run build
npm run test:audit && npm run test:audit:selftest
cd ../backend
/mnt/nvm/repos/transcribe-app/backend/venv/bin/python -m pytest -v \
  tests/unit/test_self_erasure_last_super_admin.py tests/unit/test_mediacms_host_matching.py \
  tests/unit/test_llm_unusable_config_no_system_fallback.py tests/unit/test_chat_sse_contract.py
# DB-backed API tests against the ISOLATED gate stack (offset 600), never the dev stack:
POSTGRES_PORT=5776 OPENSEARCH_PORT=5780 MINIO_PORT=5778 /mnt/nvm/repos/transcribe-app/backend/venv/bin/python -m pytest -v \
  tests/api/test_self_erasure_api.py tests/api/test_self_erasure_blocked_outcome.py tests/test_chat_endpoints.py \
  tests/api/test_llm_unusable_config_detail.py
cd .. && python3 scripts/audit-tests.py backend/tests
scripts/safe-precommit.sh run --all-files && scripts/safe-precommit.sh run --all-files --hook-stage pre-push
```
Confirm the ports first: `./opentr.sh status --fresh v061gate` (or `cat .fresh/v061gate.offset` in the main
checkout). The gate stack runs whatever checkout started it, so a DB test against it tests YOUR code only for
the test process side; that is fine for these API tests (they use the in-process `client`). Check that the new
test names appear in the output.

### Manual browser checklist (light, dark, and `ar`)

**Do not seed on `v061gate`:** the erasure ledger is append-only (rows cannot be deleted afterwards) and the gate
agent owns that stack. Use your own throwaway stack built from YOUR branch, and destroy it at the end:

```bash
./opentr.sh start dev --fresh v061ui --port-offset 700 --seed-benchmark   # from your worktree; GPU 1 only
# frontend: http://localhost:5873  backend: http://localhost:5874  (base 5173/5174 + 700; confirm in the start output)
```
Ask the coordinator before starting it if GPU 1 is busy (`nvidia-smi`), and never touch GPU 0.
Browser driver: `node ~/bin/browser-tools/browse.js --display=:13 <url> "fill:..." "click:..." "screenshot:/home/superdave/ot-cleanup-work/audit-v061/ui-<name>.png"`
(see `~/bin/browser-tools/README.md` for actions). Log in as `admin@example.com` / `password`. For dark mode use the
theme toggle in the header; for `ar` change the language in Settings → Language. Take each screenshot in light, dark and `ar`.

1. **Held recording, voiceprint opt-out (item 1).** Seed a hold: as an org admin with `org.legal_hold`
   `POST /api/org-admin/holds` `{"scope":"file","file_uuids":["<uuid of a benchmark file>"],"reason":"UI check v061 hold seed"}`.
   Open the file → privacy / voice identification → turn off → confirm. Expect toast A, toggle unchanged.
   Clean up: `POST /api/org-admin/holds/<hold_uuid>/release`.
2. **Quarantined recording (item 1).** Quarantine a benchmark file from the admin quarantine UI, open its URL in a second
   tab where it was already loaded, try the toggle → toast B. Release it after.
3. **Blocked erasure (item 2).** On `v061ui` only: Settings → Admin → self-service erasure ON, grace 0 days
   (`PUT /api/admin/self-erasure {"enabled":true,"grace_days":0}`). As `admin@example.com` (the only super admin on this
   stack) request erasure in Settings → Profile. Trigger the due sweep: find it with `rg -n "def _execute_one|beat_schedule.*erasure" backend/app`
   and run it in `./opentr.sh shell backend --fresh v061ui` (or wait for beat). Reload: card shows notice E, banner shows H;
   the account still exists. If the account was erased instead, that is a backend bug: stop and report it.
   Also: request again, cancel → toast; and check the banner shows date AND time.
4. **Purge with held files (item 3).** With a hold on one file, run a privacy purge from Settings → Privacy (user scope), and a
   per-file opt-out on the held file. Expect line I, no `residualOk`, and K/D when everything was protected.
5. **Support-grant refusal (item 4).** Start a support session per `backend/app/auth/CLAUDE.md`, then regenerate a summary
   on a file → toast shows the server's refusal sentence, and the summary is still shown (not cleared).
6. **Unusable LLM (item 7).** Settings → AI: add a provider config with a bogus key, make it active, delete its credentials/region
   (or point the active setting at a deleted config per the backend test you wrote). Chat → send → message L and the "Open AI settings" button.
   Gallery → select files → bulk speaker-ID and bulk summarize → toast M. File detail with no summary → hint + link opens Settings → AI.
7. **WebSocket (item 8).** Log in, then revoke the session from another browser (Settings → Security → sessions, or log out there).
   Watch the Network tab / console for 60 s: one close with 4003, one `/auth/me`, then the login screen; no repeated `/api/ws` attempts.
8. **RTL:** in `ar`, notices E/H and list item I read right-to-left with the count on the correct side; no clipped text.

**Cleanup:** `./opentr.sh fresh-destroy v061ui` (asks y/N; this deletes the throwaway stack's containers and volumes,
including the append-only ledger rows). Delete nothing on any other stack. Then `./opentr.sh fresh-list` to confirm it is gone.

### Report back
Branch name, commit list, the red-then-green test names per item, the pinned §3 contract (or "blocked"), the verification
outputs' file paths, and the screenshot paths.
