/**
 * `mediaUrl.ts` owns an in-memory presigned-URL cache with expiry-buffer logic,
 * batch fetching for gallery thumbnails, and a self-rescheduling refresher for
 * long video playback. All three have real correctness risk: a stale cache hit
 * serves an expired (403ing) URL, a batch fetch that doesn't tolerate individual
 * failures takes down the whole gallery grid over one bad file, and a refresher
 * that doesn't reschedule off the NEW expiry silently stops refreshing.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockInstance }));

import {
  getMediaStreamUrl,
  getCachedUrlInfo,
  clearMediaUrlCache,
  getMediaStreamUrlsBatch,
  createUrlRefresher,
} from './mediaUrl';

beforeEach(() => {
  vi.clearAllMocks();
  clearMediaUrlCache();
});

describe('getMediaStreamUrl', () => {
  it('fetches once, then serves the cache on a second call within the expiry buffer', async () => {
    mockInstance.get.mockResolvedValue({
      data: { url: 'https://s3/video.mp4', expires_in: 3600, content_type: 'video/mp4' },
    });

    const first = await getMediaStreamUrl('file-1', 'video');
    const second = await getMediaStreamUrl('file-1', 'video');

    expect(first).toBe('https://s3/video.mp4');
    expect(second).toBe('https://s3/video.mp4');
    expect(mockInstance.get).toHaveBeenCalledTimes(1);
  });

  it('refetches once the cached URL is within the 30s expiry safety buffer', async () => {
    vi.useFakeTimers();
    try {
      mockInstance.get.mockResolvedValue({
        data: { url: 'https://s3/a', expires_in: 40, content_type: 'video/mp4' },
      });
      await getMediaStreamUrl('file-1', 'video');

      vi.advanceTimersByTime(15000); // 25s left — inside the 30s buffer already

      mockInstance.get.mockResolvedValue({
        data: { url: 'https://s3/b', expires_in: 3600, content_type: 'video/mp4' },
      });
      const url = await getMediaStreamUrl('file-1', 'video');

      expect(url).toBe('https://s3/b');
      expect(mockInstance.get).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('caches video/thumbnail/audio for the same file independently', async () => {
    mockInstance.get.mockImplementation((url: string, opts: { params: { media_type: string } }) => {
      return Promise.resolve({
        data: { url: `https://s3/${opts.params.media_type}`, expires_in: 3600, content_type: 'x' },
      });
    });

    const video = await getMediaStreamUrl('file-1', 'video');
    const thumb = await getMediaStreamUrl('file-1', 'thumbnail');

    expect(video).toBe('https://s3/video');
    expect(thumb).toBe('https://s3/thumbnail');
    expect(mockInstance.get).toHaveBeenCalledTimes(2);
  });
});

describe('getCachedUrlInfo / clearMediaUrlCache', () => {
  it('returns null before anything is cached, and the cached entry after', async () => {
    expect(getCachedUrlInfo('file-1', 'video')).toBeNull();

    mockInstance.get.mockResolvedValue({
      data: { url: 'https://s3/a', expires_in: 3600, content_type: 'video/mp4' },
    });
    await getMediaStreamUrl('file-1', 'video');

    expect(getCachedUrlInfo('file-1', 'video')?.url).toBe('https://s3/a');
  });

  it('clears only the named file, leaving other files cached', async () => {
    mockInstance.get.mockResolvedValue({
      data: { url: 'https://s3/a', expires_in: 3600, content_type: 'video/mp4' },
    });
    await getMediaStreamUrl('file-1', 'video');
    await getMediaStreamUrl('file-2', 'video');

    clearMediaUrlCache('file-1');

    expect(getCachedUrlInfo('file-1', 'video')).toBeNull();
    expect(getCachedUrlInfo('file-2', 'video')).not.toBeNull();
  });

  it('clears every file when called with no argument', async () => {
    mockInstance.get.mockResolvedValue({
      data: { url: 'https://s3/a', expires_in: 3600, content_type: 'video/mp4' },
    });
    await getMediaStreamUrl('file-1', 'video');

    clearMediaUrlCache();

    expect(getCachedUrlInfo('file-1', 'video')).toBeNull();
  });
});

describe('getMediaStreamUrlsBatch', () => {
  it('serves cached entries and fetches only the uncached ones', async () => {
    mockInstance.get.mockResolvedValue({
      data: { url: 'https://s3/cached', expires_in: 3600, content_type: 'image/jpeg' },
    });
    await getMediaStreamUrl('file-1', 'thumbnail'); // pre-warm the cache
    mockInstance.get.mockClear();

    mockInstance.get.mockResolvedValue({
      data: { url: 'https://s3/fresh', expires_in: 3600, content_type: 'image/jpeg' },
    });
    const results = await getMediaStreamUrlsBatch(['file-1', 'file-2'], 'thumbnail');

    expect(mockInstance.get).toHaveBeenCalledTimes(1); // only file-2
    expect(results.get('file-1')).toBe('https://s3/cached');
    expect(results.get('file-2')).toBe('https://s3/fresh');
  });

  it('tolerates one file failing without failing the whole batch', async () => {
    mockInstance.get.mockImplementation((url: string) => {
      if (url.includes('bad-file')) return Promise.reject(new Error('403'));
      return Promise.resolve({
        data: { url: 'https://s3/good', expires_in: 3600, content_type: 'image/jpeg' },
      });
    });

    const results = await getMediaStreamUrlsBatch(['good-file', 'bad-file'], 'thumbnail');

    expect(results.get('good-file')).toBe('https://s3/good');
    expect(results.has('bad-file')).toBe(false);
  });
});

describe('createUrlRefresher', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('refreshes 30s before expiry and reschedules off the NEW expiry, not the old one', async () => {
    mockInstance.get.mockResolvedValue({
      data: { url: 'https://s3/refreshed-once', expires_in: 3600, content_type: 'video/mp4' },
    });
    const onRefresh = vi.fn();

    const refresher = createUrlRefresher('file-1', onRefresh, 40); // fires in 10s

    await vi.advanceTimersByTimeAsync(10000);
    expect(onRefresh).toHaveBeenCalledWith('https://s3/refreshed-once');

    // A second refresh should be scheduled off the fresh 3600s expiry, not the
    // original 40s — advancing by only 10s again must not fire a second refresh yet.
    mockInstance.get.mockClear();
    await vi.advanceTimersByTimeAsync(10000);
    expect(mockInstance.get).not.toHaveBeenCalled();

    refresher.stop();
  });

  it('does not schedule anything when the initial expiry is already inside the buffer', async () => {
    const onRefresh = vi.fn();
    createUrlRefresher('file-1', onRefresh, 20); // 20 - 30 <= 0

    await vi.advanceTimersByTimeAsync(60000);

    expect(onRefresh).not.toHaveBeenCalled();
    expect(mockInstance.get).not.toHaveBeenCalled();
  });

  it('stop() prevents the scheduled refresh from firing', async () => {
    const onRefresh = vi.fn();
    const refresher = createUrlRefresher('file-1', onRefresh, 40);

    refresher.stop();
    await vi.advanceTimersByTimeAsync(60000);

    expect(onRefresh).not.toHaveBeenCalled();
  });

  it('logs rather than throws when the refresh fetch fails, and does not reschedule', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    mockInstance.get.mockRejectedValue(new Error('network down'));
    const onRefresh = vi.fn();

    createUrlRefresher('file-1', onRefresh, 40);
    await vi.advanceTimersByTimeAsync(10000);

    expect(onRefresh).not.toHaveBeenCalled();
    expect(consoleError).toHaveBeenCalledWith('Failed to refresh video URL:', expect.any(Error));
    consoleError.mockRestore();
  });
});
