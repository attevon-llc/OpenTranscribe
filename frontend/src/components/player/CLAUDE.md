# src/components/player — the composed Video.js 10 player (issue #1327)

## Purpose

The player chrome for both players, built from Video.js 10 UI elements (`@videojs/html/ui/*`)
in our own markup and design tokens instead of a packaged skin (plan
`docs/design/1327_plyr_replacement_plan.md`, section 4.1). With `lib/player/`, the only place
allowed to import `@videojs/*` (`lib/player/playerSeam.guard.test.ts`). Hosts never touch these
elements' API: they render their own `<video>`/`<audio>` and take a `PlayerHandle`.

## Key files

- `MediaPlayer.svelte` — the file-detail player: `<media-i18n>` > `<video-player>`/`<audio-player>`
  (with the speed list, `default-playback-rates` up to 4x) > `PlayerSurface`. Props `audio`,
  `variant`, `media` (the slotted element, for the caption overlay). Binds the player locale.
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
- `MiniPlayer.svelte` — the floating search / speaker preview (`FloatingPreviewPlayer`).
  `<audio-player>` or `<video-player>` around our own `<audio>`/`<video>`; play, time,
  timeline, mute, volume (audio only, as before), a speed menu (0.5x to 4x) and fullscreen
  (video). Container-scoped shortcuts, `L` loop, remembered preferences, translated labels
  and a status live region. Where the browser has no element Fullscreen API it renders its own
  full-window toggle (`lib/player/fullWindow.ts`). App behaviour kept here: clip start/end,
  autoplay at an offset, the presigned-URL hot swap, the seeking overlay, the audio waveform.
- `elements.ts` / `miniPlayerElements.ts` — the element registrations and icons each player
  renders (the mini player registers only its own, to keep the search/speakers routes small). A
  part without its import stays a plain element with no state, so add the import with the markup.
- `player.css` — the file player's styles, global under `.media-player` (runtime `data-*` state
  would be pruned by scoped CSS). Over the picture: the theme-invariant `--player-*` tokens; on
  app surfaces (audio, menu, tooltips): the ordinary surface tokens.

## Conventions / patterns

- **Labels are Video.js's**, translated by its locale packs (`$lib/player/i18n`), never app
  copy: buttons get state-aware `aria-label`s, headings use `<media-text token>`. Adding a
  control needs no locale JSON; adding app-owned text does (all 12 locales).
- Style Video.js parts with `:global()` under the component root: their state is in `data-*`
  attributes Svelte cannot see (videojs.org `guides/svelte`). Tokens only, logical properties
  only. Library parts ship no styles; `display: contents` on the provider and grouping elements,
  and hidden popups (`:not([data-open])`), are ours to declare.
- **RTL:** controls keep their order (Video.js design), menus, chevrons and submenus mirror;
  the sliders stay physically left-to-right (`direction: ltr`, videojs.org
  `guides/internationalization`, "Set text direction"). Logical properties only.
- An unavailable control gets the `hidden` attribute; `.control[hidden]` must stay
  `display: none` because `.control` sets a display.
  The same trap applies to a closed settings submenu: `.menu-view[hidden]` must stay
  `display: none` because `.menu-view` sets `display: grid`.
- The time readout (`.time`) is `direction: ltr` with `unicode-bidi: isolate`: inheriting RTL
  reorders it to "total / elapsed" and moves its start margin against the volume slider
  (`test_time_readout_stays_elapsed_then_total_in_rtl`).
- Menus and tooltips are positioned by the library (Popover API top layer); never give them a
  `position` (`floatingPanel.convention.test.ts`).
- Tests: jsdom runs these elements and their store. `VideoPlayer.controls.test.ts` and
  `MiniPlayer.controls.test.ts` drive the real ones (labels in all 12 locales, announcements,
  hotkey scope, 4x, full-window fallback); `MiniPlayer.test.ts` drives the seam with a fake
  handle (#649 seek timing, clip end, hot swap). Layout, mirroring and real playback are in
  `backend/tests/e2e/test_media_playback.py`.
