# Issue #1327: Replace Plyr (security-updates-only) with a maintained player: Implementation Plan

Status: PLAN ONLY, not started. Written 2026-11-07 for issue
[#1327](https://github.com/attevon-llc/OpenTranscribe/issues/1327)
(`chore(frontend): replace Plyr, which now only receives security updates`, milestone v0.7.0,
labels `enhancement` `frontend` `dependencies`). A fresh agent with no conversation memory should be
able to execute this end to end from this file alone.

**Verified against base commit `66e3e1f68`** (`docs(changelog): timed and general comments`),
2026-11-07. Every `file:line` below was read from that tree. Re-derive with `git rev-parse HEAD`
before trusting line numbers; `VideoPlayer.svelte` and the file-detail page are edited often.

**Owner requirement (2026-11-07):** "Find a proper replacement that has the same features and
capabilities or more." Section 3 is the proof: a candidate is eligible only if it is "yes" on every
row where Plyr is "yes" today.

---

## 0. Summary

- **Recommendation: Video.js 10 (`@videojs/html`), composed from its individual UI elements
  (`@videojs/html/ui/*`) inside our own Svelte layout, behind a small app-owned `PlayerHandle`
  seam.** It is the project Plyr's own deprecation notice points to, it is built by the people who
  built Plyr, Vidstack, Media Chrome and Video.js, it is Apache-2.0, and it is the only candidate that
  is both actively developed and covers every capability the app uses.
- Out of the box Video.js 10 is "yes" on 35 of the 41 Plyr-"yes" rows. The other six need small,
  documented app-side code (the library's own docs say to write it in the app): remembered
  preferences, captions-on-by-default from that remembered choice, speaker-styled caption
  rendering in Firefox, the `L` loop shortcut, the 4x speed option, and the mini player's
  full-window fullscreen fallback. Each gets a test that fails first,
  in the lane that builds it. With those in place it is "yes" on every row, and it adds
  translated controls in all 12 app locales, documented RTL behaviour, screen-reader status
  announcements, touch gestures, and more shortcuts.
- **Every other candidate is ineligible**: Vidstack is itself deprecated (security fixes only until
  January 2028), Media Chrome's own README steers users to Video.js 10 and it ships 6 of our 12
  locales, Video.js 8 is the legacy line (202 KB gzip full build), Shaka Player's UI build is 378 KB
  gzip and aimed at adaptive streaming, and a hand-rolled wrapper over `<video>` breaks the repo's
  "never hand-roll what a maintained library does" rule.
- **Bundle cost (measured, esbuild, gzip -9):** Plyr is 33,977 B JS + 5,149 B CSS. Video.js 10
  composed controls are 55,416 B of JS loaded with the player. The library also emits 53 lazy
  locale chunks (66,009 B in total), only one of which a user ever downloads. First-load closure is
  unaffected (the player is route-scoped). The **total-JS** budget will need a reviewed, measured
  raise: **owner question Q1**.
- Five lanes in three waves (section 5). Plyr, its static assets, its prebuild download, its
  allowlist entries and its guard baselines are all deleted in the last lane. There will not be two
  players in the tree when the work is done.

---

## 1. Premise corrections (read before doing anything)

**PC1: "keeping … light/dark/RTL parity". Plyr has no RTL parity to keep.** Plyr's chrome is
physical-direction CSS and stays left-to-right in the Arabic UI. The styling-layer plan records
this as "third-party player chrome is LTR by design"
(`docs/design/frontend_styling_layer_plan.md:485`, `:1130`). The only RTL-aware piece today is our
own caption CSS (`VideoPlayer.svelte:503-537`, `unicode-bidi: plaintext`). The replacement must
keep that and should add proper RTL. It must not reproduce the LTR chrome as "parity".

**PC2: Plyr's controls are not translated today.** Plyr has an `i18n` option, but neither player
passes it (`VideoPlayer.svelte:247-263`, `PlyrMiniPlayer.svelte:54-65`). Every control label,
tooltip and menu item is English in all 12 locales. The e2e test depends on this: it clicks the
button named `"Settings"` (`backend/tests/e2e/test_plyr_playback.py:97`). Translated labels are a
gain from the replacement, and the e2e selectors must stop matching on English names.

**PC3: "waveform behaviour" is not Plyr behaviour.** The waveform is our own
`WaveformPlayer.svelte` (no Plyr import). It reaches the player only through `seekToTime`
(`fileDetail/FilePlayer.svelte:110-120`) and, in the mini player, by assigning
`player.currentTime` (`PlyrMiniPlayer.svelte:183-187`). The seam in section 4 preserves both call
paths. No waveform code changes beyond that one assignment.

**PC4: the obvious modern alternative is deprecated too.** Vidstack (`vidstack@1.15.7`) carries an
npm deprecation notice: "Vidstack is deprecated and gets security fixes only until January 2028.
Use Video.js 10." (`npm view vidstack@latest deprecated`, read 2026-11-07). Choosing it would put
us back where #1327 started.

---

## 2. Inventory: every Plyr capability the app uses

There are two Plyr instances and five consumers:

- `frontend/src/components/VideoPlayer.svelte` (1,071 lines, about 600 of them Plyr CSS): the
  file-detail player, wrapped by `components/fileDetail/FilePlayer.svelte`.
- `frontend/src/components/PlyrMiniPlayer.svelte` (567 lines): the floating preview, wrapped by
  `FloatingPreviewPlayer.svelte`, used by `routes/search/+page.svelte:18,721-736` (static import)
  and `routes/speakers/+page.svelte:331-335` through `speakers/SpeakerPreviewPlayer.svelte:10-15`
  (dynamic import).

Rows marked **(app)** are behaviour our code owns that depends on the media element or on player
events rather than on Plyr's UI. A replacement must not break them, but it does not provide them.

| # | Capability | Where (file:line) |
|---|---|---|
| 1 | Video playback of the original (`<video>`) | `VideoPlayer.svelte:404-416` |
| 2 | Audio playback (`<audio>`), including `audio_only` and `converted` renditions | `VideoPlayer.svelte:40-42, 391-402`; `lib/api/mediaUrl.ts:17-21` |
| 3 | Raw `<source type>` hint from the served type, omitted for aliases (#1044) **(app)** | `VideoPlayer.svelte:38-41, 400, 412`; `lib/utils/mediaType` |
| 4 | Media fetched by the native element from same-origin presigned URLs (byte-range requests, no JS fetch, no MSE) | `lib/api/mediaUrl.ts:1-60`; CSP `media-src 'self' blob:` `kit.config.js:48` |
| 5 | Presigned-URL hot swap that keeps position and play state | `VideoPlayer.svelte:361-383`; `PlyrMiniPlayer.svelte:31-37, 159-173` |
| 6 | External seek API: transcript segment click, comment time chip, waveform, `?t=` deep link | `VideoPlayer.svelte:62-120`; `FilePlayer.svelte:40-48, 82-95, 110-120`; `routes/files/[id]/+page.svelte:389-392, 446-453`; `comments/CommentItem.svelte:75`; `CommentSection.svelte:395` |
| 7 | Seek issued before `loadedmetadata` lands on the target (#647) **(app)** | `lib/utils/mediaReady.ts:1-92`; `VideoPlayer.svelte:87-111`; `PlyrMiniPlayer.svelte:110-157` |
| 8 | Player progress bar and time readout follow an externally issued seek | `VideoPlayer.svelte:99-119` (a workaround: Plyr's setter ignores seeks while its duration is 0) |
| 9 | Seeking spinner driven by `seeking`/`seeked`/`error` **(app)** | `VideoPlayer.svelte:296-316, 426-430`; `PlyrMiniPlayer.svelte:92-100, 204-208` |
| 10 | `timeupdate` drives the active-segment highlight, comment "current time" and waveform playhead | `VideoPlayer.svelte:284-291`; `FilePlayer.svelte:61-64`; `+page.svelte:649, 736, 790` |
| 11 | `ready`/`loadedmetadata` reports duration | `VideoPlayer.svelte:272-282` |
| 12 | `play`/`pause`/`ended` events feed the search-card and speakers state | `VideoPlayer.svelte:293-295`; `PlyrMiniPlayer.svelte:92-97`; `routes/search/+page.svelte:77-80, 734-736`; `routes/speakers/+page.svelte:333-335` |
| 13 | Imperative `play()`/`pause()` from outside the player | `routes/search/+page.svelte:437-445`; `routes/speakers/+page.svelte:54, 143`; `FloatingPreviewPlayer.svelte:31-37` |
| 14 | Clip-end auto-pause at `endTime` (speaker preview) **(app)** | `PlyrMiniPlayer.svelte:82-84` |
| 15 | Autoplay from a start offset, paused during a deep seek **(app)** | `PlyrMiniPlayer.svelte:74, 118-157` |
| 16 | Large centre play button (`play-large`) | `VideoPlayer.svelte:217` |
| 17 | Play/pause, elapsed and total time, seek bar | `VideoPlayer.svelte:216-222`; `PlyrMiniPlayer.svelte:51-52` |
| 18 | Mute and volume slider (no volume slider on the mini video player) | `VideoPlayer.svelte:225`; `PlyrMiniPlayer.svelte:51-52` |
| 19 | Settings menu with Captions and Speed submenus (Speed only for audio) | `VideoPlayer.svelte:233, 245`; `PlyrMiniPlayer.svelte:56` |
| 20 | Speed choices 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2 and **4** (Plyr defaults, not overridden) | `plyr/src/js/config/defaults.js:85-88` |
| 21 | Captions (CC) button, video only, with a styled pressed state | `VideoPlayer.svelte:228-230, 539-555` |
| 22 | Server-built WebVTT loaded through a blob-URL `<track>`, reloaded after edits and the redaction toggle, with a stale-response guard | `VideoPlayer.svelte:122-205, 414`; `lib/fileDetail/notificationHandler.ts:134-137`; `+page.svelte:417-421` |
| 23 | Captions turn on automatically unless the viewer switched them off (reads Plyr's stored `captions` flag) | `VideoPlayer.svelte:126-133, 187-196` |
| 24 | Custom caption rendering: dark box, wrapped lines, size scales with player width (`cqi`), per-cue bidi, speaker name styled through `<c.speaker>` | `VideoPlayer.svelte:503-537`; `backend/app/services/captions/render.py:3-5, 38-40` |
| 25 | Picture-in-picture (video, main player) | `VideoPlayer.svelte:234-236` |
| 26 | AirPlay button on Apple platforms | `VideoPlayer.svelte:213, 237-239` |
| 27 | Fullscreen on the main player, native iOS presentation | `VideoPlayer.svelte:240-242, 262` |
| 28 | Fullscreen on the mini video player with **full-window fallback** where the Fullscreen API is missing (iPhone) | `PlyrMiniPlayer.svelte:52, 64, 380-391` |
| 29 | `playsinline` (no forced iOS fullscreen on play) | `VideoPlayer.svelte:261, 398, 410`; `PlyrMiniPlayer.svelte:212-216` |
| 30 | Keyboard: page-wide on the main player (`keyboard.global: true`), focus-scoped on the mini player; skipped while typing in inputs | `VideoPlayer.svelte:254`; `PlyrMiniPlayer.svelte:61`; Plyr README "Shortcuts" |
| 31 | Shortcut set: `0`-`9` seek to 0-90%, Space and `K` play/pause, Left and Right seek 10 s, Up and Down volume, `M` mute, `F` fullscreen, `C` captions, **`L` loop** | Plyr README "Shortcuts"; `seekTime` default 10 |
| 32 | Tooltips on controls | `VideoPlayer.svelte:255`; `PlyrMiniPlayer.svelte:62` |
| 33 | Controls auto-hide during video playback, click-to-play on the video | Plyr defaults `hideControls`, `clickToPlay` (not overridden) |
| 34 | Touch: draggable sliders on mobile (Plyr depends on `rangetouch`), tap to play | `plyr@3.8.5` dependencies |
| 35 | Remembered preferences in `localStorage['plyr']`: volume, muted, speed, captions on/off, caption language | `plyr/src/js/listeners.js:436-448`, `captions.js:195, 272`; read at `VideoPlayer.svelte:129` |
| 36 | Accessible controls: ARIA-labelled buttons, `menuitemradio` menus, keyboard focus | `test_plyr_playback.py:97-99` |
| 37 | Light and dark theming through `:global(.plyr…)` overrides of Plyr's stylesheet | `VideoPlayer.svelte:452-1071`; `PlyrMiniPlayer.svelte:242-567`; `styles/range-slider.css:1-10` |
| 38 | Compact mini-player sizing | `PlyrMiniPlayer.svelte:14, 399-408` |
| 39 | Mini player embeds the waveform for audio and seeks the player from it | `PlyrMiniPlayer.svelte:228-238, 183-187` |
| 40 | CSP-clean, no third-party origin: icons self-hosted (`/plyr.svg`), blank video self-hosted (`/plyr-blank.mp4`), no inline script, no eval | `VideoPlayer.svelte:250-253`; `PlyrMiniPlayer.svelte:57-60`; `frontend/scripts/download-fonts.js:53-70`; `kit.config.js:39-53` |
| 41 | Browser-only module (no SSR): route `ssr = false`, dynamic import on the speakers route | `routes/files/[id]/+page.ts:3-4`; `speakers/SpeakerPreviewPlayer.svelte:10-15` |
| 42 | Clean teardown: `destroy()`, blob-URL revoke, listener removal | `VideoPlayer.svelte:323-354`; `PlyrMiniPlayer.svelte:103-108, 197-200` |
| 43 | Control labels in the UI language | **Not used.** Plyr supports `i18n`, the app never passes it (PC2) |
| 44 | RTL-mirrored chrome in `ar` | **Not supported** (PC1) |
| 45 | Arbitrary `<track>` language menu (multiple caption languages) | Plyr supports it; the app ships one track (`VideoPlayer.svelte:414`) |

App-owned behaviour that stays exactly as it is (listed so no lane deletes it by accident): the
error and retry UI (`VideoPlayer.svelte:432-448`), the buffering overlay (`:419-424`), the
audio-only notice (`:388-390`) and the floating panel chrome (`FloatingPreviewPlayer.svelte`).

**Inventoried capability count: 45 rows. Plyr is "yes" on 41 of them, partial on row 8 (it needs
the resync workaround), "no" on rows 43 and 44, and row 45 is supported but unused by the app.**

---

## 3. Candidates and feature matrix

### 3.1 Sources (all read 2026-11-07)

- npm registry metadata: `npm view <pkg>@latest` for `plyr`, `video.js`, `@videojs/html`,
  `media-chrome`, `vidstack`, `shaka-player` (versions, release times, licences, deprecation
  notices, maintainers).
- GitHub repository metadata: `gh api repos/<owner>/<repo>` for `videojs/video.js`,
  `sampotts/plyr`, `muxinc/media-chrome`, `vidstack/player`, `shaka-project/shaka-player`, and
  `gh api repos/videojs/video.js/releases`.
- Plyr README (deprecation banner, Options, Shortcuts) and source
  (`src/js/config/defaults.js`, `listeners.js`, `captions.js`, `fullscreen.js`), from the
  `plyr@3.8.5` tarball. https://github.com/sampotts/plyr
- Video.js 10 docs, read from https://videojs.org/docs/framework/html/guides/migrate-from-plyr
  and from the copy shipped in `@videojs/html@10.3.0/docs/` (same content, version-pinned):
  `guides/migrate-from-plyr.md`, `content-security-policy.md`, `svelte.md`,
  `installation-svelte.md`, `keyboard-shortcuts.md`, `internationalization.md`, `captions.md`,
  `accessibility.md`, `user-preferences.md`, `fullscreen.md`, `browser-support.md`,
  `why-videojs.md`, `reference/components/*.md`, `reference/api/feature-playback-rate.md`,
  `reference/api/load-locale.md`.
- Media Chrome README and `dist/lang/` from `media-chrome@4.19.4`. https://github.com/muxinc/media-chrome
- MDN `::cue` (permitted properties): https://developer.mozilla.org/en-US/docs/Web/CSS/::cue ;
  browser support from `@mdn/browser-compat-data` (`css.selectors.cue.selector_argument`:
  Firefox `false`, bug https://bugzil.la/1321489).
- Licence policy: `scripts/lib/license_policy.py:19-57` (`ALLOW` includes `mit`, `apache-2.0`,
  `bsd-3-clause`).

### 3.2 Candidate facts

| | Plyr 3.8.5 (today) | **Video.js 10.3.0** (`@videojs/html`) | Media Chrome 4.19.4 | Vidstack 1.15.7 | Video.js 8.24.2 | Shaka Player 5.3.4 | Native `<video>` wrapper |
|---|---|---|---|---|---|---|---|
| Status | **Deprecated**: "security updates only. Use Video.js 10" (npm + README) | Active current major. 10.0.0 on 2026-10-01, then 10.0.1, 10.1.0, 10.2.0, 10.3.0 (2026-11-04). 100+ commits since 2026-10-07; repo pushed 2026-11-07 | Active (canary 2026-10-30), but README banner steers users to Video.js 10 | **Deprecated**: security fixes only until Jan 2028 | Legacy line (v10 is `latest`; v10 docs ship "migrate from Video.js 8"); patch releases continue (8.24.2, 2026-10-06) | Active (5.3.4 on 2026-11-06) | n/a |
| Maintainers | 1 (sampotts) | Mux and Brightcove engineers; "built at Mux by the teams behind Video.js, Plyr, Vidstack, and Media Chrome" (`why-videojs.md`) | Mux | 1 | Video.js org | Google (shaka-project) | us |
| Issue health | 1 open issue/PR (frozen) | 204 open issues+PRs, 40.1k stars | 7 open | 1 open (frozen) | shared repo | 46 open | n/a |
| Licence (policy class) | MIT (ALLOW) | Apache-2.0 (ALLOW); transitive `@lit/*` BSD-3-Clause, `signal-polyfill` and `@svta/*` Apache-2.0, `react` + `react-compiler-runtime` MIT (installed, **not bundled**) | MIT (ALLOW); `ce-la-react` BSD-3-Clause | MIT | Apache-2.0 | Apache-2.0 | n/a |
| JS gzip, as the app would load it (measured) | 33,977 B + 5,149 B CSS | 55,416 B composed (51,384 B without the i18n provider import) + 66,009 B across 53 lazy locale chunks (one is fetched per user, about 1.3 KB) | 56,668 B (full element set) | not measured (deprecated) | 201,724 B full / 68,350 B core-novtt + 12,061 B CSS | 377,726 B (`shaka-player.ui.js`) | ~0 |
| Svelte 5 / SvelteKit 3 | Imperative class | Custom elements; official Svelte and SvelteKit guide (`guides/svelte.md`) | Custom elements | Svelte export (deprecated) | Imperative class | Imperative class | native |
| CSP (`script-src 'self' 'wasm-unsafe-eval'`, `style-src 'self' 'unsafe-inline'`, `media-src 'self' blob:`) | OK once self-hosted | OK: no eval, no worker, no blob URL in the composed build (scanned); progressive MP4/M4A needs no `blob:`; the guide's required `style-src 'unsafe-inline'` is already granted | OK | OK | OK | Uses MSE (`blob:` media, already allowed) | OK |

Measurement method, reproducible: in an empty scratch dir, `npm install --ignore-scripts
@videojs/html@10.3.0 plyr@3.8.5 media-chrome@4.19.4 esbuild`, then bundle one entry per candidate
with `esbuild --bundle --minify --format=esm --splitting` and `gzip -9c | wc -c` each output. The
composed Video.js entry imports `video/player`, `audio/player` and these `ui/*` elements:
`container controls play-button time-slider time mute-button volume-slider captions-button
playback-rate-button menu playback-rate-radio-group pip-button fullscreen-button airplay-button
hotkey gesture tooltip status-announcer buffering-indicator`, plus `i18n`. The packaged
`video/skin` and `audio/skin` together measured 97,888 B, which is one reason section 4 composes
the controls instead. **Vite/Rollup output will differ by a few percent. Lane B re-measures with
the real `vite build` and the repo's `check-bundle-budget.mjs`.**

### 3.3 Feature matrix

Legend: **Y** = yes out of the box; **Y(app)** = the library deliberately leaves this to the app,
documents the pattern, and the gap is closed by a few lines in our adapter with a test that fails
first; **P** = partial; **N** = no; **–** = not applicable or not evaluated because the candidate
already failed an earlier gate.

| # | Capability | Plyr (today) | Video.js 10 | Media Chrome | Vidstack | Video.js 8 | Shaka UI | Native wrapper |
|---|---|---|---|---|---|---|---|---|
| 1 | Video playback | Y | Y (`<video>` slotted as-is) | Y | Y | Y | Y | Y |
| 2 | Audio playback | Y | Y (audio preset, `<audio>`) | Y | Y | Y | P (video-centric UI) | Y |
| 3 | Raw `<source type>` kept | Y | Y (we own the `<video>`/`<audio>` markup) | Y | Y | P (owns the element) | P | Y |
| 4 | Native range-request playback of presigned URLs | Y | Y (no MSE for MP4/M4A) | Y | Y | Y | P (MSE engine path) | Y |
| 5 | URL hot swap | Y | Y (store follows the media element; `load()` + seek as today) | Y | Y | Y | Y | Y |
| 6 | External seek API | Y | Y (`seek(t)` or `media.currentTime`) | Y | Y | Y | Y | Y |
| 7 | Seek before metadata | Y (app-owned) | Y (app-owned, unchanged) | Y | Y | Y | Y | Y |
| 8 | UI follows an external seek | P (needs the resync workaround) | **Y** (state is read from the media element; workaround deleted) | Y | Y | Y | Y | Y |
| 9 | Seeking spinner events | Y | Y (native events on the element) | Y | Y | Y | Y | Y |
| 10 | `timeupdate` sync | Y | Y (native events) | Y | Y | Y | Y | Y |
| 11 | Duration on metadata | Y | Y | Y | Y | Y | Y | Y |
| 12 | `play`/`pause`/`ended` events | Y | Y (native events) | Y | Y | Y | Y | Y |
| 13 | Imperative play/pause | Y | Y (`play()`, `pause()`) | Y | Y | Y | Y | Y |
| 14 | Clip-end auto-pause | Y (app-owned) | Y (app-owned) | Y | Y | Y | Y | Y |
| 15 | Autoplay at an offset | Y (app-owned) | Y (app-owned) | Y | Y | Y | Y | Y |
| 16 | Large centre play button | Y | Y (`media-play-button` placed and sized by us) | Y | Y | Y | Y | build |
| 17 | Play, time, seek bar | Y | Y (`media-play-button`, `media-time`, `media-time-slider`) | Y | Y | Y | Y | build |
| 18 | Mute, volume | Y | Y (`media-mute-button`, `media-volume-slider`) | Y | Y | Y | Y | build |
| 19 | Settings menu (captions, speed) | Y | Y (`media-menu` + captions and playback-rate radio groups) | Y | Y | Y | Y | build |
| 20 | Speed list including 4x | Y | Y(app): default list stops at 2; set `default-playback-rates="0.5 0.75 1 1.25 1.5 1.75 2 4"` (`feature-playback-rate.md:23, 43-53`) | Y | Y | Y | Y | build |
| 21 | CC button, pressed state | Y | Y (`media-captions-button`, `data-active`) | Y | Y | Y | Y | build |
| 22 | Blob-URL WebVTT track, reloadable | Y | Y (standard `<track>`; `textTrackList` follows changes) | Y | Y | Y | Y | Y |
| 23 | Captions on unless switched off | Y | Y(app): `toggleSubtitles(true)` on load; stored flag per `user-preferences.md` | P | Y | Y | Y | build |
| 24 | Speaker-styled, width-scaled, bidi-safe caption rendering | Y (own renderer) | **Y(app)**: the browser renders cues, styled with `::cue`. `::cue(.speaker)` is unsupported in Firefox (MDN BCD) and `::cue` forbids padding, wrapping and box properties, so we keep rendering cues ourselves: track `showing`, native cues hidden, overlay built with `getCueAsHTML()`. That is what Plyr did internally | P (native) | Y (own renderer) | Y (own renderer) | Y (own displayer) | build |
| 25 | Picture-in-picture | Y | Y (`media-pip-button`; `i` hotkey) | Y | Y | Y | Y | build |
| 26 | AirPlay | Y | Y (`media-airplay-button`) | Y | Y | N (plugin) | Y | build |
| 27 | Fullscreen, native iOS presentation | Y | Y (`fullscreen.md:152`) | Y | Y | Y | Y | build |
| 28 | Mini player full-window fallback | Y | **Y(app)**: no full-window mode (`migrate-from-plyr.md`: "has no matching Video.js feature"). On iPhone the native presentation fills the screen. We add a CSS full-window class when `document.fullscreenEnabled` is false | P | Y | Y | P | build |
| 29 | `playsinline` | Y | Y (attribute on our element) | Y | Y | Y | Y | Y |
| 30 | Page-wide vs focus-scoped keyboard, typing-safe | Y | Y (`target="document"` on the main player; container scope by default; editable fields skipped) | Y | Y | P | P | build |
| 31 | Shortcut set incl. `L` loop | Y | Y for `0-9`, Space, `K`, arrows, `M`, `F`, `C` (`media-hotkey`). **Y(app) for `L` loop**: no loop action exists, so a scoped keydown handler toggles `media.loop` | P | Y | P | P | build |
| 32 | Tooltips | Y | Y (`media-tooltip`) | Y | Y | Y | Y | build |
| 33 | Auto-hide, click-to-play | Y | Y (`media-controls`, `media-gesture`) | Y | Y | Y | Y | build |
| 34 | Touch sliders, tap | Y | Y (gesture recognizer, touch tap toggles controls) | Y | Y | Y | Y | build |
| 35 | Remembered volume, mute, speed, captions | Y | **Y(app)**: "Persist preferences in your app" (`migrate-from-plyr.md`; tracked upstream as #944); `user-preferences.md` documents the store-subscribe pattern | N | Y | N | P | build |
| 36 | Accessible controls | Y | Y (WAI-ARIA 1.2 / WCAG 2.2 targets, `role="slider"`, focus management; `accessibility.md`) | Y | Y | Y | Y | build |
| 37 | Light and dark theming with app tokens | Y (by overriding `:global(.plyr…)`) | Y (our own markup and CSS; `data-*` state hooks; no shadow root when composed) | Y | Y | P | P | Y |
| 38 | Compact mini sizing | Y | Y (our CSS) | Y | Y | Y | P | Y |
| 39 | Waveform seek in the mini player | Y | Y (handle `seek`) | Y | Y | Y | Y | Y |
| 40 | CSP-clean, self-hosted | Y | Y (npm-bundled; icons are inline SVG elements, `@videojs/html/icons`; no CDN, no blank-video fetch) | Y | Y | Y | Y | Y |
| 41 | Browser-only init | Y | Y (custom elements upgrade client-side; `customElements.whenDefined`) | Y | Y | Y | Y | Y |
| 42 | Clean teardown | Y | Y (element disconnect; store unsubscribe) | Y | Y | Y | Y | Y |
| 43 | Controls in the UI language (12 locales) | N (unused) | **Y, new**: 50+ packs incl. ar de es fr it ja ko nl pt ru zh; follows `<html lang>` (`internationalization.md`) | P (6 locales) | Y | Y (106 lang files) | P | build |
| 44 | RTL chrome in `ar` | N | **Y, new**: follows `dir`; menus and chevrons mirror, time sliders keep a physical scale by design (`internationalization.md`, "Set text direction") | ? (undocumented) | Y | P | P | build |
| 45 | Multiple caption languages | Y (unused) | Y (`media-captions-radio-group`) | Y | Y | Y | Y | build |
| + | Screen-reader status announcements | N | **Y, new** (`media-status-announcer`) | N | P | N | N | build |
| + | Double-tap seek on touch, `<`/`>` speed, `i` PiP shortcuts | N | **Y, new** | P | Y | N | P | build |
| + | `prefers-contrast` / reduced-transparency handling | N | **Y, new** (skins; we carry it into our CSS) | N | N | N | N | build |
| + | Store-driven state (no Plyr clock to resync) | N | **Y, new** | Y | Y | N | N | n/a |

**Eligibility, applying the owner's rule (yes on every row Plyr is yes):**

- **Video.js 10: ELIGIBLE.** 35 of the 41 Plyr-"yes" rows are Y out of the box. Six are Y(app):
  rows 20, 23, 24, 28, 35 and the `L` key in 31. In each case the library documents the gap and the
  app-side pattern, and each closure has a red-first test in the lane that builds it. It adds rows
  43, 44 and the four "+" rows.
- **Media Chrome: NOT ELIGIBLE.** Row 35 is N, rows 23, 24 and 28 are P, and row 43 is P (6 of 12
  locales). Its own README tells users to move to Video.js 10.
- **Vidstack: NOT ELIGIBLE.** It is feature-complete but deprecated (PC4). Adopting it repeats
  #1327.
- **Video.js 8: NOT ELIGIBLE.** Row 26 is N without a plugin, rows 30, 31 and 37 are P, and row
  35 is N. It is the legacy line with a migration path out, and the full build is 5.9x Plyr's JS.
- **Shaka Player: NOT ELIGIBLE.** Rows 2, 4, 30, 31 and 37 are P. The UI build is 378 KB gzip, 11x
  Plyr's JS, and it is an adaptive-streaming engine the app has no use for (progressive files
  only).
- **Native `<video>` wrapper: NOT ELIGIBLE.** Every "build" cell is code we would have to write and
  keep accessible, translated and cross-browser. That is the library's job, and the repo rule
  ("Never hand-roll what a proper library already does") forbids it.

---

## 4. Recommendation and design

### 4.1 Why Video.js 10, and why composed rather than a packaged skin

Video.js 10 is the only eligible candidate. Plyr's deprecation notice names it and links a Plyr
migration guide, and the people who wrote Plyr work on it. Its licence is ALLOW. It is a set of
standards-based custom elements, so it does not depend on our framework version. It brings the
three things Plyr never gave us: translated controls, RTL behaviour and live-region announcements.

We **compose the controls from `@videojs/html/ui/*` in our own Svelte markup** instead of using
`<video-skin>`/`<audio-skin>`:

1. **Theming.** Packaged skins render in a shadow root and expose only a few CSS custom
   properties (`migrate-from-plyr.md`, CSS theming). Our light and dark design tokens,
   logical-property RTL rules, z-index tiers and contrast tests need our own markup. Composed
   elements carry `data-*` state attributes that our CSS reads directly (`svelte.md`, "Keep runtime
   styles global").
2. **Size.** Composed controls measured 55.4 KB against 97.9 KB for the two packaged skins.
3. **One layout for both players.** The file-detail player and the mini player become two
   configurations of one component, which removes about 1,000 lines of duplicated `:global(.plyr…)`
   overrides.

This is the documented "build your own skin / custom UI from components" path
(`guides/build-your-own-skin.md`, `guides/ui-components.md`), not a workaround.

### 4.2 The seam: `PlayerHandle`

The app talks to a small interface, not to a library. Events stay **native media-element events**
(the Video.js docs say to keep playback handlers on the `<video>`/`<audio>` element). The handle
only covers imperative control, so it is small.

New file `frontend/src/lib/player/types.ts`:

```ts
/** What the app may ask of a player. Events come from `media` (native HTMLMediaElement events). */
export interface PlayerHandle {
  readonly media: HTMLMediaElement;
  play(): Promise<void>;
  pause(): void;
  /** Seek the media element now (pre-metadata safe, see $lib/utils/mediaReady). */
  seek(time: number): void;
  setPlaybackRate(rate: number): void;
  setCaptionsVisible(visible: boolean): void;
  readonly captionsVisible: boolean;
}
```

Consumers change to use it:

- `FloatingPreviewPlayer.getPlayer()` returns `PlayerHandle | null` (today `Plyr | null`,
  `PlyrMiniPlayer.svelte:179-181`). The callers in `routes/search/+page.svelte:437-445` and
  `routes/speakers/+page.svelte:54, 143` already call only `play()`/`pause()`, so they type-check
  unchanged.
- `VideoPlayer.seekToTime` and `updateSubtitles` keep their names and signatures. `FilePlayer`,
  the page and `notificationHandler.ts` do not change.
- `mediaReady.ts` stays. Its doc comment about "Plyr's own clock" is rewritten, because with a
  store that reads the media element there is no second clock (row 8).

### 4.3 New modules (all under the existing aliases)

| Path | Purpose |
|---|---|
| `src/lib/player/types.ts` | `PlayerHandle` (above) |
| `src/lib/player/plyrHandle.ts` | Lane A only: wraps today's Plyr so consumers move to the seam first. Deleted in lane E |
| `src/lib/player/videojsHandle.ts` | `PlayerHandle` over a `<video-player>`/`<audio-player>` element's `store` (`play`, `pause`, `seek`, `setPlaybackRate`, `toggleSubtitles`) |
| `src/lib/player/prefs.ts` | Remembered volume, muted, rate, captions in `localStorage['opentr:playerPrefs']`, following `user-preferences.md`. Reads the legacy `localStorage['plyr']` once if the new key is absent, so a viewer who turned captions off keeps that choice. Storage errors are tolerated, as in the doc's example |
| `src/lib/player/i18n.ts` | Bridges the app locale to Video.js: preloads the active locale's pack (`@videojs/html/i18n/locales/<x>/register`, via a 12-entry lazy map) when `stores/locale.ts` switches. `<html lang>`/`dir` are already set at `stores/locale.ts:31-35, 109-110`, which `media-i18n` follows |
| `src/lib/player/hotkeys.ts` | The binding table for rows 30 and 31: `media-hotkey` declarations plus the `L` loop keydown handler. One source for both players |
| `src/lib/player/CLAUDE.md` | Folder doc: the seam rule ("components never import `@videojs/*` except `components/player/*`"), the caption-overlay reason, the prefs key |
| `src/components/player/MediaPlayer.svelte` | Composed `<video-player>`/`<audio-player>` + controls; props select `variant: 'full' \| 'mini'` and `compact` |
| `src/components/player/MediaControls.svelte` | Control bar and settings menu markup and CSS (tokens, logical properties) |
| `src/components/player/CaptionOverlay.svelte` | Renders active cues from the `showing` track with `getCueAsHTML()`, carrying over the CSS at `VideoPlayer.svelte:503-537` verbatim. Native cue painting is suppressed with `video::cue { visibility: hidden }` (`visibility` is a permitted `::cue` property in every engine) |

`VideoPlayer.svelte` keeps its name and public API and becomes a thin host of `MediaPlayer`
(target under 300 lines). `PlyrMiniPlayer.svelte` is replaced by `MiniMediaPlayer.svelte` in lane
D. Add Svelte custom-element typings (`SvelteHTMLElements`) per `svelte.md`.

Pin the dependency exactly (`"@videojs/html": "10.3.0"`, not `^`). The major is five weeks old
and ships a minor about every ten days.

### 4.4 Migration risks

| Risk | Mitigation |
|---|---|
| A young major (10.0.0 on 2026-10-01) with known gaps (`migrate-from-plyr.md` "Not supported") | Exact pin. The gaps we hit (prefs, full-window, loop key, caption styling) are closed in our code, each with a test. Re-read the gap list on every bump |
| `::cue(selector)` unsupported in Firefox, so speaker names would lose their colour | `CaptionOverlay` (row 24). The e2e asserts the `.speaker` element in the overlay, not a computed `::cue` style |
| Overlay plus a native cue painting twice | `video::cue { visibility: hidden }`, checked by an e2e screenshot of a cue frame |
| jsdom cannot exercise custom elements, Popover API or the store faithfully | Unit tests target `PlayerHandle` with a fake (as the Plyr fakes do today, `VideoPlayer.captions.test.ts:14-36`); real behaviour is proven by Playwright e2e in Chromium |
| Locale chunks raise total JS by about 66 KB (never downloaded unless used) | Owner question Q1. Do not patch the library's loader map |
| `react` and `react-compiler-runtime` appear in `node_modules` and the licence report (a dependency of `@videojs/store`) but are not bundled | Note it in the licence-report diff. Lane B asserts that no `react` module appears in the Vite manifest for any route |
| Keyboard conflict: Video.js binds `j`/`l` to seek, Plyr binds `L` to loop | We declare our own table (row 31). Default: keep `L` = loop for parity and do not bind `j`/`l` (Q2) |
| Hot reload and stash windows during lanes | Lane rules in section 5. Each lane works in its own worktree; no lane runs a full suite |
| Visual baselines for `file_detail` change | Lead recaptures with `./scripts/e2e/update-visual-baselines.sh --reason "#1327 player replaced"` in the live window |

---

## 5. Lanes and sequencing

Per the root `CLAUDE.md` and the multi-agent rules: each lane is one agent in its **own git
worktree on its own branch**, commits there with hooks on (never `--no-verify`), runs only
targeted tests plus static checks on touched files, and never pushes or merges. The lead merges
lanes `--no-ff` into the integration branch and runs **one** full sweep per wave. Red-first
evidence uses the `git archive HEAD` tree (`ot-redcheck.` prefix), never file swapping in a shared
checkout.

### Wave 1 (parallel, disjoint files)

**Lane A: the seam on Plyr (no dependency change, no visible change).**
1. Add `lib/player/types.ts` and `lib/player/plyrHandle.ts`.
2. `PlyrMiniPlayer.getPlayer()` and `FloatingPreviewPlayer.getPlayer()` return `PlayerHandle`.
   The waveform seek (`PlyrMiniPlayer.svelte:183-187`) calls `handle.seek`.
3. Make the e2e test library-neutral: rename `backend/tests/e2e/test_plyr_playback.py` to
   `test_media_playback.py`, add a page object `backend/tests/e2e/media_player.py` that finds
   controls by role and accessible name read from the app's own `en.json` keys or by `#player`
   state, and update `scripts/e2e/run-e2e-release.sh:18` and
   `backend/tests/unit/test_release_e2e_selection.py:23`.
4. Add **characterization e2e tests**, green on Plyr, that later lanes must keep green:
   comment time chip seeks to `t - 0.5` (#6); segment click and active-segment highlight follow
   playback (#10); `?t=` deep link (#6); speed 1.5 reaches `media.playbackRate` (#19); 4x is offered
   (#20); CC toggle hides and shows the cue and the `.speaker` element is present (#21, #24);
   Space, Left/Right (10 s), `0`-`9`, `M`, `C`, `L` (#31), and typing in the comment box does not
   trigger them (#30); search preview starts at the hit's time and the card toggle pauses and
   resumes (#13, #15); speaker preview pauses at `end_time` (#14); an audio file shows no CC, PiP or
   fullscreen and its waveform seeks (#2, #39); captions stay off after reload when switched off
   (#23, #35).
5. Unit: `plyrHandle.test.ts` against the existing Plyr fakes. Port `PlyrMiniPlayer.test.ts`'s
   #649 cases to drive `PlayerHandle`.
Proof: `npm run test -- src/lib/player src/components/PlyrMiniPlayer.test.ts` and
`./scripts/e2e/run-e2e.sh -k test_media_playback` (lead's live window).

**Lane B: Video.js support modules, no consumer touched.**
1. `npm install --save-exact @videojs/html@10.3.0` (an `npm` command, run in `frontend/`).
2. `lib/player/prefs.ts`, `i18n.ts` and `hotkeys.ts`, with unit tests written first and watched
   red: prefs round-trip, tolerance of a throwing `localStorage`, one-time legacy `plyr` read; the
   i18n bridge registers only the active locale and switches on a `locale` store change; the
   hotkey table contains every row-31 key, and `L` toggles `media.loop` but not inside an
   editable element.
3. `lib/player/videojsHandle.ts` with a unit test over a fake `store`.
4. **Measure:** `nice -n 19 npx vite build` before and after, and record the
   `check-bundle-budget.mjs` output (node, closure, entry, total JS) in the PR body. Add a
   manifest assertion to `src/bundleBudget.manifest.test.ts` that no `@videojs/*` or `react`
   module is in the first-load closure.
5. `scripts/check-licenses.py` and `scripts/dependency-diff.py` on the lockfile change, with
   output in the PR body.

### Wave 2 (parallel; needs A and B merged)

**Lane C: the file-detail player.** Rebuild `VideoPlayer.svelte` on `components/player/MediaPlayer`
+ `MediaControls` + `CaptionOverlay`, keeping its props, events and `seekToTime`/`updateSubtitles`
API. Delete the Plyr-clock resync (`VideoPlayer.svelte:99-119`), since row 8 is now structural.
Port `VideoPlayer.test.ts` and `VideoPlayer.captions.test.ts` to a fake `PlayerHandle`. For each
capability, write the e2e assertion first and run it red against the bare `<video-player>`
skeleton, then wire it. New tests that must fail on Plyr: control labels in `ar` (the play button's
accessible name is Arabic), the settings menu mirrored under `dir="rtl"`, and a status
announcement after play. Mobile viewport (390x844) e2e for touch-tap and fullscreen button
presence. Light and dark contrast through the existing `styles/contrast-pairs.test.ts` tokens.

**Lane D: the mini player.** `MiniMediaPlayer.svelte` (same `MediaPlayer`, `variant="mini"`,
container-scoped hotkeys, CSS full-window fallback when `document.fullscreenEnabled === false`,
with a unit test stubbing `fullscreenEnabled` to false and watching it red first).
`FloatingPreviewPlayer` imports it. Clip-end pause, autoplay at offset, hot swap and waveform seek
keep their #649 tests, ported to `PlayerHandle`. Remove the `browser` dynamic-import guard in
`SpeakerPreviewPlayer.svelte:10-15` only if the speakers route builds and refreshes cleanly
without it. Otherwise keep it and update its comment.

### Wave 3

**Lane E: remove Plyr and close out.** Delete, in one lane:
- `plyr` from `frontend/package.json:72` and the lockfile.
- `lib/player/plyrHandle.ts` and its test.
- `frontend/static/plyr.svg`, `frontend/static/plyr-blank.mp4`, and `downloadPlyrIcons()` with
  its call in `frontend/scripts/download-fonts.js:53-70, 122`.
- `PlyrMiniPlayer.svelte` and `PlyrMiniPlayer.test.ts`.
- Allowlist entries that only existed for Plyr: `components/ui/rangeSliderStructure.test.ts:23-26`
  (the time slider is a `role="slider"` element, not an `<input type="range">`) and
  `components/floatingPanel.convention.test.ts:36` (menus use the Popover API top layer).
- Guard baseline lines for `PlyrMiniPlayer.svelte` in `scripts/guards/baselines/style_literals.txt`
  and `scripts/guards/baselines/eslint.txt:36-38`, and lower the `VideoPlayer.svelte` counts to the
  new measured values (the baselines ratchet down, never up).
- The `ssr = false` comment in `routes/files/[id]/+page.ts:3` (keep the setting, which is SPA mode,
  and fix the reason).
- Plyr references in docs: `frontend/README.md:24`, `src/components/speakers/CLAUDE.md:21, 54-55`,
  `src/styles/CLAUDE.md:39`, `src/styles/range-slider.css:6-8`, `src/components/ui/CLAUDE.md:85, 163`,
  `src/components/search/CLAUDE.md`, `src/components/fileDetail/CLAUDE.md`,
  `routes/search/+page.svelte:77`, `components/search/SearchPreviewButton.svelte:4`,
  `lib/utils/mediaReady.ts:4-16`, `routes/files/[id]/+page.svelte:1001`.
- Then `rg -i plyr frontend/src frontend/scripts frontend/static backend/tests scripts` must
  return nothing except CHANGELOG history.
- Licence report: regenerate `security-reports/license-compliance/frontend-npm-licenses*.{md,csv}`
  with `scripts/generate-sbom-report.py` (its documented license-checker inputs). The diff should
  be `plyr`, `rangetouch`, `loadjs`, `url-polyfill`, `custom-event-polyfill` removed and the
  `@videojs/*`, `@lit/*`, `@lit-labs/ssr-dom-shim`, `signal-polyfill`, `@svta/*`,
  `react-compiler-runtime`, `react` set added, all ALLOW.
- Bundle budget: final `vite build` measurement in the `check-bundle-budget.mjs` header (a dated
  block, as for #1326) and the budget change agreed in Q1.
- `CHANGELOG.md` (Keep a Changelog, `Changed`): the player change, the newly translated and RTL
  controls, `L` behaviour per Q2, and that the remembered caption choice carries over.
- User docs: any `docs-site/docs` page describing player shortcuts or controls.

### The lead's live window (after each wave's merge)

One full sweep per wave on the integration branch (`./scripts/run-dev-tests.sh --full`, then
`scripts/safe-precommit.sh run --all-files` and `--hook-stage pre-push`). Then, in the live
window:
- `./scripts/e2e/run-e2e.sh -m transcription` (includes `test_media_playback.py`) and
  `./scripts/e2e/run-e2e.sh -m visual`.
- After wave 2: `./scripts/e2e/update-visual-baselines.sh --reason "#1327 player replaced"` for
  `file_detail` light and dark.
- Look at it in a browser (`DISPLAY=:11`), not only the tests. Check file detail with a video and
  an audio file, the search preview and the speakers preview, each in light, dark and `ar`, plus a
  mobile viewport. Check captions with the speaker colour in Chromium and Firefox. Test
  PiP, fullscreen and the settings menu inside the floating panel.

---

## 6. Owner questions (each has a recommended default)

**Q1: total-JS bundle budget.** The library ships 53 lazily loaded locale packs (66 KB gzip in
total, one about 1.3 KB pack fetched per user). With the composed controls (+55 KB) minus Plyr
(-34 KB), the projected total-JS delta is about +87 KB, above today's headroom of about 57 KB
(2,641,920 B budget against 2,585,192 B measured, `check-bundle-budget.mjs` header). First-load
closure and entry chunks are unchanged. **Default: accept a total-JS budget raise of exactly the
delta measured in lane E plus the usual ~4% headroom**, recorded in a dated header block like the
#1326 precedent, rather than patching the library's locale loader.

**Q2: the `L` key.** Plyr toggles loop on `L`. Video.js and YouTube use `J`/`L` to seek 10 s.
**Default: keep `L` = loop (strict parity with today) and do not bind `J`/`L` to seek.** The
arrow keys already seek 10 s.

No other decision needs the owner. Composed versus packaged skin, the caption overlay, the exact
pin and the prefs key are technical choices settled above with their reasons.
