import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createPreviewCache } from './previewCache';
import type { SpeakerMediaPreviewData } from '$lib/api/speakerClusters';

const data = (id: string) => ({ speaker_uuid: id }) as SpeakerMediaPreviewData;

describe('createPreviewCache', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('serves a prefetched preview without a second fetch', async () => {
    const fetch = vi.fn(async (id: string) => data(id));
    const cache = createPreviewCache(fetch, 1000);
    cache.prefetch('s1');
    await vi.advanceTimersByTimeAsync(0);
    expect((await cache.load('s1')).speaker_uuid).toBe('s1');
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('does not start a second prefetch while one is in flight', async () => {
    const fetch = vi.fn(() => new Promise<SpeakerMediaPreviewData>(() => {}));
    const cache = createPreviewCache(fetch, 1000);
    cache.prefetch('s1');
    cache.prefetch('s1');
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('refetches once the entry is older than the ttl', async () => {
    const fetch = vi.fn(async (id: string) => data(id));
    const cache = createPreviewCache(fetch, 1000);
    await cache.load('s1');
    vi.setSystemTime(Date.now() + 1001);
    await cache.load('s1');
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('swallows prefetch failures but rejects load failures', async () => {
    const fetch = vi.fn().mockRejectedValue(new Error('x'));
    const cache = createPreviewCache(fetch, 1000);
    cache.prefetch('s1');
    await vi.advanceTimersByTimeAsync(0);
    await expect(cache.load('s1')).rejects.toThrow('x');
  });
});
