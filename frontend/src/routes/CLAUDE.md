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
- `login/`, `register/`, `forgot-password/`, `reset-password/`, `accept-invite/`,
  `verify-email/` — auth pages. `login/` also hosts the OIDC callback landing (the IdP's
  registered redirect URI points **here**, never at `/api/auth/oidc/callback`), the login
  banner, the MFA challenge and the forced-MFA-enrolment step.
  `accept-invite/` redeems an admin invitation — for an external `auth_type` it collects no
  password at all and hands the user to the identity provider.
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

- **Don't paint a page against placeholder config and then correct it — gate the render on the
  fetch.** `login/+page.svelte` initialises `authMethods` to a local-only deployment
  (`oidc_enabled: false`, `ldap_enabled: false`, `allow_registration: false`) and then replaces
  it with `getAuthMethods()`. On any deployment that actually enables SSO the answer _differs_
  from those defaults, so the SSO buttons, the PKI button and the register link were inserted
  into an already-painted card — moving the submit button under the user's cursor. It is a real
  CLS defect for anyone on a slow connection, and it made the whole auth E2E suite
  non-deterministic: `wait_for_selector('#email')` returned on the pre-fetch paint, the fills
  succeeded, and the click then raced the reflow. **58 auth tests failed this way on
  2026-09-06** while the same files passed 29/29 against an idle stack — the fetch is only slow
  enough to lose when the machine is busy, which is exactly when the gate runs. It stayed hidden
  for months because the local-only defaults _matched_ reality whenever OIDC was off; enabling
  Keycloak is what exposed it. The card now renders a spinner until `authMethodsLoaded`.
  ⚠️ That flag must be set in a **`finally`** — this `onMount` body has no try/catch of its own,
  so gating the UI on a request that can reject would strand the page on its placeholder and
  turn a transient backend blip into "nobody can sign in". Fail **open** to the local form.
- This is a static SPA — `+page.ts` load runs in the browser; data-loading routes set `ssr = false`.
  Don't add server `load` functions expecting a Node server; there is none in production.
- `+error.svelte` is the catch-all for unmatched routes (no real 404 from a server).
