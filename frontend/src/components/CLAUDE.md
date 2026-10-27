# frontend/src/components

## Purpose

Feature components for the OpenTranscribe SvelteKit SPA — the large, app-specific
building blocks (gallery, transcript, settings, speakers, navbar, upload, search,
sharing, etc.) that the routes compose. Primitives live in `ui/`.

## Key files

- `TranscriptDisplay.svelte`, `VideoPlayer.svelte`, `Navbar.svelte`, `SettingsModal.svelte`,
  `FileUploader.svelte`, `UserFileStatus.svelte`, `CollectionsPanel.svelte` — coordinators
  that own data + state and delegate rendering to thin children.
- `RetrievalQualityNotice.svelte` — the honest "results may be imperfect" note, shared by chat,
  search, and (issue #756) the speakers page. **The `surface` prop is not cosmetic**: the
  cross-encoder reranker runs only on the chat retrieval path, so the chat copy names it and the
  search copy must not — `/search` is ranked by OpenSearch RRF fusion; the speakers surface names
  neither (clustering is voice-embedding similarity) and links to #524, not #461. Copy and issue
  URL are looked up from a `Record<surface, …>` map, not a ternary — a ternary silently falls
  through to one branch's copy for any surface it wasn't written to handle. Dismissal is
  per-surface in `localStorage` (`opentr:retrievalQualityNotice:<surface>`) and, being a UI
  preference, is deliberately NOT cleared by `clearUserState`.
- `NotificationsPanel.svelte` — bell panel **shell** (open state, scroll lock, click-outside,
  backdrop). Its contents, `notifications/NotificationsPanelBody.svelte`, load with a dynamic
  `import()` on first open so the card model stays out of the root bundle (bundle budget; a
  source test in `NotificationsPanel.test.ts` pins it). **One card per file**: a file's
  `transcription_status` / `summarization_status` / `topic_extraction_status` / `redaction_status` /
  `youtube_processing_status` / `post_processing_status` records render as ONE
  `notifications/ProcessingJobCard.svelte` (weighted bar, one chip per tier: Main processing /
  Finishing / AI enrichment, the running or failed tasks, expandable per-tier timing) and count once in
  the badge. The store still keeps one record per type, because the file page, gallery and status page
  dispatch on `type`. Grouping lives in `$lib/notifications/jobGrouping.ts` (badge + retention), the
  card model in `$lib/notifications/processingJob.ts`, run timing in `runTiming.ts`, the post-processing
  run reducer in `postProcessing.ts` (root bundle, keep it small) and the missed-frame REST fallback in
  `reconcile.ts` (lazy). **"Complete" means every task the backend's plan named has finished**: while
  any is queued/running the card says "Finishing up", and with no plan it says "Transcript ready / final
  steps could not be confirmed", never Complete. A new backend `PostProcessingTask` key needs a tier in
  `POST_PROCESSING_TIERS` and a `JOB_TASK_KEYS` label (a backend test fails otherwise). The batch-scoped
  `auto_label_status` run stays ONE plain row; per-file auto-labeling arrives as the `auto_label` task on
  the file's own card. Design: `docs/design/consolidated_processing_notification_plan.md`.
  Icons: `ICON_BY_TYPE` (`$lib/notifications/display.ts`) maps types to icon names and **each name needs a
  render branch** in `notifications/NotificationIcon.svelte` (an unmapped name silently shows the bell;
  `NotificationsPanel.test.ts` asserts coverage). `transcription_status` is the `logo` icon, an
  `<img>` of `assets/logo-icon.png` (the only non-SVG entry, so tests query `.notification-icon img`);
  topic/auto-label share the tag glyph and collection notifications use the folder glyph.
  **Retention (issue #1217):** the list is an inbox, pruned — never animated away. The policy is a
  pure function in `$lib/notifications/retention.ts` (injected `now`, no timers); `websocket.ts`
  runs it on load, on add and on panel close, and it is a no-op while the panel is open. Never
  removed: errors/warnings (`status`/`data.status` anything but completed), in-flight items,
  `persistent` types, unread items under 14 days. A file's card is retained as ONE unit (all its
  records go together, a failure anywhere pins it, a skipped LLM task does not). `RETENTION_BY_TYPE` classifies every
  `NotificationType` (compile-time `Record` + a test against `NOTIFICATION_TYPES`); an unknown
  wire type is never clearable, so a new type is NOT auto-clearable until someone classifies it.
  `readAt` (set by `markAsRead`/`markAllAsRead`, cleared when a progress update re-marks a row
  unread) is the clock for read items. The "Keep notifications for" select
  (`notifications/NotificationRetentionSetting.svelte`) persists to
  `localStorage['opentr:notificationRetention']`, a UI preference that `clearUserState` keeps. The
  "N older notifications cleared" note (`notifications/ClearedNotice.svelte`) reads
  `clearedNotice`, whose count is fixed at panel open and acknowledged at close **in the store**:
  the store is the source of truth because the panel is app chrome that must not keep its own
  copy. **Mounted exactly once, by `routes/+layout.svelte`** (issue #1222): it used to be mounted
  again by `Navbar.svelte`, so the markup, the click-outside listener and the scroll lock all
  doubled while it was open. `Navbar.notificationsMount.test.ts` fails on a second mount. The
  bell button (`.notifications-btn`, a visual-regression mask) stays in the navbar and only toggles
  `showNotificationsPanel`; `.notifications-panel` / `.notifications-backdrop` are unchanged.
- Subfolders: `transcript/`, `speakers/`, `settings/`, `gallery/`, `navbar/`,
  `fileStatus/`, `collections/`, `sharing/`, `groups/`, `search/`, `upload/`, `ui/`, `chat/`,
  `fileDetail/`, `tags/`, `supportAccess/` (each with its own `CLAUDE.md`), plus `notifications/`,
  `mfa/`, `legal/` and `takedown/` (none yet).
  Each subfolder holds the presentational children split out of one oversized coordinator.

## Conventions / patterns

- Import via the `$components` alias (not `../../`). Stores via `$stores/...`, libs via `$lib/...`.
- **Coordinator vs child**: the route/parent owns API calls, WebSocket/SSE, and source-of-truth
  state; children are thin presentational components that receive props and `dispatch` events up.
  This is the result of the file-split refactor — don't move data logic back into children.
- **Thin frontend**: business logic belongs on the backend; components render and forward.
  ⚠️ **There is NO client-side exception for transcript serialization.** This line used to
  name `src/lib/export/` as one; that exception was retracted in `frontend/CLAUDE.md` because
  it was the mechanism of a live security bug (issue #673 — a client-assembled transcript
  bypasses the server's redaction and `export_locked` policy), and since issue #821
  `src/lib/export/` contains no serializer at all: `requestTranscriptExport.ts` asks the
  server. Every transcript byte a user can copy or download must come from
  `GET /api/files/{uuid}/export`. **The summary path now complies too (issue #885):**
  `SummaryModal.svelte`'s copy button used to build its own markdown client-side and dropped
  the action-items/speaker-analysis sections doing so; it now calls
  `requestSummaryExport.ts` -> `GET /api/files/{uuid}/summary/export`, which renders from the
  same masked copy the summary read endpoint returns.
- i18n everywhere via `$t(...)` from `$stores/locale`. Light/dark parity required.
- Prefer `$store` auto-subscription in markup over `get(store)`.

## How it connects

- Composed by `src/routes/*` pages; talk to the backend through `$lib/api/*` + `$lib/axios`;
  share state through `src/stores/*`.

## Gotchas

- For shared primitives (BaseModal, Spinner, ProgressBar, EmptyState…) use `ui/` — don't
  re-implement. See `ui/CLAUDE.md`.
- Several components carry E2E-guarded class selectors (see each subfolder's CLAUDE.md) —
  renaming them breaks Playwright tests in `backend/tests/e2e/`.
