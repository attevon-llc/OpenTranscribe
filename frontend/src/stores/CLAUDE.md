# src/stores — Svelte stores

## Purpose

Client-side reactive state (the SPA's UI/session state). Import via `$stores/...`. State that
is the system of record belongs in the backend, not here — these hold view/session state and
caches of API responses.

## Key stores

- `auth.ts` — user + tokens, login/logout, 401 auto-refresh.
- `websocket.ts` — real-time file/task status updates (large; split candidate).
- `toast.ts` — `toastStore.success/error(...)` notifications.
- `transcriptStore.ts` — transcript segments + processed cache (used by transcript views/analytics).
- `notifications.ts`, `downloads.ts`, `uploads.ts`, `recording.ts`, `gallery.ts`, `search.ts`,
  `sharing.ts`, `groups.ts`, `llmStatus.ts`, `network.ts`, `locale.ts`, `theme.js`,
  `speakerColors.ts`, `audioPlaybackStore.ts`, `settingsModalStore.ts`.

## Conventions / patterns

- In `.svelte` files use `$storeName` auto-subscription. In `.ts` files use `get(store)` from
  `svelte/store`. **Never mix** the two patterns in one file type.
- Prefer subscribing to an existing store over prop-drilling shared state (e.g. transcript
  views read `transcriptStore` instead of threading segments through many props).
- Toasts/errors: use `toastStore` (and `$lib/utils/apiError`) — don't build ad-hoc notifiers.

## Gotchas

- `websocket.ts` dispatches custom events some pages listen for (e.g. clustering progress);
  keep event names stable when refactoring.
