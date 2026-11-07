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
- `fakePlayer.ts` — test-only `createFakePlayer` + `fakePlayers`; host tests mock
  `$lib/player/plyrHandle` with it instead of mocking the library.

## Conventions / patterns

- **Events are native.** Hosts listen on their own `<video>`/`<audio>` element
  (`timeupdate`, `play`, `pause`, `playing`, `seeking`, `seeked`, `ended`, `error`) through
  `listenToMedia`, never through the library's event API. Every candidate library keeps those.
- **Hosts:** `components/VideoPlayer.svelte` (variant `full`) and
  `components/PlyrMiniPlayer.svelte` (variant `mini`). `FloatingPreviewPlayer.getPlayer()` hands
  the search and speakers pages a `PlayerHandle`; they only call `play()`/`pause()`.
- `seek()` is pre-metadata safe: before the library knows the duration it assigns
  `media.currentTime` directly, which the browser keeps as the start position.
- **Guard:** `playerSeam.guard.test.ts` fails on a `plyr` import (static, dynamic, `vi.mock`, CSS
  `@import`) anywhere except `plyrHandle.ts` and its test, and on a successor-library import
  (`@videojs/*` and friends) outside `lib/player/` and `components/player/`.
- Real playback is proven in a browser by `backend/tests/e2e/test_media_playback.py` (page object
  `backend/tests/e2e/media_player.py`); jsdom cannot play media.
