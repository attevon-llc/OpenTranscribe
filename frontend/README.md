<div align="center">
  <img src="../assets/logo-banner.png" alt="OpenTranscribe Logo" width="400">

# Frontend

</div>

The OpenTranscribe web client: a static, client-side-only single-page app that talks to the
FastAPI backend over `/api` (REST) and `/api/ws` (WebSocket).

Conventions, gotchas and per-folder rules live in `CLAUDE.md` files next to the code, starting
with [`frontend/CLAUDE.md`](CLAUDE.md). Read the one for the directory you are changing before you
edit it.

## Stack

All versions below come from `package.json`.

- **SvelteKit 2** (`@sveltejs/kit`) with **Svelte 5**, written in **TypeScript**
- **Vite 8** for dev server and build; `@sveltejs/adapter-static` with `fallback: index.html`, so
  the build is a pure SPA (no SSR, no server routes)
- Plain CSS with custom properties (`src/styles/theme.css`); there is no Tailwind
- **i18next** for translations (12 locales)
- Plyr for media playback, `@ffmpeg/ffmpeg` (a self-built FFmpeg.wasm core) for in-browser audio
  extraction, `marked` + DOMPurify + KaTeX for chat markdown
- **Vitest** + Testing Library (jsdom) for unit and component tests; ESLint 10 and Prettier for
  linting and formatting

There is no service worker and no offline mode, deliberately (see "dev vs prod" in `CLAUDE.md`).
`static/manifest.json` keeps the app installable.

## Quick start

Prerequisites: Node 26 (`frontend/.nvmrc`; `package.json` `engines` only requires `>=20`) and npm.

The normal way to develop is through the whole stack, which runs Vite with hot reload in a
container and bind-mounts this directory:

```bash
# from the repo root
./opentr.sh start dev        # frontend at http://localhost:5173, backend at :5174
```

Standalone Vite, if you only want the frontend and already have a backend running:

```bash
cd frontend
npm install
npm run dev                  # http://localhost:5173
```

The dev server proxies `/api` (including the `/api/ws` WebSocket), `/health`, `/s3`, `/minio`,
`/flower` and `/docs` to the compose service names (`backend:8080`, `minio:9000`, ...). Outside
Docker, point it at a reachable backend with `VITE_API_PROXY_TARGET`
(for example `VITE_API_PROXY_TARGET=http://localhost:5174 npm run dev`) and `VITE_MINIO_URL`
for MinIO. See `vite.config.ts`.

In production the same static build is served by nginx (`Dockerfile.prod`, container port 8080,
published on `FRONTEND_PORT`, default 5173). Local source changes only reach a prod-style stack
after you rebuild its images; see the root `CLAUDE.md`.

## Commands

Run these in `frontend/`. The full list is in `package.json` (`npm run` prints it).

| Command                                            | What it does                                                                                                                                                          |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `npm run dev`                                      | Vite dev server with HMR on :5173                                                                                                                                     |
| `npm run build`                                    | Production build into `dist/`. `prebuild` downloads fonts and builds the FFmpeg.wasm core; `postbuild` runs the CSP check, the bundle-budget gate and pre-compression |
| `npm run preview`                                  | Serve the built output locally                                                                                                                                        |
| `npm run check`                                    | `svelte-check` type-check. It fails on type errors but not on a11y warnings                                                                                           |
| `npm run lint` / `npm run lint:fix`                | ESLint                                                                                                                                                                |
| `npm run check:i18n`                               | Locale **key parity** against `en.json` (see below)                                                                                                                   |
| `npm run test`                                     | Vitest, single run. Sets `NODE_OPTIONS=--no-experimental-webstorage`, which Node 26 needs; do not call bare `npx vitest`                                              |
| `npm run test:watch` / `test:ui` / `test:coverage` | Vitest watch, UI and coverage (coverage is report-only)                                                                                                               |
| `npm run test:audit`                               | Finds tests that would pass whether or not the code works; allowlist is `test-audit-allowlist.txt`. `npm run test:audit:selftest` checks the auditor itself           |
| `npm run deadcode`                                 | knip unused-export/dependency scan                                                                                                                                    |
| `npm run cycles`                                   | madge circular-import scan                                                                                                                                            |
| `npm run build:analyze`                            | Build with a bundle treemap, copied to `dist/stats.html`                                                                                                              |

Before a commit, the repo's pre-commit hooks run `scripts/frontend-check.sh --check-only`
(ESLint, i18n parity, svelte-check, vitest and the test auditors); the full `vite build` runs at the
push stage. Run the hooks through `scripts/safe-precommit.sh`; see the root `CLAUDE.md`.

## Directory layout

```
frontend/
  src/
    routes/        SvelteKit pages (see below)
    components/    Feature components, grouped by area (gallery, transcript, settings,
                   speakers, upload, search, chat, sharing, navbar, ...); ui/ holds the
                   shared primitives (BaseModal, Tabs, Chip, EmptyState, ...)
    stores/        Svelte stores: auth, locale, theme, uploads, websocket, gallery, chat,
                   capabilities, downloads, recording, toast, ...
    lib/
      api/         Typed API clients, one module per backend area
      i18n/        i18next setup, language list, locales/*.json
      settings/    Settings-modal section loaders and tab definitions
      cloud/       Managed-edition extension seam (see below)
      actions/     Svelte actions (anchoredPanel, ...)
      utils/       Pure helpers; time formatting lives only in formatting.ts
      axios.ts     The shared axios instance (baseURL /api, cookie credentials)
      edition.ts   isCloudEdition build constant
      ...          auth, chat, export, search, services, session, transcript, types, ui
    styles/        theme.css (tokens), form-elements.css, tables.css, and friends
    app.html       HTML shell; theme bootstrap and @font-face live here
  static/          Unhashed assets (theme.js, manifest, icons); fonts/ and ffmpeg/ are generated
  scripts/         Build and gate scripts (fonts, ffmpeg, CSP check, bundle budget, i18n parity,
                   test auditor)
  ffmpeg-wasm-build/   Source for the LGPL-only FFmpeg.wasm core
  Dockerfile.dev, Dockerfile.prod, nginx.conf, nginx-pki.conf
```

Routes under `src/routes`: `/` (library gallery), `files/[id]` (file detail), `search`,
`speakers`, `chat/[[conversationId]]`, `file-status`, and the auth pages `login`, `register`,
`forgot-password`, `reset-password`, `accept-invite`, `verify-email`. Settings and admin screens
are not routes; they are sections of `components/SettingsModal.svelte`, loaded lazily through
`lib/settings`. `routes/a11y-fixtures` is test-only.

### Path aliases

Use `$lib` (`src/lib`), `$components` (`src/components`) and `$stores` (`src/stores`) instead of
relative `../../` paths. They are defined in `svelte.config.js` and mirrored in
`vitest.config.ts`.

## Internationalization

Twelve locales: `ar de en es fr it ja ko nl pt ru zh`, in `src/lib/i18n/locales/*.json`. `ar` is
right-to-left; `stores/locale.ts` sets `<html lang>` and `<html dir>` from `lib/i18n/languages.ts`.
Locale files are flat dot-notation key maps, code-split one chunk per language, so never
static-import a locale JSON.

```svelte
<script>
  import { t } from '$stores/locale';
</script>

<button>{$t('common.save')}</button>
```

Every release must ship any new or edited user-facing copy in all 12 locales.
`npm run check:i18n` enforces **key parity only** (every locale has exactly the keys of
`en.json`). It does not check translation: English text copied into a locale passes. A commit that
touches only locale JSON does not trigger the pre-commit hook, so run `npm run check:i18n`
yourself in that case. Check right-to-left rendering in `ar` for layout changes; use logical CSS
properties (`padding-inline-start`, `text-align: start`).

To add a language, add the JSON file, add an entry (with `direction`) to `SUPPORTED_LANGUAGES` in
`src/lib/i18n/languages.ts`, and keep the backend's `LLM_OUTPUT_LANGUAGES` in step.

## Theming

Design tokens are in `src/styles/theme.css`: light values on `:root`, dark values on
`[data-theme='dark']`. `stores/theme.js` (and `static/theme.js`, before first paint) set
`data-theme` on `<html>`. Every UI change needs light and dark parity.

- In a component, write dark overrides as `:global([data-theme='dark'])`, never `.dark`.
- Use the type, weight and colour tokens rather than literals; the app has no purple.

Details and the reasons behind each rule: "Theming" and "Type and weight rules" in `CLAUDE.md`.

## Talking to the backend

- Use the shared axios instance (`import axios from '$lib/axios'`) or a module in `lib/api/`.
  Authentication is by httpOnly cookie (`withCredentials`) plus a `csrf_token` cookie header; there
  is no JavaScript-readable bearer token in the community edition.
- Real-time updates (task progress, notifications) come over the WebSocket opened by
  `stores/websocket.ts` at `/api/ws`.
- The frontend renders what the backend sends (pre-formatted durations, display status, resolved
  speaker names). Business logic belongs in the API.

## Cloud extension seam

`src/lib/cloud` is the single extension point for the commercial managed edition. In this
repository it holds inert no-op stubs; the managed edition replaces the directory at image-build
time. Core code may import only `$lib/cloud` (and `$lib/cloud/components/*`), must gate every call
site with `isCloudEdition` from `$lib/edition` (set at build time by
`VITE_DEPLOYMENT_EDITION=cloud`), and must not name the edition's vendors; CI's Seam Guard
workflow greps for that. Full rules: [`src/lib/cloud/README.md`](src/lib/cloud/README.md).

## Testing

- **Unit and component tests** (Vitest, jsdom): colocated `*.test.ts` files next to the code they
  cover, run with `npm run test`. `npm run test:audit` rejects tests that cannot fail.
- **Bundle budget**: `postbuild` fails the build if the root layout chunk or its static import
  closure grows past its budget (`scripts/check-bundle-budget.mjs`). Move code behind a dynamic
  `import()` rather than raising the budget.
- **End-to-end** (pytest + Playwright, against the running dev stack): lives in
  `backend/tests/e2e/`, not here. Start the stack with `./opentr.sh start dev`, then run
  `./scripts/e2e/run-e2e.sh`. See `backend/tests/e2e/README.md`.

## Environment variables

Build-time variables read through `import.meta.env`:

| Variable                                                                       | Purpose                                                                                   |
| ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| `VITE_DEPLOYMENT_EDITION`                                                      | `cloud` selects the managed edition; anything else is community                           |
| `VITE_API_PROXY_TARGET`, `VITE_MINIO_URL`                                      | Dev-server proxy targets (read by `vite.config.ts`, not shipped)                          |
| `VITE_SENTRY_DSN`, `VITE_SENTRY_ENVIRONMENT`, `VITE_SENTRY_TRACES_SAMPLE_RATE` | Optional error reporting                                                                  |
| `VITE_API_BASE_URL`                                                            | Dev-mode override of the API origin used when building absolute URLs (`lib/utils/url.ts`) |
| `VITE_FLOWER_URL_PREFIX`                                                       | Flower link prefix                                                                        |

Never put secrets in a `VITE_*` variable; Vite inlines them into the client bundle.

## Troubleshooting

- **Dev server won't start or behaves oddly**: remove `node_modules` and re-run `npm install`.
- **`npm run build` fails in `prebuild`**: it downloads fonts and compiles the FFmpeg.wasm core
  (needs Docker buildx or the prebuilt `static/ffmpeg/`); see `scripts/build-ffmpeg.js`.
- **Bundle budget failure**: the gate prints the remaining headroom; see "Bundle budget gate" in
  `CLAUDE.md`.
- **Vitest fails on `localStorage` under Node 26**: you ran vitest without the `npm run test`
  wrapper, which sets `--no-experimental-webstorage`.
- **WebSocket not connecting**: check the backend is up and that the browser origin is allowed by
  the backend's `CORS_ORIGINS`.

## Further reading

- [Svelte](https://svelte.dev/docs) and [SvelteKit](https://svelte.dev/docs/kit) documentation
- [Vite](https://vite.dev/guide/) documentation
- [`docs-site/docs/developer-guide/`](../docs-site/docs/developer-guide) for architecture, testing and
  contributing guides
