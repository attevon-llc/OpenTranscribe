/**
 * DEFECT THIS CATCHES (issue #649, found via live browser verification of
 * this same PR): `seekToTime` used to read the raw media element through
 * `(player as any).media`, and early-returned entirely if `player` (the
 * Plyr instance) didn't exist yet: `if (!player) return;`.
 *
 * That is a race, not a safety check. `player` is set by this component's
 * OWN reactive statement (`$: if (videoUrl && mediaElement && !player)
 * initializePlyr()`), which is independent of — and can resolve AFTER — the
 * `?t=` deep-link handler in `routes/files/[id]/+page.svelte`, which calls
 * `videoPlayerComponent.seekToTime(...)` as soon as `videoPlayerComponent`
 * is bound. Once #649 removed the artificial 500ms sleep that used to sit in
 * front of that call (because the wait was itself the #645 bug), the two
 * reactive statements were left to race directly, and a `?t=` deep link on a
 * live 3-hour audio file landed at `currentTime: 0` instead of the
 * requested timestamp — confirmed live, not just in this suite.
 *
 * The fix: use the raw `mediaElement` (bound directly on the `<video>`/
 * `<audio>` tag, independent of Plyr) as the primary seek target, and treat
 * `player` as optional best-effort sync for Plyr's own progress bar.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';

// Plyr fails to construct here on purpose, so `player` stays permanently
// null while `mediaElement` and `videoUrl` are both valid — reproducing the
// exact state of the race (child's own init reactive statement having not
// (yet, or ever) resolved) without depending on Svelte's effect-scheduling
// order to land a real unit test in the right window.
vi.mock('plyr', () => {
  class ThrowingPlyr {
    constructor() {
      throw new Error('Plyr never constructs in this test — player stays null');
    }
  }
  return { default: ThrowingPlyr };
});
vi.mock('plyr/dist/plyr.css', () => ({}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import VideoPlayer from './VideoPlayer.svelte';

async function flushMicrotasks() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('seekToTime without a constructed Plyr instance', () => {
  it('still seeks the raw media element when `player` is null', async () => {
    const { container, component } = render(VideoPlayer, {
      props: {
        videoUrl: 'blob:test-video',
        file: { content_type: 'audio/mpeg' },
      },
    });
    await flushMicrotasks();

    const media = container.querySelector('#player') as HTMLMediaElement;
    expect(media).not.toBeNull();

    // Deliberately not awaited: jsdom never fires `loadedmetadata`, so the
    // full call would hang on `seekToTime`'s internal metadata wait (up to
    // its 15s timeout). The `currentTime` assignment under test happens in
    // the synchronous portion of the function, before that wait — a few
    // microtask turns is enough to observe it.
    void (component as unknown as { seekToTime: (t: number) => Promise<void> }).seekToTime(45);
    await flushMicrotasks();

    expect(media.currentTime).toBe(45);
  });

  it('does nothing (no throw) when neither player nor mediaElement exist', async () => {
    const { component } = render(VideoPlayer, { props: { videoUrl: '' } });
    await flushMicrotasks();

    await expect(
      (component as unknown as { seekToTime: (t: number) => Promise<void> }).seekToTime(45)
    ).resolves.toBeUndefined();
  });
});
