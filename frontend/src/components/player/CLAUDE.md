# src/components/player — the Video.js 10 player UI (issue #1327)

## Purpose

The players' visible controls, composed from `@videojs/html` custom elements in our own
markup and design tokens (no packaged skin; plan `docs/design/1327_plyr_replacement_plan.md`
section 4.1). With `lib/player/`, the only place `@videojs/*` may be imported
(`lib/player/playerSeam.guard.test.ts`). Everything else takes a `PlayerHandle`.

## Key files

- `MiniPlayer.svelte` — the floating search / speaker preview (`FloatingPreviewPlayer`).
  `<audio-player>` or `<video-player>` around our own `<audio>`/`<video>`; play, time,
  timeline, mute, volume (audio only, as before), a speed menu (0.5x to 4x) and fullscreen
  (video). Container-scoped shortcuts, `L` loop, remembered preferences, translated labels
  and a status live region. Where the browser has no element Fullscreen API it renders its own
  full-window toggle (`lib/player/fullWindow.ts`). App behaviour kept here: clip start/end,
  autoplay at an offset, the presigned-URL hot swap, the seeking overlay, the audio waveform.
- `miniPlayerElements.ts` — side-effect imports registering only the elements and icons the
  mini player renders.

## Conventions

- Style Video.js parts with `:global()` under the component root: their state is in `data-*`
  attributes Svelte cannot see (videojs.org `guides/svelte`). Tokens only, logical
  properties only; sliders pin `direction: ltr` because their value scale is physical by
  design (videojs.org `guides/internationalization`, "Set text direction").
- Library parts ship no styles; `display: contents` on the provider and grouping elements,
  and hidden popups (`:not([data-open])`), are ours to declare.
- Tests: `MiniPlayer.test.ts` drives the seam with a fake handle (#649 seek timing, clip end,
  hot swap); `MiniPlayer.controls.test.ts` renders the real elements in jsdom (labels in all
  12 locales, announcements, hotkey scope, 4x, full-window fallback).
