/**
 * DEFECT THIS CATCHES (issue #649, "F1" in the audit): the mini player (search-results /
 * speaker preview) carried the #645/#647 bug that `VideoPlayer.seekToTime` was fixed for:
 * `applySeek` awaited `waitForMediaMetadata` BEFORE touching the media element's
 * `currentTime`, so a click that opened the preview at a non-zero `startTime` stalled for as
 * long as metadata took to arrive (up to the 15s fallback) instead of seeking immediately.
 * Per the HTML spec, assigning `currentTime` before `loadedmetadata` is not discarded — it
 * becomes the default playback start position — so the wait bought nothing and blocked the
 * very request it was waiting for.
 *
 * The initial seek was also gated behind a `canplay` listener (readyState >=
 * HAVE_FUTURE_DATA), which is STRICTER than the metadata gate above and had no fallback if
 * `canplay` never fired.
 *
 * Ported from the Plyr mini player (issue #1327): the cases drive the `PlayerHandle` seam, so
 * they hold whichever library builds the controls. The controls themselves are covered by
 * `MiniPlayer.controls.test.ts`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';
import type { PlayerHandle } from '$lib/player/types';

// The seam is faked, not the library behind it (issue #1327): these cases must keep passing
// whichever player library the adapter wraps.
vi.mock('$lib/player/videojsHandle', async () => {
  const { createFakePlayer } = await import('$lib/player/fakePlayer');
  return {
    createVideojsHandle: (media: HTMLMediaElement) =>
      createFakePlayer(media, { variant: 'mini', audio: media.tagName === 'AUDIO' }),
  };
});

import MiniPlayer from './MiniPlayer.svelte';
import { fakePlayers } from '$lib/player/fakePlayer';

/** A couple of microtask turns — enough for the synchronous portion of an
 * async function (everything before its first `await`) to have run, without
 * needing real timers or a metadata event that this suite never fires. */
async function flushMicrotasks() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  vi.clearAllMocks();
  fakePlayers.length = 0;
});

describe('initial seek timing', () => {
  it('assigns currentTime to the media element before metadata has ever arrived', async () => {
    const { container } = render(MiniPlayer, {
      props: {
        mediaUrl: 'blob:test-media',
        contentType: 'audio/mpeg',
        startTime: 42,
        autoplay: false,
        fileId: '',
      },
    });

    await flushMicrotasks();

    const media = container.querySelector('audio') as HTMLMediaElement;
    // jsdom never fires loadedmetadata/canplay in this suite — readyState
    // stays at HAVE_NOTHING (0) for the whole test. The old code awaited
    // metadata before this assignment, so under the pre-fix implementation
    // `currentTime` would still be 0 here.
    expect(media.readyState).toBe(0);
    expect(media.currentTime).toBe(42);
  });

  it('does not touch currentTime when startTime is 0 (nothing to seek to)', async () => {
    const { container } = render(MiniPlayer, {
      props: {
        mediaUrl: 'blob:test-media',
        contentType: 'audio/mpeg',
        startTime: 0,
        autoplay: false,
        fileId: '',
      },
    });

    await flushMicrotasks();

    const media = container.querySelector('audio') as HTMLMediaElement;
    expect(media.currentTime).toBe(0);
  });
});

/** Mount the mini player and return its element, the fake behind the seam and the events. */
async function mountMini(props: Record<string, unknown>) {
  const events = { play: vi.fn(), pause: vi.fn(), ended: vi.fn(), timeupdate: vi.fn() };
  const { container, component, rerender } = render(MiniPlayer, {
    props: { mediaUrl: 'blob:test-media', contentType: 'audio/mpeg', fileId: '', ...props },
    events,
  } as never);
  await flushMicrotasks();
  const media = container.querySelector('audio, video') as HTMLMediaElement;
  return { media, events, player: fakePlayers[0], component, container, rerender };
}

describe('the mini player over the player seam (#1327)', () => {
  it('builds one handle on its own element and hands out that handle', async () => {
    const { media, player, component, container } = await mountMini({ autoplay: false });

    expect(fakePlayers).toHaveLength(1);
    expect(player.media).toBe(media);
    expect(media.tagName).toBe('AUDIO');
    expect(container.querySelector('audio-player')).not.toBeNull();
    const handle = (component as unknown as { getPlayer: () => PlayerHandle | null }).getPlayer();
    expect(handle).toBe(player);
  });

  it('builds the video player for a video clip', async () => {
    const { media, container } = await mountMini({ contentType: 'video/mp4', autoplay: false });
    expect(media.tagName).toBe('VIDEO');
    expect(media.hasAttribute('playsinline')).toBe(true);
    expect(container.querySelector('video-player')).not.toBeNull();
    expect(container.querySelector('audio-player')).toBeNull();
  });

  it('starts playback through the handle once metadata lands (autoplay at an offset)', async () => {
    const { media, player } = await mountMini({ startTime: 42, autoplay: true });
    expect(media.currentTime).toBe(42);
    expect(player.play).not.toHaveBeenCalled();

    media.dispatchEvent(new Event('loadedmetadata'));
    await flushMicrotasks();
    expect(player.play).toHaveBeenCalledTimes(1);
  });

  it('pauses through the handle when the native clock reaches the clip end', async () => {
    const { media, player, events } = await mountMini({
      startTime: 2,
      endTime: 5,
      autoplay: false,
    });

    media.currentTime = 4.9;
    media.dispatchEvent(new Event('timeupdate'));
    expect(player.pause).not.toHaveBeenCalled();
    expect((events.timeupdate.mock.calls.at(-1)?.[0] as CustomEvent).detail).toEqual({
      currentTime: 4.9,
    });

    media.currentTime = 5;
    media.dispatchEvent(new Event('timeupdate'));
    expect(player.pause).toHaveBeenCalledTimes(1);
  });

  it('reports native playing, pause and ended to the page', async () => {
    const { media, events } = await mountMini({ autoplay: false });
    media.dispatchEvent(new Event('playing'));
    expect(events.play).toHaveBeenCalledTimes(1);
    media.dispatchEvent(new Event('pause'));
    expect(events.pause).toHaveBeenCalledTimes(1);
    media.dispatchEvent(new Event('ended'));
    expect(events.pause).toHaveBeenCalledTimes(2);
    expect(events.ended).toHaveBeenCalledTimes(1);
  });

  it('seeks through seek() without starting playback', async () => {
    const { media, player, component } = await mountMini({ autoplay: false });
    (component as unknown as { seek: (t: number) => void }).seek(7);
    await flushMicrotasks();
    expect(media.currentTime).toBe(7);
    media.dispatchEvent(new Event('loadedmetadata'));
    await flushMicrotasks();
    expect(player.play).not.toHaveBeenCalled();
  });

  it('hot-swaps a refreshed presigned URL and resumes where it was, still playing', async () => {
    const { media, player, rerender } = await mountMini({ autoplay: false });
    const load = vi.spyOn(media, 'load');
    media.currentTime = 12;
    Object.defineProperty(media, 'paused', { configurable: true, get: () => false });

    await rerender({ mediaUrl: 'blob:refreshed-media' });
    await flushMicrotasks();

    expect(media.querySelector('source')?.getAttribute('src')).toBe('blob:refreshed-media');
    expect(load).toHaveBeenCalledTimes(1);
    expect(media.currentTime).toBe(12);
    media.dispatchEvent(new Event('loadedmetadata'));
    await flushMicrotasks();
    expect(player.play).toHaveBeenCalledTimes(1);
  });
});
