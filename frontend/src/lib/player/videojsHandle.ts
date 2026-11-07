/**
 * `PlayerHandle` over a Video.js 10 player: the `<video>`/`<audio>` element the app renders
 * and the `store` of the `<video-player>`/`<audio-player>` around it.
 * Store actions: https://videojs.org/docs/framework/html/reference/api/player-store
 *
 * Imperative calls go through the store's feature actions once it has attached, so the
 * controls stay in step; before that the actions throw ("Actions need an attached media
 * target", https://videojs.org/docs/framework/html/guides/user-preferences), so the handle
 * talks to the element directly.
 *
 * Seeking always writes the element: the store's `seek()` waits for `loadedmetadata`
 * before assigning, while assigning early lets the browser request the target range sooner
 * (#647, `$lib/utils/mediaReady`). The store follows the element's `seeking`/`seeked`
 * events, so the time slider still moves; no resync is needed (plan row 8).
 */
import { applyMediaSeek, waitForMediaMetadata } from '$lib/utils/mediaReady';
import type { PlayerHandle } from './types';
import { captionTracks, captionsShowing, type VideojsStore } from './videojsStore';

export function createVideojsHandle(media: HTMLMediaElement, store: VideojsStore): PlayerHandle {
  const attached = () => store.target !== null;

  const seek = (time: number) => {
    if (!Number.isFinite(time)) return;
    const target = Math.max(0, time);
    if (applyMediaSeek(media, target)) return;
    void waitForMediaMetadata(media).then((ready) => {
      if (ready) applyMediaSeek(media, target);
    });
  };

  const setCaptionsVisible = (visible: boolean) => {
    if (attached() && store.toggleSubtitles) {
      store.toggleSubtitles(visible);
      return;
    }
    // Same rule as the store's action: at most one caption/subtitle track showing.
    captionTracks(media).forEach((track, i) => {
      track.mode = visible && i === 0 ? 'showing' : 'disabled';
    });
  };

  return {
    media,
    play: () => (attached() ? store.play() : media.play()),
    pause: () => (attached() ? store.pause() : media.pause()),
    seek,
    setPlaybackRate: (rate) => {
      if (attached()) store.setPlaybackRate(rate);
      else media.playbackRate = rate;
    },
    setCaptionsVisible,
    get captionsVisible() {
      return captionsShowing(media);
    },
  };
}
