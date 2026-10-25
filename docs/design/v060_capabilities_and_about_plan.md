# #1274 + #1273 + #1282: viewer-aware capabilities, build-identity comparison, UI verification pass

> **Owner decisions of 2026-10-25 supersede this plan's open questions:** see `docs/design/v060_owner_decisions.md` (open-source/commercial-licence rule, CJK font pack, governance defaults, gender notice, #1132 moved to v0.9.0, adjustable reranker threads, migration numbering).

Status: **PLAN ONLY.** Nothing here is implemented. Written 2026-10-25 against `master` @ `a236125a`
(worktree `docs/v060-issue-plans`). Line numbers were verified on that commit. They drift, so
search for the quoted symbol rather than trusting a number.

Issues: [#1274](https://github.com/attevon-llc/OpenTranscribe/issues/1274) (capabilities know the
caller), [#1273](https://github.com/attevon-llc/OpenTranscribe/issues/1273) (false About-modal
stale-build banner), [#1282](https://github.com/attevon-llc/OpenTranscribe/issues/1282) (visual
verification pass). All three are on milestone v0.6.0. Related: #868 (resolver fails closed),
#888 (desktop map must fail closed), #862 (About uses `/api/version`), #1266 / #1272 (UI batch).

The three share one plan because two are small and all three touch "what does this tab know about
the server it is talking to". They do **not** share files, so their work packages run in parallel
(section 7).

---

## 1. Verified current state

### 1.1 #1274: capabilities endpoint has no caller identity

- `GET /api/system/capabilities` is `backend/app/api/endpoints/system.py:32-69`. Its dependencies
  are `request` and `db` only. It returns `edition`, `tenancy_mode`, `capabilities`
  (`get_capabilities(request)`), `audience`, `abuse_contact_email`, `max_upload_bytes`,
  `api_mediated_upload_enabled`. It sends **no** `Cache-Control` and **no** `Vary` header.
- `get_capabilities` (`backend/app/core/capabilities.py:243-278`) calls the resolver with the bare
  request and fails closed (#868). At this endpoint `request.state` carries no `user_id` and no
  `external_identity`, because nothing in the dependency tree authenticates. Those are set only by
  `get_current_user` (`api/endpoints/auth/dependencies.py:577-584`, `703-704`) and
  `get_optional_current_user` (`:833-835`, external-token path only).
- Platform staff already **bypass** a disabled capability on the server:
  `require_capability(..., platform_admin_bypass=True)` (`capabilities.py:297-330`) admits
  `user.is_superuser`. The bypass is switched off for `org.takedown` (`router.py:188-196`),
  `speaker_attributes.migration` (`router.py:365-373`), `url_ingest` (`files/__init__.py:160`),
  `audio_extraction` (`user_settings.py:309,366`) and `media_sources` (`user_settings.py:1660,1711,
  1771,1842`). `admin.flower` is checked in the route body with no bypass (`auth/flower.py:69-71`);
  the three `transcription.*` locked keys go through `core/locked_settings.py` with no bypass.
- The convention "platform staff = `is_superuser`" is already written down in code:
  `require_capability`'s docstring and `websockets.py:322-335` (`_may_receive_host_telemetry`).
  `is_superuser` mirrors `role == super_admin` by CHECK constraint (`models/user.py:213-217`).
- Frontend: `admin.takedown` gates the Quarantine and Takedown-notices nav rows only
  (`SettingsModal.svelte:430-436`, inside the `isAdmin` group). The routes behind them are not
  capability-gated. So when a derived resolver turns `admin.takedown` off for tenants, staff lose
  the **nav entry** but keep the API. The issue body is correct about that.
- The SPA fetches capabilities **after** a session exists, with the cookie. `loadCapabilities()` is
  called from 9 places in `stores/auth.ts` (`:395, :436, :448, :604, :762, :994, :1043, :1089,
  :1240`) and reset by `clearUserState`. So an authenticated variant needs no new frontend fetch.
  The docstring at `stores/capabilities.ts:84-88` ("exactly one call site, `routes/+layout.svelte`")
  is stale.

**Corrections to the issue body:**
1. "Platform admin" is ambiguous in this repo. `User.is_admin` (role `admin` or `super_admin`) is
   what #1122 calls a platform admin, but every capability bypass uses `is_superuser`. This plan
   mirrors the server gate (`is_superuser`). See open question Q1.
2. **Adjacent defect found while verifying the "optional auth" option.** `get_optional_current_user`
   checks revocation without `issued_at` (`dependencies.py:862-867`), while `get_current_user`
   passes it (`:659-668`). `issued_at` is what makes the per-user revocation epoch (logout-all,
   password reset, admin termination; `auth/token_service.py:907-962`) cancel a still-valid access
   token. So on every optional-auth path, a token the user explicitly revoked still resolves to the
   user. Today that path serves the `require_capability` bypass and `files/__init__.py:1124`. This
   plan routes the capabilities endpoint through it as well, so the fix goes in first (WP-A1).

### 1.2 #1273: About modal stale-build banner

- `AboutModal.svelte:11` `buildVersion = __APP_VERSION__`. `vite.config.ts:170` defines it as
  `pkg.version` (`frontend/package.json` `"0.5.1"`). The bundle has **no commit identity at all**.
  `frontend/Dockerfile.prod:51-84` declares no `ARG`, and `docker-build-push.sh:614-621` says on
  purpose that the frontend gets no build args.
- `AboutModal.svelte:23-28`: `versionMismatch = serverVersion && serverVersion !== 'unknown' &&
  strip_v(serverVersion) !== strip_v(buildVersion)`. A server reporting `dev-<sha>`
  (`docker-build-push.sh:1285`, `APP_VERSION=dev-<sha>`, `GIT_SHA=<short sha>`, `:635-636`) can
  never match `0.5.1`. That is the bug.
- The modal reads `/api/version` (`AboutModal.svelte:45`, `endpoints/version.py`: `version`,
  `git_sha`, `build_time`), **not** `/health`. `frontend/CLAUDE.md` ("Build identity") still says
  `/health`, and that line is stale.

**Two more defects in the same 60 lines (fold in, per house rule):**
3. `sourceUrl` (`AboutModal.svelte:73-79`): for a `dev-<sha>` server whose `git_sha` is `unknown`
   it builds `/releases/tag/vdev-<sha>`, which is a 404. For a dev stack it builds
   `/tree/<sha>-dirty` (`opentr.sh:134-141` adds `-dirty`), also a 404. The AGPL §13 source offer
   then points nowhere while `sourceIsExact` claims it is exact.
4. `./opentr.sh start prod --build` (`opentr.sh:379-405`, `build_prod_images`) builds the backend
   with **no** `APP_VERSION`/`GIT_SHA`/`BUILD_TIME` build args. The base compose deliberately does
   not inject `APP_VERSION` (`docker-compose.yml:304-316`), so a locally built prod backend reports
   `unknown`. That silently disables both the stale-tab warning and the exact source link.

### 1.3 #1282: what is actually uncovered

- PR #1266 was verified with stubbed data. PR #1272 says outright that the menu-overflow fix was
  "not verified" on **search autocomplete, Plyr menus and the mobile drawer** (static analysis
  only).
- E2E today: `test_responsive.py` checks that the hamburger `.mobile-toggle` *exists*, plus page-level
  overflow (`:97-130`). It never opens the drawer. No e2e test touches
  `SearchAutocomplete.svelte` (`.suggestions-dropdown`, `.suggestion-item`) or the Plyr settings
  menu (`VideoPlayer.svelte:215-241` builds a `settings` control).
- "Real upload, read-only on dev data" contradicts itself as written. The e2e suite already
  solves it: `owned_transcribed_file` / `owned_media_factory` (`backend/tests/e2e/owned_corpus.py`,
  `conftest.py:811`) is a real 10 s upload through the real pipeline (about 38 s), deleted at the
  end of the session.
- The two "inconclusive" staging checks are **not recorded anywhere in this repo**. Only the session
  that ran them can name them (Q3).

---

## 2. Design decisions

### D1 (#1274). Compute a platform overlay after the resolver. Recommended.

| Option | Verdict |
|---|---|
| A. Optional auth, put user/role on `request.state`, let the resolver decide | Rejected as the mechanism. Core cannot test the outcome (the community resolver returns all-true), every derived resolver re-implements the rule, and the resolver is also called from task code with `request=None`, where a role-based answer silently differs. |
| **B. Optional auth in the endpoint, then a core overlay: for a platform-staff caller, every platform-audience key that is bypass-eligible reads `True`** | **Chosen.** One rule, owned and tested in core, identical to what the server gate already admits (`require_capability` bypass). The UI then shows exactly what the server lets staff use, no more. |
| C. A second endpoint (`/system/capabilities/me`) | Rejected. Two maps drift, and the SPA would need to merge them. |

Rules of the overlay (all in `core/capabilities.py`):
- **Who:** `is_platform_staff(user)` means `user is not None and user.is_active and user.is_superuser`.
  This is one function, and `require_capability`'s bypass is refactored to call it, so the overlay
  and the gate cannot diverge.
- **Which keys:** `PLATFORM_OVERLAY_KEYS = {k for k, a in CAPABILITY_AUDIENCE.items() if a ==
  AUDIENCE_PLATFORM} - NO_PLATFORM_BYPASS`.
- **New declared set** `NO_PLATFORM_BYPASS: frozenset[str]` = `org.takedown`,
  `speaker_attributes.migration`, `url_ingest`, `audio_extraction`, `media_sources`,
  `admin.flower`, `transcription.model_choice`, `transcription.diarization_source`,
  `transcription.advanced`. `require_capability(key, platform_admin_bypass=False)` raises
  `ValueError` at import time when `key` is not in the set. Router registration happens at import,
  so a new no-bypass gate cannot be added without declaring it, and the overlay can never flip a
  deployment-locked key.
- **User / team / org-admin keys are never overlaid**, even where the server bypass would admit
  staff (`watch_sources`, `chat.rag`, `organizations`). Those describe the tenant's product, and the
  audience taxonomy says platform staff are not org admins (`capabilities.py:85-95`). See Q2.
- **The overlay applies whatever the resolver did**: omitted, `False`, raised, or returned a
  non-dict. The server bypass behaves the same way, so the UI stays truthful.
- **Lifecycle states** (expired, `must_change_password`, unapproved) are not re-checked. The SPA
  already renders the remedy screen for those states, and the admin routes chain through
  `get_current_active_user`. The overlay only adds nav entries for surfaces the routes still guard.
- **Support-access grant header is ignored.** The overlay describes the staff member's own
  surfaces. Under a grant, `/admin` is refused by prefix (`refuse_under_support_grant`), so this
  opens nothing.

### D2 (#1274). Anonymous and failure behaviour: fail closed to the tenant view

No token, an invalid, expired or revoked token, an inactive user, or any exception inside
`get_optional_current_user` all give `None`, so no overlay is applied. The response is then
byte-identical to today's. The endpoint never returns 401 (it stays on the public list in
`tests/unit/test_route_privilege_tiers.py:106`).

### D3 (#1274). Caching: per-viewer response, never shared

The response now depends on the caller. It already did in a tier-scoped derived deployment, but
nothing declared it. Add `Cache-Control: private, no-store` and `Vary: Cookie, Authorization`.
The shipped nginx configs define no cache zone at all: there is no `proxy_cache_path` in `nginx/`
or `frontend/*.conf`, and the only `proxy_cache` directive is `off` on the SSE location
(`nginx/site.conf.template:137`). So this changes nothing for core deployments. It protects any edge cache a derived deployment puts in front, where a cached
staff variant would otherwise be served to tenants. **No new response field.** The shape stays
byte-compatible (#888 constraint).

### D4 (#1273). Compare commits when both sides know theirs, otherwise release versions, otherwise stay silent

| Option | Verdict |
|---|---|
| Skip the banner whenever the server is `dev-*` | Rejected on its own. It hides a real stale tab across two dev images, and that is exactly the case where staleness happens most. |
| **Embed the build commit in the bundle (`__GIT_SHA__`), compare commit to commit, fall back to release semver, otherwise `unknown` (no banner)** | **Chosen.** Correct for every shape, and it degrades to silence, matching the existing rule that "a false warning is worse than no warning" (`AboutModal.svelte:18-22`). |
| Serve a build-info JSON next to the bundle | Rejected. nginx serves the *current* file, not the tab's, so it cannot detect a stale tab. |

Pure helpers in a new `frontend/src/lib/utils/buildIdentity.ts`:

```ts
export interface BuildIdentity { version?: string | null; commit?: string | null }
export type BuildComparison = 'match' | 'mismatch' | 'unknown';
export type ParsedVersion = { kind: 'release'; semver: string } | { kind: 'dev'; commit: string } | { kind: 'unknown' };
export type SourceRef = { kind: 'commit'; sha: string; exact: boolean } | { kind: 'release'; tag: string } | { kind: 'none' };
export function parseBuildVersion(v: string | null | undefined): ParsedVersion;   // /^v?(\d+\.\d+\.\d+)$/i -> release; /^dev-([0-9a-f]{7,40})$/i -> dev
export function normalizeCommit(c: string | null | undefined): { sha: string; dirty: boolean } | null; // lowercases, strips "-dirty", needs [0-9a-f]{7,40}
export function compareBuilds(client: BuildIdentity, server: BuildIdentity): BuildComparison;
export function sourceRefFor(server: BuildIdentity): SourceRef;
```

`compareBuilds` works in this order: (1) effective commit per side = `normalizeCommit(commit)`,
else the commit inside a `dev-<sha>` version. If both sides have one, the result is `match` when
one is a prefix of the other (short vs full SHA), otherwise `mismatch`. (2) Otherwise, if both
versions parse as release, compare the semver. (3) Otherwise `unknown`. `sourceRefFor` uses the
same effective commit. A dirty commit gives `exact: false`, which drives the existing
`sourceIsExact=false` copy. It never emits `/releases/tag/vdev-…`.

**Where the client commit comes from:** only the prod image. `frontend/Dockerfile.prod` gets
`ARG GIT_SHA=unknown` placed immediately before `RUN npm run build` (`COPY . .` already busts the
cache per commit), and `vite.config.ts` defines `__GIT_SHA__` from `process.env.GIT_SHA`. The Vite
dev container stays `unknown` **on purpose**: dev bind-mounts frontend code while the backend
image's baked `GIT_SHA` lags it, so a commit comparison would cry wolf after every `git pull`.
Dev falls back to `v0.5.1` vs `0.5.1`, which matches.

### D5 (#1282). Make the three uncovered surfaces permanent e2e tests, then do one headed pass

A one-off look decays. The overflow and flicker bug class has a measurable invariant: the panel's
`scrollWidth <= clientWidth`, and a row's bounding rect is unchanged on hover. So the uncovered
surfaces become e2e tests (WP-C1), and the human headed pass (WP-C2) covers only what an assertion
cannot judge: typography, colour, RTL feel. No dev-library data is read or changed. Everything
uses the owned corpus.

---

## 3. File-by-file changes

### WP-A1: optional-auth revocation epoch (#1274 prerequisite)
- `backend/app/api/endpoints/auth/dependencies.py:862-867`: pass `issued_at=payload.get("iat")` to
  `token_service.is_token_revoked`, the same as `get_current_user`.
- Test: `backend/tests/api/test_optional_auth_revocation_epoch.py` (below).

### WP-A2: viewer-aware capabilities (#1274)
- `backend/app/core/capabilities.py`: add `NO_PLATFORM_BYPASS`, `PLATFORM_OVERLAY_KEYS`,
  `is_platform_staff(user)`, `get_capabilities_for_viewer(request, user) -> dict[str, bool]`
  (`get_capabilities(request)` with the overlay applied, as a new dict). In `require_capability`:
  raise `ValueError` for an undeclared no-bypass key, and replace the inline `is_superuser` check
  with `is_platform_staff`. Update the module docstring's tier list to describe the overlay.
- `backend/app/api/endpoints/system.py:32-69`: add `response: Response` and
  `current_user: User | None = Depends(get_optional_current_user)`. Call
  `get_capabilities_for_viewer`, and set the two headers from D3. The keys and shape stay the same.
- `backend/app/auth/constants.py:123`: bump `CLOUD_SEAM_VERSION` to the next free number
  (currently 10; re-check at implementation, since other v0.6.0 work bumps it too). Add a changelog
  entry: "platform-audience keys a resolver denies now read True for `is_superuser` callers on
  `/system/capabilities`, except `NO_PLATFORM_BYPASS`. The endpoint resolves the caller optionally,
  so the resolver can now see `request.state.user_id` / `external_identity` there. The response is
  `private, no-store`."
- `backend/app/core/CLAUDE.md` (capabilities bullet) and the `capabilities.py` docstring: document
  the overlay and the `NO_PLATFORM_BYPASS` rule.
- `frontend/src/stores/capabilities.ts:84-88`: fix the stale call-site sentence (comment only).
  No other frontend change. `isCapabilityEnabled` and SettingsModal already render what the map says.

### WP-B1: comparison helpers + About modal (#1273)
- New `frontend/src/lib/utils/buildIdentity.ts` + `buildIdentity.test.ts`.
- `frontend/src/components/AboutModal.svelte:9-80`: drop `normalizeVersion`. Compute
  `versionMismatch = compareBuilds({version: __APP_VERSION__, commit: __GIT_SHA__},
  {version: serverVersion, commit: serverCommit}) === 'mismatch'`, and derive
  `sourceUrl`/`sourceIsExact` from `sourceRefFor`. Render the client build as `0.5.1 (abc1234)` when
  `__GIT_SHA__` is known. No new strings.
- `frontend/vite.config.ts:163-172`: `__GIT_SHA__: JSON.stringify(process.env.GIT_SHA?.trim() ||
  'unknown')`. `frontend/vitest.config.ts:18-19`: `__GIT_SHA__: JSON.stringify('unknown')`.
  `frontend/src/vite-env.d.ts:42-44`: `declare const __GIT_SHA__: string;`.
- New `frontend/src/components/AboutModal.versionBanner.test.ts`.

### WP-B2: build plumbing (#1273)
- `frontend/Dockerfile.prod`: add `ARG GIT_SHA=unknown` and `ENV GIT_SHA=${GIT_SHA}` directly above
  `RUN npm run build` (`:84`). Build stage only; the nginx stage is unchanged.
- `scripts/docker-build-push.sh` `build_frontend` (`:787+`): pass `--build-arg GIT_SHA=${COMMIT_SHA}`,
  the **same** variable the backend gets (`:636`). Update the "Deliberately NOT applied to the
  frontend" comment (`:614-621`).
- `opentr.sh` `build_prod_images` (`:379-405`): pass `APP_VERSION`, `GIT_SHA`, `BUILD_TIME` to the
  backend build and `GIT_SHA` to the frontend build. Both are already exported at `:123-141`.
- `scripts/release/check-version-consistency.py` (`:204-212` pattern): also assert
  `frontend/Dockerfile.prod` declares `ARG GIT_SHA`.
- `frontend/CLAUDE.md` "Build identity": `/health` becomes `/api/version`, plus the comparison rule.

### WP-C1: e2e coverage for the uncovered surfaces (#1282)
- New `backend/tests/e2e/test_floating_menus.py`, marker `visual`. Parametrize viewports
  `{1440x900, 1180x800, 390x844}` × theme `{light, dark}` × locale `{en, ar}` where it is cheap
  (theme and locale via the existing settings and locale helpers, never by mutating the dev user's
  stored preferences; restore them if the helper writes). Tests:
  - `test_search_autocomplete_panel_has_no_horizontal_overflow`: type into `.search-input` on
    `/search` using a term from `owned_transcribed_file`. Assert `.suggestions-dropdown`
    `scrollWidth <= clientWidth` and that the `.suggestion-item` rect is unchanged under hover.
  - `test_plyr_settings_menu_has_no_horizontal_overflow`: on the owned file's detail page, click
    the Plyr settings control, open each submenu, and assert the same two invariants on the menu
    container (confirm the selector in the DOM; Plyr's is `.plyr__menu__container`).
  - `test_mobile_drawer_opens_closes_and_does_not_overflow` (390 only): click `.mobile-toggle
    button`, assert `aria-expanded="true"`, no horizontal page overflow, and that the bell closes the
    drawer (#1266 behaviour). Then `.mobile-overlay` click closes it.
  - `test_upload_shows_one_processing_card`: open the bell during `owned_media_factory`'s upload.
    Exactly one card per file (`.job-file` count == 1), `.job-state` advances, and nothing remains
    after cleanup.
- Uses `owned_corpus` / `owned_media_factory` only. Deletes what it creates.

### WP-C2: headed pass + record (#1282)
- No code. Run with `DISPLAY=:11 pytest backend/tests/e2e/test_floating_menus.py -v --headed`, then
  a scripted screenshot sweep (Playwright via the e2e fixtures, or `~/bin/browser-tools`;
  `browse.js` is pinned to 1920x1080 at `browse.js:97`, so use a Playwright script for the three
  widths). Checklist, each at 1440/1180/390 × light/dark × `en`/`ar`:
  1. User dropdown and the Export / Download / Process / Organize menus: no horizontal scrollbar,
     no hover flicker.
  2. About modal: no banner on a fresh tab against the local stack. Banner present when the backend
     image is rebuilt from a newer commit and the tab is not reloaded (needs WP-B1/B2).
  3. Search: result cards, sticky info card, play/pause follows the player.
  4. Chat: markdown, code (copy and language label), math, answer-length setting showing the real
     default and ceiling. Use the mock LLM overlay so no GPU is involved.
  5. Search autocomplete, Plyr menus, mobile drawer (the WP-C1 surfaces, by eye).
  6. Single notification card during an owned upload.
  7. The two staging checks named by the owner (Q3).
- Record a pass/fail table on #1282. Keep screenshots out of the repo and out of the public issue
  unless they show only owned-corpus content (Q4). File one issue per defect, with the area labels
  and board fields per root `CLAUDE.md`.

---

## 4. Security and tenant isolation

| Surface | What could leak | Guard | Negative test |
|---|---|---|---|
| Overlay granted to the wrong caller | A tenant (user, org admin, plain `admin`) sees platform nav | `is_platform_staff` = active superuser; tenants are never superusers (external sync never grants it) | `test_overlay_denied_to_user_admin_and_anonymous` |
| Revoked staff token | Overlay after logout-all or password reset | WP-A1 epoch fix | `test_optional_user_rejects_token_issued_before_revocation_epoch`, `test_revoked_super_admin_token_gets_no_overlay` |
| Overlay flips a deployment-locked key | Staff UI offers a control the server refuses | `NO_PLATFORM_BYPASS` + import-time `ValueError` | `test_no_bypass_keys_are_never_overlaid`, `test_require_capability_rejects_undeclared_no_bypass_key` |
| Overlay leaks tenant-product keys | Staff appears to have a tier's feature | Overlay is platform-audience only | `test_non_platform_keys_are_never_overlaid` |
| Shared cache | A staff variant served to tenants by an edge cache | `private, no-store` + `Vary` | `test_capabilities_response_is_private_and_varies_on_credentials` |
| Disclosure through the map | A tenant learns which keys staff have | None needed. The overlay is applied only to the staff member's own response, and the key list is already public (`audience`) | covered by the deny tests |
| Opened surface | None. The overlay changes UI only; every route keeps its own dependency | n/a | existing `test_cloud_seams_capabilities.py` route tests stay green |

#1273 has no security surface beyond the AGPL source offer becoming *more* exact. A commit SHA is
already public on `/api/version`.

## 5. Frontend: theme, i18n, RTL, bundle

- No new user-facing strings in any WP, so `check:i18n` parity is unaffected. The `(abc1234)`
  suffix is a value, not copy. In `ar`, wrap the version and commit span in `dir="ltr"` so the
  parentheses do not mirror.
- No CSS change, so light/dark parity is unaffected. WP-C1/C2 assert both themes.
- Bundle: `AboutModal` is already lazy-loaded (#1266), and `buildIdentity.ts` is imported only by
  it, so it costs nothing in the root layout (~16.7 of 22.5 KB) or the closure (~128.8 of 139.3 KB).
  Confirm in the `postbuild` gate output that root and closure are unchanged.
- #1274 changes no frontend code.

## 6. Test plan (red first)

Prove red on a HEAD tree, never by swapping files in the shared checkout:

```bash
RED="$(mktemp -d -t ot-redcheck.XXXXXX)" && git archive HEAD | tar -x -C "$RED"
cp <new test files> "$RED/<same paths>"
# backend: cd "$RED/backend" && /mnt/nvm/repos/transcribe-app/backend/venv/bin/pytest -v <files>
# frontend: ln -s "$PWD/frontend/node_modules" "$RED/frontend/node_modules" && cd "$RED/frontend" && npm run test -- <files>
```

| Test (file) | Red on HEAD because |
|---|---|
| `test_optional_user_rejects_token_issued_before_revocation_epoch` (`tests/api/test_optional_auth_revocation_epoch.py`): mint token, bump the user's epoch (logout-all), call `get_optional_current_user` | HEAD returns the user |
| `test_platform_key_denied_by_resolver_is_shown_to_super_admin` (`tests/api/test_system_capabilities_viewer.py`): resolver `{**COMMUNITY_CAPABILITIES, "admin.takedown": False}`, `super_admin_token_headers` | HEAD returns `False` |
| `test_overlay_applies_when_resolver_raises` (same file): super_admin gets `PLATFORM_OVERLAY_KEYS` True, everything else False | HEAD all False |
| `test_capabilities_response_is_private_and_varies_on_credentials` (same file) | HEAD sends no headers |
| `test_require_capability_rejects_undeclared_no_bypass_key` (`tests/unit/test_capability_overlay.py`) | HEAD accepts it |
| `test_revoked_super_admin_token_gets_no_overlay` (viewer file) | Red only with A2 applied and A1 not, which proves the dependency between them |
| Controls, green before and after (stated as controls, not red-first): `test_overlay_denied_to_user_admin_and_anonymous`, `test_no_bypass_keys_are_never_overlaid`, `test_non_platform_keys_are_never_overlaid`, `test_anonymous_response_identical_to_resolver_output`, `test_platform_overlay_keys_are_platform_audience_minus_no_bypass` | Their job is to fail the day the overlay over-reaches |
| `buildIdentity.test.ts`: `it.each` over the cases below | Module absent on HEAD; the component test below is the behavioural red |
| `AboutModal.versionBanner.test.ts`: server `{version:'dev-abc1234', git_sha:'abc1234'}` gives no `.version-mismatch` | HEAD shows the banner |
| Same file: `{version:'dev-abc1234', git_sha:'unknown'}` gives source link `/tree/abc1234` | HEAD links `/releases/tag/vdev-abc1234` |
| Same file, control: `{version:'v9.9.9', git_sha:'unknown'}` shows the banner | Green on both trees; guards against a "never show it" fix |
| `test_check_version_consistency_requires_frontend_git_sha_arg` (extend that script's existing test, if present; otherwise a scoped unit test) | HEAD has no ARG |

`compareBuilds(client, server)` table (client is `__APP_VERSION__`/`__GIT_SHA__`):

| client | server | expected |
|---|---|---|
| `0.6.0`/unknown | `v0.6.0`/unknown, and `0.6.0`/unknown | match |
| `0.5.1`/unknown | `v0.6.0`/unknown | mismatch |
| `0.5.1`/unknown | `dev-abc1234`/`abc1234` | **unknown** (the #1273 case) |
| `0.5.1`/`abc1234` | `dev-abc1234`/`abc1234`; `dev-abc1234`/unknown; `DEV-ABC1234`/unknown | match |
| `0.5.1`/`abc1234` | `dev-def5678`/`def5678` | mismatch |
| `0.6.0`/`abc1234` | `v0.6.1`/`abc1234` (different tag, same commit) | match |
| `0.6.0`/`abc1234` | `v0.6.0`/`def5678` (same tag, rebuilt) | mismatch |
| `0.6.0`/`abc1234` | `v0.6.0`/40-char SHA starting `abc1234` | match |
| `0.6.0`/`abc1234-dirty` | `v0.6.0`/`abc1234` | match |
| any | `unknown`/`unknown`; `''`/`''`; `undefined`/`null`; `latest`/unknown; `v0.6`/unknown; `v0.6.0-rc1`/unknown | unknown (rc: release-only regex; the build script rejects rc tags anyway) |
| `0.6.0`/`abc` (too short) | `v0.7.0`/unknown | mismatch (short commit treated unknown, falls to semver) |

Plus `sourceRefFor` cases: commit, dev-derived commit, dirty (`exact:false`), release, none.

WP-C1 tests are e2e (live stack, `./opentr.sh start dev`). Their red proof is the bug they guard:
re-add `transform: scale(1.02)` to `button:hover` in a `git archive` tree served by a `--fresh`
stack, or simply note that `test_mobile_drawer…` fails if the bell-closes-drawer handler is
removed. A `--fresh` proof is optional; do not hot-swap CSS in the shared dev stack.

Gates per WP: `cd backend && python3 ../scripts/audit-tests.py tests` reaches 0 open findings, and
`npm run test:audit` likewise. Both pre-commit tiers via `scripts/safe-precommit.sh`.

## 7. Work packages

| WP | Size | Depends on | Files (disjoint across parallel WPs) | Parallel? |
|---|---|---|---|---|
| A1 optional-auth epoch fix | S | none | `auth/dependencies.py`, one new test | yes |
| A2 viewer-aware capabilities + seam bump + docs | M | A1 merged (or the same PR, A1 commit first) | `core/capabilities.py`, `endpoints/system.py`, `auth/constants.py`, `core/CLAUDE.md`, `stores/capabilities.ts` (comment), 2 new tests | after A1 |
| B1 buildIdentity + AboutModal | S | none | `lib/utils/buildIdentity*`, `AboutModal.svelte`, `vite.config.ts`, `vitest.config.ts`, `vite-env.d.ts`, 1 new component test | yes |
| B2 build plumbing | S | none (B1 works without it and falls back to semver) | `frontend/Dockerfile.prod`, `scripts/docker-build-push.sh`, `opentr.sh`, `check-version-consistency.py`, `frontend/CLAUDE.md` | yes |
| C1 e2e floating menus + upload card | M | none | `backend/tests/e2e/test_floating_menus.py` | yes |
| C2 headed pass + record | S | B1, B2, C1 merged; Q3 answered | none (issue comment) | last |

A1, B1, B2 and C1 can run as four agents in **separate worktrees** (each commits its own branch;
never several writers on one branch). Editing `.py` under `backend/app` reloads the shared dev
backend, so A1/A2 should batch their app edits and avoid the window while C1 runs e2e.

## 8. Rollout and compat for derived deployments

- **Capabilities:** a resolver's `False` for a platform-audience key now hides the surface from
  tenants only. Superusers see it, except keys in `NO_PLATFORM_BYPASS`. The server already admitted
  superusers to those routes, so no new access exists; the UI just stops under-reporting.
- At `/system/capabilities` the resolver may now find `request.state.user_id` (and
  `external_identity` for external tokens). This is additive. Resolvers must still handle an
  anonymous request and `request=None` (task-time reads).
- The response is `private, no-store` with `Vary: Cookie, Authorization`. Remove any edge-cache rule
  that cached it.
- Bump the seam version (section 3). A derived build pinned at the old version should review the
  overlay before upgrading.
- **Build identity:** pass the **same** `GIT_SHA` to the frontend and backend builds, or pass it to
  neither. Mismatched sources produce a permanent banner. A derived frontend built without the ARG
  keeps semver behaviour, and dev images then read as `unknown` (silent). A replaced `$lib/cloud`
  is unaffected: AboutModal is not part of the seam.

## 9. Verification and exit criteria

- #1274: with a test resolver that denies `admin.takedown`, a super_admin's Settings shows
  Quarantine and Takedown notices; an admin, a user and an anonymous `curl` do not get `True`.
  `curl -sI` shows `Cache-Control: private, no-store`. Community build: the response body is
  identical to before for every role (`diff` of the JSON).
- #1273: local stack, fresh tab, no banner. `./opentr.sh start prod --build` then
  `curl /api/version` gives a real `version`/`git_sha`, and the About modal shows `0.5.1 (<sha>)`,
  no banner, and a source link to `/tree/<sha>`. A dev-image server gives no banner.
- #1282: WP-C1 green in the e2e gate (`./scripts/e2e/run-e2e.sh -m visual`), checklist table posted,
  every defect filed, Q3's two checks closed out, and the issue closed.
- All: both pre-commit tiers, `audit-tests` 0, `test:audit` 0, bundle gate unchanged.

## 10. Open questions (owner)

- **Q1. Who counts as platform staff for the overlay?** Default: **`super_admin` only**
  (`is_superuser`), the same predicate as the existing `require_capability` bypass and the WS
  telemetry gate. Widening to role `admin` would also widen the server bypass, which is a separate
  security decision.
- **Q2. Should the overlay also cover the non-platform keys the server bypasses for superusers**
  (`watch_sources`, `chat.rag`, `organizations`, ...)? Default: **no**. Those are the tenant's
  product and administration, not staff tools.
- **Q3. Which two staging UI checks were inconclusive?** Only the session that ran them knows.
  Default if there is no answer: WP-C2 reruns all ten headed checks locally.
- **Q4. Screenshots on the public issue?** Default: post the pass/fail table only, plus screenshots
  that show owned-corpus content alone. Never post dev-library content.
