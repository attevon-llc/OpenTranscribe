/**
 * DEFECTS THESE CATCH (issue #645):
 *
 * 1. A seek requested before `loadedmetadata` used to block on the event with a
 *    hard-coded 15 s fallback *before* touching the media element, so a click
 *    landing in the page-load race window stalled for as long as the metadata
 *    took (up to 15 s of silence). `applyMediaSeek` must assign `currentTime`
 *    unconditionally — the element records it as the default playback start
 *    position, which is what makes the browser fetch the target byte range.
 * 2. The old inline waits never cleared their 15 s timer on the happy path, so
 *    every seek leaked a live timeout.
 * 3. They also never resolved on `error`, so a failed media load parked the
 *    caller for the full 15 s.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  waitForMediaMetadata,
  applyMediaSeek,
  HAVE_METADATA,
  METADATA_WAIT_TIMEOUT_MS,
} from './mediaReady';

/** Minimal stand-in for the bits of HTMLMediaElement the helpers touch. */
function fakeMedia(readyState = 0) {
  const listeners = new Map<string, Set<EventListener>>();
  const el = {
    readyState,
    _currentTime: 0,
    _throwOnSeek: false,
    get currentTime() {
      return this._currentTime;
    },
    set currentTime(v: number) {
      if (this._throwOnSeek) throw new DOMException('bad state', 'InvalidStateError');
      this._currentTime = v;
    },
    addEventListener(type: string, fn: EventListener) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type)!.add(fn);
    },
    removeEventListener(type: string, fn: EventListener) {
      listeners.get(type)?.delete(fn);
    },
    emit(type: string) {
      [...(listeners.get(type) ?? [])].forEach((fn) => fn(new Event(type) as Event));
    },
    listenerCount() {
      let n = 0;
      listeners.forEach((s) => (n += s.size));
      return n;
    },
  };
  return el as unknown as HTMLMediaElement & {
    emit(type: string): void;
    listenerCount(): number;
    _throwOnSeek: boolean;
  };
}

describe('applyMediaSeek', () => {
  it('assigns currentTime even while readyState is HAVE_NOTHING', () => {
    const media = fakeMedia(0);
    expect(applyMediaSeek(media, 191.35)).toBe(true);
    expect(media.currentTime).toBe(191.35);
  });

  it('reports failure instead of throwing when the engine rejects a pre-metadata seek', () => {
    const media = fakeMedia(0);
    media._throwOnSeek = true;
    expect(applyMediaSeek(media, 12)).toBe(false);
    expect(media.currentTime).toBe(0);
  });
});

describe('waitForMediaMetadata', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('resolves immediately when metadata is already present', async () => {
    const media = fakeMedia(HAVE_METADATA);
    await expect(waitForMediaMetadata(media)).resolves.toBe(true);
    expect(media.listenerCount()).toBe(0);
  });

  it('resolves true on loadedmetadata and removes every listener', async () => {
    const media = fakeMedia(0);
    const p = waitForMediaMetadata(media);
    expect(media.listenerCount()).toBeGreaterThan(0);
    (media as unknown as { readyState: number }).readyState = HAVE_METADATA;
    media.emit('loadedmetadata');
    await expect(p).resolves.toBe(true);
    expect(media.listenerCount()).toBe(0);
  });

  it('clears the fallback timer once metadata arrives', async () => {
    const media = fakeMedia(0);
    const p = waitForMediaMetadata(media);
    media.emit('loadedmetadata');
    await p;
    // If the timer leaked, a pending timer would still be queued here.
    expect(vi.getTimerCount()).toBe(0);
  });

  it('resolves false on a media error rather than parking for the full timeout', async () => {
    const media = fakeMedia(0);
    const p = waitForMediaMetadata(media);
    media.emit('error');
    await expect(p).resolves.toBe(false);
    expect(media.listenerCount()).toBe(0);
  });

  it('resolves false when the timeout elapses with no metadata', async () => {
    const media = fakeMedia(0);
    const p = waitForMediaMetadata(media);
    vi.advanceTimersByTime(METADATA_WAIT_TIMEOUT_MS);
    await expect(p).resolves.toBe(false);
    expect(media.listenerCount()).toBe(0);
  });

  it('honours a caller-supplied timeout', async () => {
    const media = fakeMedia(0);
    const p = waitForMediaMetadata(media, 500);
    vi.advanceTimersByTime(499);
    let settled = false;
    void p.then(() => (settled = true));
    await Promise.resolve();
    expect(settled).toBe(false);
    vi.advanceTimersByTime(1);
    await expect(p).resolves.toBe(false);
  });
});
