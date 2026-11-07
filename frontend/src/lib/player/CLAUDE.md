# src/lib/player — the media-player seam (issue #1327)

The app talks to a player through `PlayerHandle` (`types.ts`), never to a library. Events stay
native `HTMLMediaElement` events on `handle.media`; the handle only covers imperative control.
Plan and capability matrix: `docs/design/1327_plyr_replacement_plan.md`.

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

| File               | Purpose                                                                                                                                                                                                                                                                         |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `types.ts`         | `PlayerHandle`                                                                                                                                                                                                                                                                  |
| `videojsStore.ts`  | `VideojsStore`, the store slice these modules use (type-checked against the real video and audio stores), plus `captionTracks`/`captionsShowing`                                                                                                                                |
| `videojsHandle.ts` | `createVideojsHandle(media, store)`: store actions once attached, the element before that. `seek` always writes the element at once (#647, `$lib/utils/mediaReady`)                                                                                                             |
| `prefs.ts`         | Remembered volume, mute, rate and captions in `localStorage['opentr:playerPrefs']`; carries a viewer's `localStorage['plyr']` choices over once. `bindPlayerPrefs(store, media)` restores on attach and on each new caption track list, then saves changes. Captions default ON |
| `i18n.ts`          | `PLAYER_LOCALE_PACKS` (one lazy `/register` chunk per app locale; English is built in) and `bindPlayerLocale($locale)`. `<media-i18n>` follows `<html lang>`/`dir`, which `$stores/locale` already sets                                                                         |
| `hotkeys.ts`       | `PLAYER_HOTKEYS` for `<media-hotkey>` (page-wide on file detail, container-scoped on the mini player) and `attachLoopHotkey` for `L`, which Video.js has no action for. `j`/`l` are not bound to seek (owner question Q2)                                                       |
| `fakeVideojs.ts`   | Test-only fake store and media element. jsdom cannot run the custom elements; Playwright proves the real ones                                                                                                                                                                   |

Values the prefs binding saves are read from the media element, not store state: the store
learns of a rate or caption-mode change from a media event a task later, so reading it right
after a restore would save the old value over the one just restored.
