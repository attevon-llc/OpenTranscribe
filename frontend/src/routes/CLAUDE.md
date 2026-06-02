# frontend/src/routes

## Purpose

SvelteKit pages for the OpenTranscribe SPA. The app ships as a static single-page app
(`@sveltejs/adapter-static`, `fallback: index.html`), so all data loading is **client-only** —
there is no server-side rendering of app data.

## Key files

- `+page.svelte` — `/` home gallery (coordinator for `$components/gallery/*`; server-driven
  filter/sort/pagination via `URLSearchParams`).
- `files/[id]/+page.svelte` (+ `+page.ts` with `export const ssr = false`) — file detail:
  player, transcript, comments, export.
- `speakers/+page.svelte` — speaker clusters/profiles/inbox (coordinator for `$components/speakers/*`).
- `search/+page.svelte` (+ `+page.ts`, `ssr = false`) — hybrid search.
- `file-status/+page.svelte` — processing/task dashboard (`$components/UserFileStatus.svelte`).
- `login/`, `register/`, `forgot-password/`, `reset-password/` — auth pages.
- `+layout.svelte` — app shell (navbar, theme, toasts, websocket wiring).
- `+error.svelte` — error/404 boundary; renders `$page.status`/`$page.error`, friendly i18n copy.

## Conventions / patterns

- Pages are **coordinators**: they own data fetching (via `$lib/api/*` + `$lib/axios`), WebSocket/SSE,
  and source-of-truth state, and delegate rendering to thin children under `$components/*`.
- i18n via `$t` from `$stores/locale`; import components via `$components`, libs via `$lib`,
  stores via `$stores`.

## How it connects

- Composes feature components from `src/components/*`; shares state via `src/stores/*`; talks to
  the FastAPI backend through `$lib/api/*`.

## Gotchas

- This is a static SPA — `+page.ts` load runs in the browser; data-loading routes set `ssr = false`.
  Don't add server `load` functions expecting a Node server; there is none in production.
- `+error.svelte` is the catch-all for unmatched routes (no real 404 from a server).
