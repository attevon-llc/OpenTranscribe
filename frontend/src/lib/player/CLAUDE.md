# src/lib/player — the media-player seam (issue #1327)

## Purpose

The app's only route to a media-player library. Plyr is deprecated upstream and is being
replaced (`docs/design/1327_plyr_replacement_plan.md`); everything that plays media talks to the
library-neutral `PlayerHandle` here, so the swap changes the adapter, not the app.

## Key files

- `types.ts` — `PlayerHandle` (play, pause, seek, playback rate, captions on/off, `media`),
  `OwnedPlayer` (adds `destroy`, for the host that built it) and `PlayerOptions`
  (`variant: 'full' | 'mini'`, `audio`, `onReady`). Library-neutral on purpose: no Plyr names.
- `plyrHandle.ts` — the Plyr adapter: `createPlyrPlayer(media, options)`, the per-variant Plyr
  configuration (`plyrConfig`), and `viewerTurnedCaptionsOff()` (reads Plyr's remembered
  captions choice from `localStorage['plyr']`). **The only module that may import `plyr`.**
  Deleted when the last host moves to the replacement.
- `mediaEvents.ts` — `listenToMedia(media, handlers)` (returns one remover) and
  `mediaDuration(media)` (NaN/Infinity read as 0).
- `fakePlayer.ts` — test-only `createFakePlayer` + `fakePlayers`; host tests mock the adapter
  (`$lib/player/videojsPlayer`, or `plyrHandle` for the mini player) with it instead of mocking
  the library.

## Conventions / patterns

- **Events are native.** Hosts listen on their own `<video>`/`<audio>` element
  (`timeupdate`, `play`, `pause`, `playing`, `seeking`, `seeked`, `ended`, `error`) through
  `listenToMedia`, never through the library's event API. Every candidate library keeps those.
- **Hosts:** `components/VideoPlayer.svelte` (variant `full`, on Video.js through
  `createVideojsPlayer` inside `components/player/MediaPlayer`) and
  `components/PlyrMiniPlayer.svelte` (variant `mini`, still Plyr until #1327 lane D). `FloatingPreviewPlayer.getPlayer()` hands
  the search and speakers pages a `PlayerHandle`; they only call `play()`/`pause()`.
- `seek()` is pre-metadata safe: before the library knows the duration it assigns
  `media.currentTime` directly, which the browser keeps as the start position.
- **Guard:** `playerSeam.guard.test.ts` fails on a `plyr` import (static, dynamic, `vi.mock`, CSS
  `@import`) anywhere except `plyrHandle.ts` and its test, and on a successor-library import
  (`@videojs/*` and friends) outside `lib/player/` and `components/player/`.
- Real playback is proven in a browser by `backend/tests/e2e/test_media_playback.py` (page object
  `backend/tests/e2e/media_player.py`); jsdom cannot play media.

## Video.js 10 rules

- **Only `components/player/*` and this folder import `@videojs/*`.** Everything else takes a
  `PlayerHandle`.
- **Import Video.js from `@videojs/html` entry points only.** `@videojs/store` depends on `react`
  for its React bindings, so `react` and `react-compiler-runtime` sit in `node_modules` and the
  licence inventory. They must never reach the client: `scripts/check-bundle-budget.mjs` fails
  the build on any `Symbol.for("react.` in `_app/immutable`, and on any Video.js code
  (`@videojs/`) in the first-load closure, since the player is route-scoped.
- The dependency is pinned exactly (`10.3.0`, no caret). Re-read the "Not supported" list in
  the Plyr migration guide (`node_modules/@videojs/html/docs/guides/migrate-from-plyr.md`) on
  every bump; the preferences, captions-on and `L` loop gaps it lists are closed below.
- In 10.3.0 `VideoPlayerElement['store']` is typed `any` (its `.d.ts` references a type the
  package does not export, hidden by `skipLibCheck`). Derive store types through
  `createPlayer` as `videojsHandle.test.ts` does, or a type check passes vacuously.

## Video.js 10 modules

| File               | Purpose                                                                                                                                                                                                                                                                                                                                                     |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `types.ts`         | `PlayerHandle`                                                                                                                                                                                                                                                                                                                                              |
| `videojsStore.ts`  | `VideojsStore`, the store slice these modules use (type-checked against the real video and audio stores), plus `captionTracks`/`captionsShowing`                                                                                                                                                                                                            |
| `videojsHandle.ts` | `createVideojsHandle(media, store)`: store actions once attached, the element before that. `seek` always writes the element at once (#647, `$lib/utils/mediaReady`)                                                                                                                                                                                         |
| `prefs.ts`         | Remembered volume, mute, rate and captions in `localStorage['opentr:playerPrefs']`; carries a viewer's `localStorage['plyr']` choices over once. `bindPlayerPrefs(store, media)` restores on attach and on each new caption track list, then saves changes. Captions default ON                                                                             |
| `i18n.ts`          | `PLAYER_LOCALE_PACKS` (one lazy `/register` chunk per app locale; English is built in) and `bindPlayerLocale($locale)`. `<media-i18n>` follows `<html lang>`/`dir`, which `$stores/locale` already sets                                                                                                                                                     |
| `hotkeys.ts`       | `PLAYER_HOTKEYS` for `<media-hotkey>` (page-wide on file detail, container-scoped on the mini player) and `attachLoopHotkey` for `L`, which Video.js has no action for. `j`/`l` are not bound to seek (owner question Q2)                                                                                                                                   |
| `videojsPlayer.ts` | `createVideojsPlayer(media, options)`: the `OwnedPlayer` for a media element inside `<video-player>`/`<audio-player>` (same shape as `createPlyrPlayer`). Binds prefs and the `L` key (document for `full`, the container for `mini`), calls `onReady` once the store attaches, and `destroy` releases both. The elements tear themselves down when removed |
| `fakeVideojs.ts`   | Test-only fake store and media element. jsdom cannot run the custom elements; Playwright proves the real ones                                                                                                                                                                                                                                               |

Values the prefs binding saves are read from the media element, not store state: the store
learns of a rate or caption-mode change from a media event a task later, so reading it right
after a restore would save the old value over the one just restored.
