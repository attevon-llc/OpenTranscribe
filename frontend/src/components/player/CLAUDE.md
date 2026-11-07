# src/components/player — the composed Video.js 10 player (issue #1327)

## Purpose

The player chrome, built from Video.js 10 UI elements (`@videojs/html/ui/*`) in our own markup
and design tokens instead of a packaged skin (plan `docs/design/1327_plyr_replacement_plan.md`,
section 4.1). With `lib/player/`, the only place allowed to import `@videojs/*`
(`lib/player/playerSeam.guard.test.ts`). Hosts never touch these elements' API: they render
their own `<video>`/`<audio>` in `MediaPlayer`'s slot and call `$lib/player/videojsPlayer`.

## Key files

- `MediaPlayer.svelte` — `<media-i18n>` > `<video-player>`/`<audio-player>` (with Plyr's speed
  list, `default-playback-rates` up to 4x) > `PlayerSurface`. Props `audio`, `variant`, `media`
  (the slotted element, for the caption overlay). Binds the player locale.
- `PlayerSurface.svelte` — the `<media-container>`: the slotted media, the caption overlay
  (video), the controls, `<media-hotkey>` per `PLAYER_HOTKEYS` (page-wide for `full`, container
  for `mini`), touch/mouse gestures (video), the screen-reader `<media-status-announcer>` and the
  media error dialog.
- `MediaControls.svelte` — centre play button (video), time slider, play, mute, volume (always
  on `full`/audio), time, captions (video), settings, PiP + AirPlay (`full` video), fullscreen
  (video). Tooltips link to triggers by per-instance ids.
- `SettingsMenu.svelte` — the settings button and one `<media-menu>` with Speed and Captions
  submenus (radio groups build their own items from player state).
- `CaptionOverlay.svelte` — draws the showing track's active cues from `getCueAsHTML()`, so the
  server's `<c.speaker>` keeps its colour in Firefox (`::cue(.speaker)` is unsupported there);
  hides the browser's own cue painting with `video::cue { visibility: hidden }`.
- `elements.ts` — every element registration and the icons used. A part without its import stays
  a plain element with no state, so add the import with the markup.
- `player.css` — all player styles, global under `.media-player` (runtime `data-*` state would
  be pruned by scoped CSS). Over the picture: the theme-invariant `--player-*` tokens; on app
  surfaces (audio, menu, tooltips): the ordinary surface tokens.

## Conventions / patterns

- **Labels are Video.js's**, translated by its locale packs (`$lib/player/i18n`), never app
  copy: buttons get state-aware `aria-label`s, headings use `<media-text token>`. Adding a
  control needs no locale JSON; adding app-owned text does (all 12 locales).
- **RTL:** controls keep their order (Video.js design), menus, chevrons and submenus mirror;
  the sliders stay physically left-to-right (`direction: ltr`). Logical properties only.
- An unavailable control gets the `hidden` attribute; `.control[hidden]` must stay
  `display: none` because `.control` sets a display.
- Menus and tooltips are positioned by the library (Popover API top layer); never give them a
  `position` (`floatingPanel.convention.test.ts`).
- jsdom runs these elements and their store (`VideoPlayer.controls.test.ts` drives the real
  ones: Arabic labels, the status announcement, 4x, page-wide shortcuts that skip typing).
  Layout, mirroring and real playback are in `backend/tests/e2e/test_media_playback.py`.
