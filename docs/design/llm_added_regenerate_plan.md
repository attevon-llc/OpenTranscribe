# Adding an LLM provider after upload: stale availability, and what "Generate summary" fills in

Branch `fix/llm-added-regenerate-insights` (from master `a236125a`). Owner report, real usage on
the dev stack: files were uploaded before any LLM provider existed. The owner then added a
provider and clicked **Generate summary** on one of those files.

- **(A)** Only the summary was produced. The file still had no AI tags and no AI collections.
- **(B)** Right after adding the provider the Generate summary button stayed **disabled** until a
  page refresh.

This document is the plan a fresh agent can execute from disk. It records the findings with
evidence, the decisions, the file list, the sequencing and the verification.

---

## (B) Stale LLM availability: findings

`frontend/src/stores/llmStatus.ts` is the one client cache of `GET /api/llm/status`
(`isLLMAvailable` is derived from it). Every gated surface reads it: the file page's
`FileActionButtons` (via `llmAvailable = $isLLMAvailable`, `routes/files/[id]/+page.svelte:225`),
`SummaryModal.svelte:52`, `SelectiveReprocessModal.svelte:142-160`, the chat page,
`ChatAdminSettings`, `AutoLabelSettings`, `SpeakerAttributeSettings` and the notification card's
LLM chip weighting (`NotificationsPanelBody.svelte:40`).

The store only learns a new answer from `initialize()` (once per session: it is a no-op after its
first success, by design, `llmStatus.ts:34-45`), a 2-minute poll started by `initialize()`, and
explicit `refreshStatus()` calls. The owner's exact flow defeats all three:

1. **Opening Settings > LLM Provider with no configuration KILLS the store.**
   `LLMSettings.svelte:113-122` (`loadData`) calls `llmStatusStore.reset()` whenever the user has
   no configurations. `reset()` (`llmStatus.ts:142-146`) sets `available: false`, **stops the
   2-minute poll** and clears `initPromise`. Nothing re-initializes it afterwards: the layout's
   reactive `$: if ($isAuthenticated) llmStatusStore.initialize()` (`+layout.svelte:142-144`)
   only re-runs when `$isAuthenticated` changes, and it does not. From this moment the store is
   frozen at "unavailable" for the rest of the tab's life.
   It is also *wrong* on its own terms: "this user has no configuration" is not "no LLM" — the
   status endpoint falls back to the system provider (`LLMService.create_from_settings`), so a
   deployment with `LLM_PROVIDER` set was told "unavailable" merely by opening this panel.
2. **Saving a new configuration never refreshes the store.** `handleConfigSaved`
   (`LLMSettings.svelte:434-457`) updates the local list and calls `onSettingsChange`, which is an
   empty function in `SettingsModal.svelte:986-988`. The backend **auto-activates the first
   configuration** (`backend/app/api/endpoints/llm_settings.py:820-828`), so after this save the
   server says "available" while the client store still says what step 1 left it at. The local
   `activeConfigurationId` is not updated either, so the panel itself shows no active config
   until a reload.
3. **"Test connection" on a saved config never refreshes the store** (`testSavedConfiguration`,
   `LLMSettings.svelte:256-273`).
4. **Deleting the active configuration resets instead of re-asking.** `deleteConfiguration`
   (`LLMSettings.svelte:364-372`) and `deleteAllSettings` (`:415-416`) call `reset()`. The
   backend promotes the first remaining configuration when the active one is deleted
   (`llm_settings.py:1045-1058`) and still has the system fallback, so `reset()` can disable the
   button while an LLM is in fact available — and it stops the poll again (step 1).
5. **The push path is dead code.** `llmStatusStore.handleNotification` handles
   `llm_settings_changed` / `llm_status_changed`, but nothing calls it (`stores/websocket.ts` does
   not) and the backend emits neither event. It looked like a refresh path and was none.
6. **No re-check when a long-lived page regains attention.** A file page left open in another tab
   while the provider is added in this one waits up to 2 minutes (or forever, after step 1).

Root cause in one line: settings flows mutate server state and then either do nothing to the
store or *destroy* it (`reset()`), and the store has no self-healing path once destroyed.

## (B) Decisions

- **One function for "LLM settings changed, re-ask the server": `llmStatusStore.invalidate()`.**
  It re-fetches status now and makes sure the background re-check (poll + focus/visibility) is
  running. Every settings flow calls it after a successful mutation: create, update, activate,
  delete (one or all), connection test, and the panel's initial load. `reset()` is reserved for
  sign-out (`lib/session/clearUserState.ts`), its only legitimate caller.
- **Re-check on attention.** While monitoring is on, the store re-checks on `visibilitychange`
  (becoming visible) and window `focus`, throttled so a burst of focus events costs one request.
  This covers the other-tab case for every gated surface at once, without per-page code.
  Listeners are removed by `stopMonitoring()` (sign-out).
- **Delete the dead `handleNotification` push path.** No backend event exists to drive it; keeping
  it suggests a mechanism that does not exist. If a server push is wanted later it needs a backend
  event first (see follow-ups).
- The chat `ModelSwitcher` is NOT a settings flow: it pins a configuration to one conversation
  and never changes the account's active configuration, so availability cannot change there.

## (A) What "Generate summary" should do: findings

- `POST /api/files/{uuid}/summarize` (`backend/app/api/endpoints/summarization.py:61-202`)
  dispatches **only** `summarize_transcript_task`. Nothing else.
- Tags and collections come from a different step: `extract_topics_task`
  (`backend/app/tasks/topic_extraction.py:251`, `ai.extract_topics`) writes a `TopicSuggestion`
  row and, inside the same write, `AutoLabelService.auto_apply_suggestions` applies the
  high-confidence ones as `source="auto_ai"` tags/collection memberships
  (`services/topic_extraction_service.py:726-747`). Auto-apply is **additive only**
  (`auto_label_service._add_tag_to_file` / `_add_file_to_collection` skip existing rows and never
  delete), so a user's own tags and collections are never removed or overwritten.
- At upload time the pipeline dispatches topic extraction from
  `tasks/transcription/downstream.py:153-164` (automatic path, gated by the owner's
  `auto_topic_extraction` switch through `services/auto_llm_settings.py`, #1148). When the file
  was transcribed with **no** LLM, `extract_topics_task` returned `{"status": "skipped",
  "reason": "LLM not configured"}` (`topic_extraction.py:289-296`) and wrote nothing — no
  `TopicSuggestion` row exists for those files.
- So a manual summary on such a file runs one LLM step and leaves the other one, which the
  automatic pipeline would have run, permanently missing. The owner's expectation ("summary
  regenerate should fill in AI tags and collections if missing") is the automatic pipeline's
  behaviour, which never got its chance.
- The recovery sweep (`TaskDetectionService.identify_incomplete_post_transcription_files`) is not
  a fix: it is gated on an LLM being configured at sweep time and is a background process, not
  the user's action.

## (A) Decisions

**A manual Generate / Regenerate summary also queues topic extraction for the file when, and only
when, all of these hold** (one service function, `services/ai_insights_companion.py`,
`queue_topic_extraction_with_summary`):

| Condition | Why | Outcome when it fails |
|---|---|---|
| No `TopicSuggestion` row exists for the file | The row is the record that extraction ran. Its absence is exactly the owner's case. If it exists (even with nothing auto-applied, or with suggestions the user rejected) the AI output is already there and re-running costs money and second-guesses the user's decisions. | `already_extracted` |
| The file owner's `auto_topic_extraction` resolves ON (`resolve_auto_llm_flags`) | This dispatch is a companion the user did not ask for by name, so it is an automatic dispatch and must go through the resolver (the rule in `services/CLAUDE.md`). The OWNER's switch, because the upload pipeline uses the owner's and the task bills the owner's LLM. | `disabled_by_setting` (the UI says so) |
| The owner has an LLM configured (`LLMService.is_configured_for_user`, DB-only) | The task resolves the owner's provider; without one it skips silently, which would leave a queued chip that never resolves. | `unavailable` |
| No topic-extraction run is in flight for the file | Repeated clicks, two tabs, or an automatic run already queued must not stack LLM calls. Two layers: an active `Task` row (`task_type="topic_extraction"`, pending/in_progress) and a Redis `SET NX` guard (`ai_insights:topic_companion:<file_id>`, 15 min) taken before dispatch. A Redis error fails CLOSED (no dispatch): a duplicate costs an LLM call, a skipped companion costs nothing the user cannot redo. | `in_progress` / `unavailable` |

- Dispatch is `extract_topics_task.delay(file_uuid=..., force_regenerate=False)`. `False`, so even a
  race past both guards cannot replace a suggestion another run just stored.
- **Tenancy and quarantine** come from the endpoint's existing `get_file_by_uuid_with_permission`
  (tenant gate + takedown gate + `editor` permission) — the companion runs only after that
  passed and after the summary's own checks (transcript present, system summary switch, LLM
  available). A 4xx from any of them dispatches nothing at all.
- **Chips on the consolidated card** use the existing events: the endpoint sends
  `topic_extraction_status` `queued` right after the summary's `summarization_status` `queued`
  (same recipient, `current_user`, as the summary's queued frame); the task's own `processing` /
  `completed` frames follow. No new event type. `queued` already maps to the pending chip
  (`lib/notifications/runTiming.ts` `PENDING`), and the file page already reloads suggestions on
  `topic_extraction_status completed` (`lib/fileDetail/notificationHandler.ts:237-263`).
- **Event contract relied on** (for reconciling with the notification-stages branch): type
  `topic_extraction_status`, sent through `tasks/topic_extraction.send_topic_extraction_notification`
  -> `notification_service.send_task_notification` with the file's real integer `file_id` (so the
  frame carries the file's uuid/filename and groups onto that file's card), `status` `queued` from
  the endpoint, then the task's own `processing` / `completed` (with `suggestion_id`) / `failed`.
  The summary keeps its existing `summarization_status` frames. No new type, no change to
  `auto_label_status`.
- The endpoint returns the outcome as `topic_extraction` in its JSON. The SPA shows one info toast
  for `queued` ("tags and collections will also be suggested") and one for `disabled_by_setting`
  ("skipped because automatic tag and collection suggestions are turned off"); other outcomes are
  silent. All three summary call sites on the file page (Generate, Regenerate, Regenerate with
  prompt) go through one helper, `lib/api/summarize.ts`.
- **LLM speaker-ID is never touched** — not queued, not applied (repo rule).
- **Not changed:** the explicit stage list in `SelectiveReprocessModal` (choosing "summarization"
  alone stays exactly that), the `POST /files/{uuid}/extract-topics` button (explicit, ignores the
  switch, by #1148's rule), and the automatic pipeline.

## Catch-up affordance ("N files have no AI summary or tags: generate?")

**Not built in this change; recommended as a follow-up.** Doing it properly needs a backend count
endpoint over the caller's accessible files (summary missing / topic suggestion missing), a
cost-aware confirmation that states N, and a bulk dispatch that reuses the companion's guards
(`POST /files/batch-extract` exists for topics; summaries have no batch route). That is a feature
with its own UX and tests, not a small addition to this fix, and it must never run automatically.

## File list

Backend
- `backend/app/services/ai_insights_companion.py` (new) — the decision + guard + dispatch.
- `backend/app/api/endpoints/summarization.py` — call it after the summary dispatch, return
  `topic_extraction`.
- `backend/tests/api/test_summarize_topic_companion.py` (new) — the API tests below.

Frontend
- `frontend/src/stores/llmStatus.ts` — `invalidate()`, focus/visibility re-check, drop
  `handleNotification`.
- `frontend/src/components/settings/LLMSettings.svelte` — every flow calls `invalidate()`; no
  `reset()`; `handleConfigSaved` re-reads the configuration list (picks up auto-activation).
- `frontend/src/lib/api/summarize.ts` (new) — the one summarize call + outcome-to-message map.
- `frontend/src/routes/files/[id]/+page.svelte` — three call sites use the helper.
- `frontend/src/lib/i18n/locales/*.json` (12) — two strings.
- Tests: `frontend/src/stores/llmStatus.test.ts`, `frontend/src/components/settings/LLMSettings.statusRefresh.test.ts`,
  `frontend/src/lib/api/summarize.test.ts`.

## Tests (each seen red first)

Backend (`test_summarize_topic_companion.py`, real `MediaFile`/`TopicSuggestion`/`UserSetting`/
`Task` rows on `db_session`; only the Celery `.delay`, the notification send and Redis are
replaced):
1. no suggestion + switch on + LLM configured -> `extract_topics_task.delay` called once with
   `force_regenerate=False`, response `topic_extraction == "queued"`, a `queued`
   `topic_extraction_status` frame sent.
2. a `TopicSuggestion` exists -> not dispatched, `already_extracted`.
3. owner's `auto_topic_extraction = false` -> not dispatched, `disabled_by_setting`.
4. two clicks -> dispatched once; second answers `in_progress`.
5. an active `topic_extraction` `Task` row -> not dispatched, `in_progress`.
6. Redis unavailable -> not dispatched (fail closed), summary still 200.
7. quarantined file -> 404, nothing dispatched; unshared user -> 403; a file in an organization
   the caller is not acting in -> 403; nothing dispatched in any of them.
8. owner has no LLM configured -> `unavailable`, not dispatched, no chip.

Implemented as `tests/api/test_summarize_topic_companion.py` (11 tests). On a `git archive HEAD`
tree 8 fail (`KeyError: 'topic_extraction'`); the 3 gate tests (quarantine, other user, other
tenant) pass on HEAD by design: they pin that the new dispatch sits behind the existing gates.

Frontend
- `llmStatus.test.ts`: `invalidate()` re-fetches and restarts monitoring after a `reset()`;
  becoming visible / focus re-checks (throttled); `stopMonitoring` removes the listeners.
- `LLMSettings.statusRefresh.test.ts`: the owner's exact flow through the real panel and the
  real config modal — panel loads with no configs (status unavailable), a provider is saved, the
  store refreshes, and a `FileActionButtons` rendered against `$isLLMAvailable` enables its
  Generate summary button; the converse, deleting the last provider disables it (a control:
  HEAD's `reset()` also disabled it); and opening the panel with no configuration while the
  system provider answers keeps the button enabled. Tests 1 and 3 red on HEAD.
- `summarize.test.ts`: outcome -> message mapping (queued / disabled_by_setting -> a message,
  every other outcome -> none).

Red check: `RED="$(mktemp -d -t ot-redcheck.XXXXXX)" && git archive HEAD | tar -x -C "$RED"`, copy
the new tests in, run, expect red. Remove the tree afterwards.

## Verification

- Backend: full unit/API suite once in one process (`-n 12`, POSTGRES/MINIO/OPENSEARCH creds from
  the main `.env` via a wrapper that prints nothing), `mypy app`, `ruff`, `python3
  ../scripts/audit-tests.py tests` = 0.
- Frontend: `npm run check`, vitest (touched, then full), `npm run check:i18n`, `npm run
  test:audit`, `npm run build` (bundle budget).
- Browser: this worktree's frontend on `npx vite dev --port 5482 --strictPort` against the live
  backend (:5174), headless Playwright, `page.route` stubs for `/api/llm/status` and
  `/api/llm-settings*` to flip availability without touching real provider config; prove the
  file page's Generate summary button enables after a provider "save" and on focus, without a
  reload. Light and dark screenshots to `/tmp/ot-insights-shots/`. The live backend runs master,
  so (A) is proven by tests, not live.

## Follow-ups for the owner

1. **Catch-up affordance** (above): a suggestion banner after the first provider is added, with a
   backend count and a confirmation stating how many LLM calls it will make.
2. **Server push for LLM availability**: emit an `llm_settings_changed` event to the user (and to
   share recipients of a shared config) on create/update/delete/activate, so other open tabs flip
   at once instead of on focus. Needs a backend event; the client hook is `invalidate()`.
3. The summary itself has no in-flight dedup (a double click queues two summaries). Same guard
   shape as the companion would fix it; out of scope here.
