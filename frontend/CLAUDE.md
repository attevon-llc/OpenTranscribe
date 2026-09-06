# frontend — OpenTranscribe SvelteKit SPA

Orientation for the web client. Repo-wide rules live in the root `CLAUDE.md`; this file
is frontend-specific. Folder-level `CLAUDE.md` files add detail where you're working.

## Stack

- **SvelteKit 2 + Svelte 5 + TypeScript + Vite 6**, `@sveltejs/adapter-static` → a pure
  **client-side SPA** (`fallback: index.html`, no SSR, no `+page.server.ts`). Data is fetched
  client-side (`onMount`/reactive) from the FastAPI backend.
- i18n via i18next (**12** locales in `src/lib/i18n/locales`: ar de en es fr it ja ko nl pt ru zh
  — `ar` is RTL, driven by `document.documentElement.dir` from `stores/locale.ts`). This said
  11 and called `it` the one language the UI lacked; `it.json` exists and `it` is registered in
  `lib/i18n/languages.ts`, so the count and the caveat were both stale.
  Node pinned to 26 (`.nvmrc`).
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
  ⚠️ **Those scripts set `NODE_OPTIONS=--no-experimental-webstorage`, and it is load-bearing on
  Node >=26.** Node 26 defines a `localStorage` accessor on `globalThis` that evaluates to
  `undefined` without `--localstorage-file`; vitest's `populateGlobal` skips any jsdom window key
  already present on `globalThis` (`localStorage` is in neither its KEYS nor jsdom's
  additionalKeys), so jsdom's real `Storage` is never installed and Node's `undefined` wins —
  15 failures in `FileUploader.test.ts` at `localStorage.clear()`. The flag removes the global so
  jsdom installs the real thing. **Do not replace it with a hand-rolled Storage shim**:
  `clearUserState`, `auth.logoutOrdering` and `txtExportPrefs` all `vi.spyOn(Storage.prototype, …)`,
  which cannot hook an object that doesn't inherit from it. Measured equivalent — node:26+flag and
  node:24 both 189 files / 1747 passed. Enforced by
  `backend/tests/unit/test_node_version_consistency.py`, gated on `.nvmrc` so it lapses if we ever
  drop below 26. If you run bare `npx vitest` on Node 26 you will hit this.
- `npm run test:audit` — finds tests that pass whether the code works or not (below).
- **Before committing**: lint + svelte-check + build + test must be green (pre-commit enforces
  it via `scripts/frontend-check.sh`).

## Test quality gate — `npm run test:audit`

`scripts/audit-frontend-tests.mjs` is the vitest counterpart of `scripts/audit-tests.py`
(pytest). It parses every `src/**/*.{test,spec}.{ts,js}` with the **TypeScript compiler API**
(already a devDependency — do not swap it for a regex or a new parser) and exits non-zero on
any un-allowlisted finding, so it can gate a commit. Detectors: `only-leak`, `skipped-test`,
`no-assertion`, `unfalsifiable`, `weak-only`, `conditional-only`, `conditional-skip`,
`floating-async-assertion`, `mock-heavy`, `mock-only`.

- **Allowlist**: `frontend/test-audit-allowlist.txt`, one
  `<file>::<full test name>::<category>  # reason` per line. The **category is required** —
  an entry keyed only by test would exempt that test from every detector at once. So is the
  reason. Never allowlist `only-leak`; delete the `.only`.
- **`npm run test:audit:selftest` runs the auditor against in-memory fixtures** — 14 cases
  that must fire and 7 clean cases that must not. Run it after touching any detector: it
  already caught `conditional-skip` silently matching nothing (it only looked at the `then`
  branch, and `.pos` vs `.getStart()` made the early-return variant a no-op). A detector that
  cannot fire is indistinguishable from a clean suite.
- Two calibration traps, both load-bearing: Testing Library's `getBy*`/`findBy*` **throw**, so
  they are assertions (ignore that and a third of the component suite reads as assertion-free),
  and `expect.arrayContaining(...)` is a matcher **argument**, not an assertion head.
- The auditor counts source-level tests, so its total is lower than vitest's whenever
  `it.each` is used (one source test → N cases). That is the only legitimate difference; any
  other gap means it is blind to a file.

**Vitest environment is `jsdom` for every file, deliberately.** Switching the ~25 pure-logic
files to `environment: 'node'` measures ~1.3 s faster, and was rejected: this is a
client-only SPA, several modules under test branch on `typeof window === 'undefined'`
(`$lib/utils/url`, `$lib/axios`), and a test running the SSR branch of code that has no SSR
in production passes while proving nothing. Correctness over 1.3 s.

## Path aliases (never use `../../`)

`$lib` → `src/lib` · `$components` → `src/components` · `$stores` → `src/stores`.

## Architecture rule: thin frontend, fat backend

The frontend renders backend data and captures input. Business logic, aggregation, and
domain formatting belong in the API. The backend already sends pre-formatted display fields
(`formatted_duration`, `display_status`, `resolved_speaker_name`, analytics, …) — render those,
don't recompute. ⚠️ **The former "approved client-side exception" for purely-presentational
transforms on already-downloaded data (TXT/SRT/VTT/CSV export) is retracted — it is the
mechanism of a live security bug, issue #673.** A client-side serializer cannot enforce a
server-side policy: five of the six user-clickable export formats build transcript text in
the browser, so none of them ever consult the server-side `export_locked` admin lock. An
admin can mandate censored exports and every SPA export button still writes the unredacted
original to disk. Transcript export is being moved server-side to close this. Do not add a
second client-side exporter believing this exception still applies.

## dev (Vite) vs prod (nginx) — both must work

- **dev**: `./opentr.sh start dev` bind-mounts source for HMR; the app is served by Vite at :5173.
- **prod**: `frontend/Dockerfile.prod` builds static assets served by **nginx** (`nginx.conf`,
  `nginx-pki.conf`) with CSP/HSTS/security headers. Any change touching `app.html`, asset paths,
  env (`import.meta.env.VITE_*`), or CSP must be verified in BOTH. No secrets in the bundle.
- **No service worker, deliberately.** This is an authenticated always-online SPA, so a worker
  buys nothing and risks plenty: a cache-first worker would cache the app shell _and_ same-origin
  `/s3/` media, and Cache Storage is not cleared by `$lib/session/clearUserState`. Chrome dropped
  the service-worker requirement for installability (M108/M112), so the manifest alone keeps the
  PWA installable. `$lib/serviceWorkerCleanup` unregisters strays on boot — don't re-add one.
- **Anything in `static/` is unhashed** and nginx caches `*.js`/`*.css`/images for a year. `app.html`
  busts `/theme.js` with a `?v=` content digest (`%sveltekit.env.PUBLIC_THEME_VERSION%`, computed in
  `vite.config.ts`); a new unhashed asset needs the same treatment. `static/fonts/` and
  `static/ffmpeg/` are gitignored and populated by the `prebuild` scripts (`download-fonts.js`,
  `build-ffmpeg.js`) — a clean checkout must still produce a complete image. `static/ffmpeg/` is
  no longer fetched from a CDN: `build-ffmpeg.js` compiles a minimal, LGPL-2.1+-only FFmpeg.wasm
  core via `ffmpeg-wasm-build/` (issue #473 — the published `@ffmpeg/core` is GPL 2+, not just
  license-ambiguous). `Dockerfile.prod` compiles the same core as its own build stage, so prod
  images ship fully self-contained with no CDN dependency at all.
- **Build identity**: `__APP_VERSION__` / `__BUILD_TIME__` are `define`d in `vite.config.ts` (and
  mirrored in `vitest.config.ts`). The About dialog compares `__APP_VERSION__` against the
  backend's `/health` version so a stale cached tab is visible rather than silent.

## Where things live

- `src/components/ui` — shared primitives (see its CLAUDE.md). `src/lib/utils` — pure helpers
  (time formatting lives ONLY in `formatting.ts`). `src/lib/api` — typed API clients.
  `src/stores` — Svelte stores. `src/routes` — pages.
- `src/components/chat` — RAG chat surface (see its CLAUDE.md). Assistant output renders
  through `renderChatMarkdown`'s dedicated DOMPurify profile, which blocks relative URLs so
  model text can never mint an app-internal link. Never route it through
  `sanitizeHighlightHtml` instead.
  **It is not the only model-authored HTML, though** — `SummaryDisplay.svelte` and
  `TopicsList.svelte` render LLM summaries, key decisions, follow-ups and topics through
  `sanitizeHighlightHtml`, the weaker profile. That is tolerable only because that profile
  allows no `a`/`href`/`src`/`style` at all, so model text cannot mint a link there either;
  `sanitizeHtml.test.ts` pins exactly that. Adding `a` or `href` to
  `HIGHLIGHT_ALLOWED_TAGS`/`_ATTR` would hand the LLM an app-internal link surface — route
  summaries through `renderChatMarkdown` first if you ever need links there.
- `src/lib/cloud` — **managed-edition seam stub** (see its README). The commercial repo replaces
  this directory at image-build time. Core code imports only `$lib/cloud` (+ its `components/`),
  gates every call site with `isCloudEdition` from `$lib/edition`, and must never name the
  edition's vendors — CI's seam-guard greps `frontend/src` (and `backend/app`) for `clerk|stripe`
  and fails the build on a match.

## Theming — two rules that have both already shipped a bug (issue #746)

Tokens live in `src/styles/theme.css`; the light values are on `:root`, the dark ones on
`[data-theme='dark']`. `src/stores/theme.js` sets **`data-theme` on `<html>`** and
`theme-<light|dark>` on `<body>`.

- **The dark-mode selector is `:global([data-theme='dark'])`, never `:global(.dark)`.** No
  element in this app is ever given the class `dark`, so 34 components had written their entire
  dark-mode override against a selector that could not match: the light value stayed applied in
  dark mode and nothing failed. `src/styles/theme-parity.test.ts` now fails on any new one.
- **A global element rule can out-specify a component's scoped rule, so don't set colours on
  `button:<pseudo-class>` in `src/styles/form-elements.css`.** Svelte compiles `.foo { … }` to
  `.foo.svelte-HASH` — specificity (0,2,0) — which `button:focus:not(:disabled)` (0,2,1) beats.
  That is exactly how #746 happened: the global focus rule repainted `background-color`, browsers
  put `:focus` on a button on mouse-down, and every icon button styled
  `.foo { background: <solid>; color: white }` turned into a white glyph on a near-transparent
  background — invisible in light mode — as soon as it was clicked. Focus feedback is an
  `outline` on `:focus-visible` now; keep it that way.

Hand-rolled icon buttons are where this recurs, because they are copied rather than reused.
Prefer the `ui/` primitives (`SearchBar`'s clear/next controls, `CopyButton`, `Chip`,
`BaseModal`'s header close) over re-declaring the pattern.

## Verify UI changes in a browser (light + dark) — type-check ≠ feature-check.
