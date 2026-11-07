/**
 * Test doubles for the Video.js store and its media element. jsdom implements neither the
 * custom elements' store nor `HTMLMediaElement.textTracks`, so the player modules are unit
 * tested against these and proven end to end by Playwright (plan #1327, section 4.4).
 * Test-only: nothing outside `*.test.ts` may import it.
 */
import { vi } from 'vitest';
import type { VideojsStore } from './videojsStore';

interface FakeTrack {
  kind: string;
  mode: 'showing' | 'hidden' | 'disabled';
}

export interface FakeMedia {
  volume: number;
  muted: boolean;
  playbackRate: number;
  currentTime: number;
  loop: boolean;
  readyState: number;
  textTracks: FakeTrack[];
  play: ReturnType<typeof vi.fn>;
  pause: ReturnType<typeof vi.fn>;
  addEventListener: ReturnType<typeof vi.fn>;
  removeEventListener: ReturnType<typeof vi.fn>;
}

export function fakeMedia(overrides: Partial<FakeMedia> = {}): FakeMedia {
  return {
    volume: 1,
    muted: false,
    playbackRate: 1,
    currentTime: 0,
    loop: false,
    readyState: 4,
    textTracks: [],
    play: vi.fn(() => Promise.resolve()),
    pause: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    ...overrides,
  };
}

export const asMedia = (m: FakeMedia) => m as unknown as HTMLMediaElement;

/**
 * A store that writes through to *media* the way the real feature actions do (volume and
 * mute synchronously; rate and caption modes on the element), and notifies subscribers on
 * `notify()`, standing in for the store's own media-event sync.
 */
export function fakeStore(media: FakeMedia, { attached = true, captions = true } = {}) {
  const listeners = new Set<() => void>();
  const store = {
    target: attached ? { media } : null,
    subscribe: vi.fn((cb: () => void) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    }),
    play: vi.fn(() => Promise.resolve()),
    pause: vi.fn(),
    setVolume: vi.fn((v: number) => {
      const c = Math.max(0, Math.min(1, v));
      if (c > 0) media.muted = false;
      media.volume = c;
      return c;
    }),
    setMuted: vi.fn((m: boolean) => {
      media.muted = m;
      return m;
    }),
    setPlaybackRate: vi.fn((r: number) => {
      media.playbackRate = r;
    }),
    toggleSubtitles: captions
      ? vi.fn((force?: boolean) => {
          const tracks = media.textTracks.filter((t) => t.kind === 'captions');
          if (tracks.length === 0) return false;
          const show = force ?? !tracks.some((t) => t.mode === 'showing');
          tracks.forEach((t, i) => (t.mode = show && i === 0 ? 'showing' : 'disabled'));
          return show;
        })
      : undefined,
  };
  return {
    store: store as typeof store & VideojsStore,
    attach() {
      store.target = { media };
      this.notify();
    },
    notify() {
      for (const cb of [...listeners]) cb();
    },
    listenerCount: () => listeners.size,
  };
}
