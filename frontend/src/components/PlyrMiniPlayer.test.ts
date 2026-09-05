/**
 * DEFECT THIS CATCHES (issue #649, "F1" in the audit): `PlyrMiniPlayer` — the
 * search-results / speaker preview player — carried the #645/#647 bug that
 * `VideoPlayer.seekToTime` was fixed for: `applySeek` awaited
 * `waitForMediaMetadata` BEFORE touching the media element's `currentTime`,
 * so a click that opened the preview player at a non-zero `startTime` stalled
 * for as long as metadata took to arrive (up to the 15s fallback) instead of
 * seeking immediately. Per the HTML spec, assigning `currentTime` before
 * `loadedmetadata` is not discarded — it becomes the default playback start
 * position — so the wait bought nothing and blocked the very request it was
 * waiting for.
 *
 * The initial seek was also gated behind a `canplay` listener (readyState >=
 * HAVE_FUTURE_DATA), which is STRICTER than the metadata gate above and had
 * no fallback if `canplay` never fired.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('plyr', () => {
  class FakePlyr {
    media: HTMLMediaElement;
    private listeners: Record<string, Array<() => void>> = {};
    constructor(el: HTMLMediaElement) {
      this.media = el;
    }
    on(event: string, cb: () => void) {
      (this.listeners[event] ||= []).push(cb);
    }
    get currentTime() {
      return this.media.currentTime;
    }
    set currentTime(v: number) {
      this.media.currentTime = v;
    }
    get duration() {
      return this.media.duration || 0;
    }
    play() {
      return Promise.resolve();
    }
    pause() {}
    destroy() {}
  }
  return { default: FakePlyr };
});

vi.mock('plyr/dist/plyr.css', () => ({}));

import PlyrMiniPlayer from './PlyrMiniPlayer.svelte';

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
});

describe('initial seek timing', () => {
  it('assigns currentTime to the media element before metadata has ever arrived', async () => {
    const { container } = render(PlyrMiniPlayer, {
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
    const { container } = render(PlyrMiniPlayer, {
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
