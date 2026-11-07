/**
 * A fake `OwnedPlayer` for component tests (issue #1327).
 *
 * Host tests mock the adapter module (`vi.mock('$lib/player/plyrHandle', ...)`) with
 * `createFakePlayer`, so they exercise the seam instead of whichever library is behind it.
 * jsdom cannot run a real player; real playback is proven by `backend/tests/e2e/test_media_playback.py`.
 * Test-only: no production module imports this.
 */
import { vi, type Mock } from 'vitest';
import type { OwnedPlayer, PlayerOptions } from './types';

export interface FakePlayer extends OwnedPlayer {
  readonly options: PlayerOptions;
  play: Mock<() => Promise<void>>;
  pause: Mock<() => void>;
  seek: Mock<(time: number) => void>;
  setPlaybackRate: Mock<(rate: number) => void>;
  setCaptionsVisible: Mock<(visible: boolean) => void>;
  destroy: Mock<() => void>;
  /** What the library does once its controls are built. */
  ready(): void;
}

/** Every fake built since the last `fakePlayers.length = 0`, oldest first. */
export const fakePlayers: FakePlayer[] = [];

/** Seeks really move `media.currentTime`, so a host's own reads of the element stay truthful. */
export function createFakePlayer(media: HTMLMediaElement, options: PlayerOptions): FakePlayer {
  let captionsVisible = false;
  const player: FakePlayer = {
    media,
    options,
    play: vi.fn((): Promise<void> => Promise.resolve()),
    pause: vi.fn((): void => {}),
    seek: vi.fn((time: number): void => {
      media.currentTime = time;
    }),
    setPlaybackRate: vi.fn((_rate: number): void => {}),
    setCaptionsVisible: vi.fn((visible: boolean): void => {
      captionsVisible = visible;
    }),
    get captionsVisible() {
      return captionsVisible;
    },
    destroy: vi.fn((): void => {}),
    ready(): void {
      options.onReady?.();
    },
  };
  fakePlayers.push(player);
  return player;
}
