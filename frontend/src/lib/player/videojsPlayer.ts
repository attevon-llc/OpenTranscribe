/**
 * The Video.js 10 player behind the seam: what a host gets for its `<video>`/`<audio>` once
 * that element sits inside a `<video-player>`/`<audio-player>` (rendered by
 * `components/player/MediaPlayer.svelte`).
 *
 * Same shape as the Plyr adapter (`createPlyrPlayer(media, options)`), so a host swaps one
 * import. It wires the three app-side pieces Video.js leaves to the app:
 * - remembered volume, mute, speed and captions (`bindPlayerPrefs`, which also turns captions
 *   on unless the viewer switched them off), per
 *   https://videojs.org/docs/framework/html/guides/user-preferences;
 * - the `L` loop shortcut (`attachLoopHotkey`): page-wide on the file page, container-scoped
 *   on the mini player, matching where its `<media-hotkey>` bindings listen
 *   (https://videojs.org/docs/framework/html/reference/components/hotkey);
 * - `onReady`, once the store has attached to the media (actions throw before that).
 *
 * The player element is found from the media element (`closest`), so this module never
 * creates or registers a custom element: the host's markup does.
 */
import { attachLoopHotkey } from './hotkeys';
import { bindPlayerPrefs } from './prefs';
import type { OwnedPlayer, PlayerOptions } from './types';
import { createVideojsHandle } from './videojsHandle';
import type { VideojsStore } from './videojsStore';

const PLAYER_ELEMENTS = 'video-player, audio-player';

type PlayerElement = Element & { readonly store?: VideojsStore };

/** Build the player for *media*. Throws when *media* is not inside a Video.js player element. */
export function createVideojsPlayer(media: HTMLMediaElement, options: PlayerOptions): OwnedPlayer {
  const host = media.closest(PLAYER_ELEMENTS) as PlayerElement | null;
  const store = host?.store;
  if (!host || !store) {
    throw new Error(
      'createVideojsPlayer: the media element is not inside <video-player>/<audio-player>'
    );
  }

  const handle = createVideojsHandle(media, store);
  const loopScope: EventTarget =
    options.variant === 'full' ? document : media.closest('media-container') ?? host;
  const cleanups = [bindPlayerPrefs(store, media), attachLoopHotkey(loopScope, media)];

  if (options.onReady) {
    const onReady = options.onReady;
    let fired = false;
    const fireOnce = () => {
      if (fired || store.target === null) return;
      fired = true;
      onReady();
    };
    cleanups.push(store.subscribe(fireOnce));
    fireOnce();
  }

  return {
    media,
    play: handle.play,
    pause: handle.pause,
    seek: handle.seek,
    setPlaybackRate: handle.setPlaybackRate,
    setCaptionsVisible: handle.setCaptionsVisible,
    get captionsVisible() {
      return handle.captionsVisible;
    },
    // The elements tear themselves down when removed from the page (two frames later); this
    // releases only what the app attached.
    destroy: () => {
      for (const cleanup of cleanups.splice(0)) cleanup();
    },
  };
}
