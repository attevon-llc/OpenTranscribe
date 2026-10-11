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
- `NotificationsPanel.svelte` — bell panel. `ICON_BY_TYPE` maps every notification type to an
  icon name and **each name needs a render branch** (an unmapped name silently shows the bell;
  `NotificationsPanel.test.ts` asserts coverage). `transcription_status` is the `logo` icon, an
  `<img>` of `assets/logo-icon.png` (the only non-SVG entry, so tests query `.notification-icon img`);
  topic/auto-label share the tag glyph and collection notifications use the folder glyph.
  **Retention (issue #1217):** the list is an inbox, pruned — never animated away. The policy is a
  pure function in `$lib/notifications/retention.ts` (injected `now`, no timers); `websocket.ts`
  runs it on load, on add and on panel close, and it is a no-op while the panel is open. Never
  removed: errors/warnings (`status`/`data.status` anything but completed), in-flight items,
  `persistent` types, unread items under 14 days. `RETENTION_BY_TYPE` classifies every
  `NotificationType` (compile-time `Record` + a test against `NOTIFICATION_TYPES`); an unknown
  wire type is never clearable, so a new type is NOT auto-clearable until someone classifies it.
  `readAt` (set by `markAsRead`/`markAllAsRead`, cleared when a progress update re-marks a row
  unread) is the clock for read items. The "Keep notifications for" select
  (`notifications/NotificationRetentionSetting.svelte`) persists to
  `localStorage['opentr:notificationRetention']`, a UI preference that `clearUserState` keeps. The
  "N older notifications cleared" note (`notifications/ClearedNotice.svelte`) reads
  `clearedNotice`, whose count is fixed at panel open and acknowledged at close **in the store**:
  `NotificationsPanel` is mounted twice (`+layout.svelte` and `Navbar.svelte`), so component state
  or a DOM `id` would be duplicated — the setting uses an implicit `<label>` for that reason.
- Subfolders: `transcript/`, `speakers/`, `settings/`, `gallery/`, `navbar/`,
  `fileStatus/`, `collections/`, `sharing/`, `groups/`, `search/`, `upload/`, `ui/`.
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
