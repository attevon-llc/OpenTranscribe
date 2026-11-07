/**
 * `PlayerHandle` over a Video.js 10 store (plan #1327 section 4.2). jsdom cannot run the
 * real custom elements, so this drives a fake store; the type-level checks below keep the
 * fake's contract (`VideojsStore`) tied to the real video and audio player stores.
 *
 * The store types are derived through `createPlayer` (the documented typed entry point,
 * https://videojs.org/docs/framework/html/reference/api/html-create-player) because in
 * 10.3.0 `VideoPlayerElement['store']` resolves to `any`: its declaration references
 * `index_d_exports.VideoPlayerStore`, which the published `.d.ts` does not export (hidden by
 * `skipLibCheck`). A check against `any` would pass whatever the store looked like.
 */
import { describe, expect, expectTypeOf, it } from 'vitest';
import type { createPlayer } from '@videojs/html';
import type { videoFeatures } from '@videojs/html/video';
import type { audioFeatures } from '@videojs/html/audio';
import type { PlayerHandle } from './types';
import type { VideojsStore } from './videojsStore';
import { createVideojsHandle } from './videojsHandle';
import { asMedia, fakeMedia, fakeStore } from './fakeVideojs';

type VideoPlayer = ReturnType<typeof createPlayer<typeof videoFeatures>>['PlayerElement'];
type AudioPlayer = ReturnType<typeof createPlayer<typeof audioFeatures>>['PlayerElement'];
type RealVideoStore = InstanceType<VideoPlayer>['store'];
type RealAudioStore = InstanceType<AudioPlayer>['store'];

const setup = (opts: { attached?: boolean; captions?: boolean } = {}) => {
  const media = fakeMedia();
  const f = fakeStore(media, opts);
  return { media, f, handle: createVideojsHandle(asMedia(media), f.store) };
};

describe('createVideojsHandle', () => {
  it('is a PlayerHandle and the real stores satisfy the slice it uses', () => {
    expectTypeOf(createVideojsHandle).returns.toEqualTypeOf<PlayerHandle>();
    expectTypeOf<RealVideoStore>().not.toBeAny();
    expectTypeOf<RealVideoStore>().toExtend<VideojsStore>();
    expectTypeOf<RealAudioStore>().toExtend<VideojsStore>();
    const { media, handle } = setup();
    expect(handle.media).toBe(media);
  });

  it('plays and pauses through the store once attached', async () => {
    const { media, f, handle } = setup();
    await handle.play();
    handle.pause();
    expect(f.store.play).toHaveBeenCalledTimes(1);
    expect(f.store.pause).toHaveBeenCalledTimes(1);
    expect(media.play).not.toHaveBeenCalled();
  });

  it('falls back to the media element before the store attaches (actions throw then)', async () => {
    const { media, f, handle } = setup({ attached: false });
    await handle.play();
    handle.pause();
    handle.setPlaybackRate(1.25);
    expect(media.play).toHaveBeenCalledTimes(1);
    expect(media.pause).toHaveBeenCalledTimes(1);
    expect(media.playbackRate).toBe(1.25);
    expect(f.store.play).not.toHaveBeenCalled();
    expect(f.store.setPlaybackRate).not.toHaveBeenCalled();
  });

  it('sets the speed through the store, including 4x', () => {
    const { media, f, handle } = setup();
    handle.setPlaybackRate(4);
    expect(f.store.setPlaybackRate).toHaveBeenCalledWith(4);
    expect(media.playbackRate).toBe(4);
  });

  it('seeks the element immediately, even before metadata (#647)', () => {
    const { media, handle } = setup({ attached: false });
    media.readyState = 0;
    handle.seek(42.5);
    expect(media.currentTime).toBe(42.5);
  });

  it('re-applies a seek the engine refused before metadata once metadata arrives', async () => {
    let value = 0;
    let refuse = true;
    const listeners: Record<string, () => void> = {};
    const media = fakeMedia({ readyState: 0 });
    Object.defineProperty(media, 'currentTime', {
      get: () => value,
      set: (t: number) => {
        if (refuse) throw new DOMException('not yet', 'InvalidStateError');
        value = t;
      },
    });
    media.addEventListener.mockImplementation((type: string, cb: () => void) => {
      listeners[type] = cb;
    });
    const handle = createVideojsHandle(asMedia(media), fakeStore(media).store);
    handle.seek(30);
    expect(value).toBe(0);
    refuse = false;
    media.readyState = 1;
    listeners.loadedmetadata();
    await Promise.resolve();
    expect(value).toBe(30);
  });

  it('ignores a non-finite seek target and clamps a negative one to 0', () => {
    const { media, handle } = setup();
    media.currentTime = 5;
    handle.seek(Number.NaN);
    expect(media.currentTime).toBe(5);
    handle.seek(-3);
    expect(media.currentTime).toBe(0);
  });

  it('shows and hides captions through toggleSubtitles and reads them from the track', () => {
    const { media, f, handle } = setup();
    media.textTracks.push({ kind: 'captions', mode: 'disabled' });
    expect(handle.captionsVisible).toBe(false);
    handle.setCaptionsVisible(true);
    expect(f.store.toggleSubtitles).toHaveBeenLastCalledWith(true);
    expect(handle.captionsVisible).toBe(true);
    handle.setCaptionsVisible(false);
    expect(handle.captionsVisible).toBe(false);
  });

  it('sets the track mode directly when the store cannot (not attached)', () => {
    const { media, handle } = setup({ attached: false });
    media.textTracks.push({ kind: 'chapters', mode: 'disabled' });
    media.textTracks.push({ kind: 'captions', mode: 'disabled' });
    handle.setCaptionsVisible(true);
    expect(media.textTracks.map((t) => t.mode)).toEqual(['disabled', 'showing']);
    expect(handle.captionsVisible).toBe(true);
    handle.setCaptionsVisible(false);
    expect(media.textTracks.map((t) => t.mode)).toEqual(['disabled', 'disabled']);
  });

  it('reports no captions on an audio player', () => {
    const { handle } = setup({ captions: false });
    handle.setCaptionsVisible(true);
    expect(handle.captionsVisible).toBe(false);
  });
});
