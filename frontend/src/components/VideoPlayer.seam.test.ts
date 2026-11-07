/**
 * The file-detail player talks to its player library only through the seam (issue #1327).
 *
 * `VideoPlayer` builds its player with the adapter's factory and reads playback from native
 * events on its own `<video>`/`<audio>` element, so the library behind the seam can be
 * swapped without changing what the page receives: `loadedmetadata` once the controls are
 * built, `timeupdate` with the element's clock, and `play`/`pause`/`ended`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';
import { tick } from 'svelte';

vi.mock('$lib/player/plyrHandle', async () => {
  const { createFakePlayer } = await import('$lib/player/fakePlayer');
  return { createPlyrPlayer: createFakePlayer, viewerTurnedCaptionsOff: () => false };
});
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import VideoPlayer from './VideoPlayer.svelte';
import { fakePlayers } from '$lib/player/fakePlayer';

beforeEach(() => {
  fakePlayers.length = 0;
});

async function mount(file: Record<string, unknown>) {
  const events = {
    loadedmetadata: vi.fn(),
    timeupdate: vi.fn(),
    play: vi.fn(),
    pause: vi.fn(),
    ended: vi.fn(),
  };
  const { container } = render(VideoPlayer, {
    props: { videoUrl: 'blob:media', file },
    events,
  } as never);
  await tick();
  const media = container.querySelector('#player') as HTMLMediaElement;
  return { media, events, player: fakePlayers[0] };
}

const detail = (fn: ReturnType<typeof vi.fn>) => (fn.mock.calls.at(-1)?.[0] as CustomEvent).detail;

describe('VideoPlayer over the player seam', () => {
  it('builds one full player on its own element, video or audio', async () => {
    const video = await mount({ content_type: 'video/mp4' });
    expect(fakePlayers).toHaveLength(1);
    expect(video.player.media).toBe(video.media);
    expect(video.media.tagName).toBe('VIDEO');
    expect(video.player.options).toMatchObject({ variant: 'full', audio: false });

    fakePlayers.length = 0;
    const audio = await mount({ content_type: 'audio/mpeg' });
    expect(audio.media.tagName).toBe('AUDIO');
    expect(audio.player.options).toMatchObject({ variant: 'full', audio: true });
  });

  it('reports loadedmetadata once the player is ready', async () => {
    const { media, events, player } = await mount({ content_type: 'audio/mpeg' });
    Object.defineProperty(media, 'duration', { value: 93 });
    expect(events.loadedmetadata).not.toHaveBeenCalled();

    player.ready();
    expect(detail(events.loadedmetadata)).toEqual({ duration: 93 });
  });

  it('forwards the element clock from native timeupdate events', async () => {
    const { media, events } = await mount({ content_type: 'audio/mpeg' });
    media.currentTime = 12.5;
    media.dispatchEvent(new Event('timeupdate'));
    // Before metadata the duration is NaN; the page gets 0, as it always has.
    expect(detail(events.timeupdate)).toEqual({ currentTime: 12.5, duration: 0 });
  });

  it('forwards native play, pause and ended', async () => {
    const { media, events } = await mount({ content_type: 'audio/mpeg' });
    media.dispatchEvent(new Event('play'));
    media.dispatchEvent(new Event('pause'));
    media.dispatchEvent(new Event('ended'));
    expect(events.play).toHaveBeenCalledTimes(1);
    expect(events.pause).toHaveBeenCalledTimes(1);
    expect(events.ended).toHaveBeenCalledTimes(1);
  });

  it('destroys the player and stops listening when it unmounts', async () => {
    const events = { timeupdate: vi.fn() };
    const { container, unmount } = render(VideoPlayer, {
      props: { videoUrl: 'blob:media', file: { content_type: 'audio/mpeg' } },
      events,
    } as never);
    await tick();
    const media = container.querySelector('#player') as HTMLMediaElement;
    const player = fakePlayers[0];

    unmount();
    expect(player.destroy).toHaveBeenCalledTimes(1);
    media.dispatchEvent(new Event('timeupdate'));
    expect(events.timeupdate).not.toHaveBeenCalled();
  });
});
