# frontend — OpenTranscribe SvelteKit SPA

Orientation for the web client. Repo-wide rules live in the root `CLAUDE.md`; this file
is frontend-specific. Folder-level `CLAUDE.md` files add detail where you're working.

## Stack

- **SvelteKit 2 + Svelte 5 + TypeScript + Vite 6**, `@sveltejs/adapter-static` → a pure
  **client-side SPA** (`fallback: index.html`, no SSR, no `+page.server.ts`). Data is fetched
  client-side (`onMount`/reactive) from the FastAPI backend.
- i18n via i18next (8 locales in `src/lib/i18n/locales`). Node pinned to 22 (`.nvmrc`).
  Locales are **code-split, one chunk per language** — `src/lib/i18n/index.ts` globs them
  non-eagerly and `ensureLocaleLoaded()` fetches only the active one. Never static-import a
  locale JSON: that puts all ~2.3 MB back into the entry chunk. Only the active language is
  loaded (not the `en` fallback), which is safe because `npm run check:i18n` enforces exact
  key parity — keep it green.

## Commands (run in `frontend/`)

- `npm run dev` — Vite dev server (:5173, HMR). Prefer the whole stack via `./opentr.sh start dev`.
- `npm run build` — production build (catches Vite-only issues svelte-check misses).
- `npm run check` — `svelte-check` (type + a11y). `npm run lint` / `lint:fix` — ESLint.
- `npm run test` / `test:watch` — Vitest unit/component tests (jsdom).
- **Before committing**: lint + svelte-check + build + test must be green (pre-commit enforces
  it via `scripts/frontend-check.sh`).

## Path aliases (never use `../../`)

`$lib` → `src/lib` · `$components` → `src/components` · `$stores` → `src/stores`.

## Architecture rule: thin frontend, fat backend

The frontend renders backend data and captures input. Business logic, aggregation, and
domain formatting belong in the API. The backend already sends pre-formatted display fields
(`formatted_duration`, `display_status`, `resolved_speaker_name`, analytics, …) — render those,
don't recompute. Approved client-side exception: purely-presentational transforms on
already-downloaded data (TXT/SRT/VTT/CSV export).

## dev (Vite) vs prod (nginx) — both must work

- **dev**: `./opentr.sh start dev` bind-mounts source for HMR; the app is served by Vite at :5173.
- **prod**: `frontend/Dockerfile.prod` builds static assets served by **nginx** (`nginx.conf`,
  `nginx-pki.conf`) with CSP/HSTS/security headers. Any change touching `app.html`, asset paths,
  env (`import.meta.env.VITE_*`), or CSP must be verified in BOTH. No secrets in the bundle.

## Where things live

- `src/components/ui` — shared primitives (see its CLAUDE.md). `src/lib/utils` — pure helpers
  (time formatting lives ONLY in `formatting.ts`). `src/lib/api` — typed API clients.
  `src/stores` — Svelte stores. `src/routes` — pages.
- `src/lib/cloud` — **managed-edition seam stub** (see its README). The commercial repo replaces
  this directory at image-build time. Core code imports only `$lib/cloud` (+ its `components/`),
  gates every call site with `isCloudEdition` from `$lib/edition`, and must never name the
  edition's vendors — CI's seam-guard greps `frontend/src` (and `backend/app`) for `clerk|stripe`
  and fails the build on a match.

## Verify UI changes in a browser (light + dark) — type-check ≠ feature-check.
