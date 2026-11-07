import { describe, it, expect, vi } from 'vitest';
import { listenToMedia, mediaDuration } from './mediaEvents';

describe('listenToMedia', () => {
  it('delivers native media events and removes exactly its own listeners', () => {
    const media = document.createElement('audio');
    const onTime = vi.fn();
    const onPause = vi.fn();
    const other = vi.fn();
    media.addEventListener('timeupdate', other);

    const stop = listenToMedia(media, { timeupdate: onTime, pause: onPause });
    media.dispatchEvent(new Event('timeupdate'));
    media.dispatchEvent(new Event('pause'));
    expect(onTime).toHaveBeenCalledTimes(1);
    expect(onPause).toHaveBeenCalledTimes(1);

    stop();
    media.dispatchEvent(new Event('timeupdate'));
    media.dispatchEvent(new Event('pause'));
    expect(onTime).toHaveBeenCalledTimes(1);
    expect(onPause).toHaveBeenCalledTimes(1);
    expect(other).toHaveBeenCalledTimes(2);
  });
});

describe('mediaDuration', () => {
  function withDuration(value: number): HTMLMediaElement {
    const media = document.createElement('video');
    Object.defineProperty(media, 'duration', { value });
    return media;
  }

  it('is 0 before metadata (NaN) and for an unbounded stream (Infinity)', () => {
    expect(mediaDuration(withDuration(NaN))).toBe(0);
    expect(mediaDuration(withDuration(Infinity))).toBe(0);
  });

  it('is the element duration once known', () => {
    expect(mediaDuration(withDuration(93.5))).toBe(93.5);
  });
});
