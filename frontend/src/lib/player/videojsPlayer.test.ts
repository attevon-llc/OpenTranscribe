/**
 * `createVideojsPlayer`: the adapter a host calls for its media element (plan #1327, lane C).
 *
 * The player element here is a plain `<video-player>` carrying the fake store, so these tests
 * pin what the adapter adds (prefs, the `L` key, `onReady`, teardown) without the library's
 * own elements; `components/player/MediaPlayer.test.ts` drives the real ones.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fakeStore, type FakeMedia } from './fakeVideojs';
import { PLAYER_PREFS_KEY } from './prefs';
import { createVideojsPlayer as create } from './videojsPlayer';
import type { OwnedPlayer } from './types';

// A file-detail player listens on the document, so every test tears its players down.
const built: OwnedPlayer[] = [];
const createVideojsPlayer: typeof create = (media, options) => {
  const player = create(media, options);
  built.push(player);
  return player;
};

function mountPlayer({ attached = false } = {}) {
  const host = document.createElement('video-player');
  const container = document.createElement('media-container');
  const media = document.createElement('video');
  container.append(media);
  host.append(container);
  document.body.append(host);
  const f = fakeStore(media as unknown as FakeMedia, { attached, captions: false });
  Object.defineProperty(host, 'store', { value: f.store });
  return { host, container, media, f };
}

const pressL = (target: EventTarget) =>
  target.dispatchEvent(new KeyboardEvent('keydown', { key: 'l', bubbles: true, cancelable: true }));

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  for (const player of built.splice(0)) player.destroy();
  document.body.innerHTML = '';
});

describe('createVideojsPlayer', () => {
  it('refuses a media element that is not inside a player element', () => {
    const media = document.createElement('video');
    document.body.append(media);
    expect(() => createVideojsPlayer(media, { variant: 'full', audio: false })).toThrow(
      /not inside <video-player>/
    );
  });

  it('reports ready once, when the store attaches, not before', () => {
    const { media, f } = mountPlayer();
    const onReady = vi.fn();
    createVideojsPlayer(media, { variant: 'full', audio: false, onReady });
    expect(onReady).not.toHaveBeenCalled();

    f.attach();
    f.notify();
    expect(onReady).toHaveBeenCalledTimes(1);
  });

  it('restores the remembered volume and speed on attach', () => {
    localStorage.setItem(
      PLAYER_PREFS_KEY,
      JSON.stringify({ volume: 0.4, muted: false, rate: 1.5, captions: true })
    );
    const { media, f } = mountPlayer();
    createVideojsPlayer(media, { variant: 'full', audio: false });
    f.attach();

    expect(media.volume).toBe(0.4);
    expect(media.playbackRate).toBe(1.5);
  });

  it('toggles loop on L anywhere on the page for the file-detail player', () => {
    const { media } = mountPlayer({ attached: true });
    createVideojsPlayer(media, { variant: 'full', audio: false });
    const elsewhere = document.createElement('div');
    document.body.append(elsewhere);

    pressL(elsewhere);
    expect(media.loop).toBe(true);
    pressL(elsewhere);
    expect(media.loop).toBe(false);
  });

  it('keeps the mini player L key inside its own container', () => {
    const { media, container } = mountPlayer({ attached: true });
    createVideojsPlayer(media, { variant: 'mini', audio: false });

    pressL(document.body);
    expect(media.loop).toBe(false);
    pressL(container);
    expect(media.loop).toBe(true);
  });

  it('releases the key handler and the store subscription on destroy', () => {
    const { media, f } = mountPlayer({ attached: true });
    const player = createVideojsPlayer(media, { variant: 'full', audio: false, onReady: vi.fn() });
    expect(f.listenerCount()).toBe(2);

    player.destroy();
    expect(f.listenerCount()).toBe(0);
    pressL(document.body);
    expect(media.loop).toBe(false);
  });

  it('is the handle over the element it was given', async () => {
    const { media, f } = mountPlayer({ attached: true });
    const player = createVideojsPlayer(media, { variant: 'full', audio: false });
    expect(player.media).toBe(media);
    await player.play();
    player.setPlaybackRate(2);
    expect(f.store.play).toHaveBeenCalledTimes(1);
    expect(media.playbackRate).toBe(2);
    expect(player.captionsVisible).toBe(false);
  });
});
