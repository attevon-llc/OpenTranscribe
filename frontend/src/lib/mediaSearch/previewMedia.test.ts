import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const media = vi.hoisted(() => ({
  getMediaStreamUrl: vi.fn(),
  getCachedUrlInfo: vi.fn(),
  createUrlRefresher: vi.fn(),
  clearMediaUrlCache: vi.fn(),
}));
vi.mock('$lib/api/mediaUrl', () => media);

import { openPreviewMedia, speakerAtTime } from './previewMedia';

describe('openPreviewMedia', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    media.getMediaStreamUrl.mockResolvedValue('http://u');
    media.createUrlRefresher.mockReturnValue({ stop: vi.fn() });
  });
  afterEach(() => vi.useRealTimers());

  it('clears the cached URL before fetching, then refreshes on the URL real expiry', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    media.getCachedUrlInfo.mockReturnValue({ expiresAt: 1_000_000 + 600_000 });
    const onRefresh = vi.fn();
    const r = await openPreviewMedia('f1', onRefresh);
    expect(media.clearMediaUrlCache.mock.invocationCallOrder[0]).toBeLessThan(
      media.getMediaStreamUrl.mock.invocationCallOrder[0]
    );
    expect(r.url).toBe('http://u');
    expect(media.createUrlRefresher).toHaveBeenCalledWith('f1', onRefresh, 600);
  });

  it('falls back to 300s with no URL info and never refreshes faster than 60s', async () => {
    media.getCachedUrlInfo.mockReturnValue(null);
    await openPreviewMedia('f1', vi.fn());
    expect(media.createUrlRefresher.mock.calls[0][2]).toBe(300);
    media.getCachedUrlInfo.mockReturnValue({ expiresAt: Date.now() + 1000 });
    await openPreviewMedia('f1', vi.fn());
    expect(media.createUrlRefresher.mock.calls[1][2]).toBe(60);
  });

  it('rejects when the stream URL cannot be fetched', async () => {
    media.getMediaStreamUrl.mockRejectedValue(new Error('x'));
    await expect(openPreviewMedia('f1', vi.fn())).rejects.toThrow('x');
  });
});

describe('speakerAtTime', () => {
  const occ = [
    { start_time: 0, end_time: 5, speaker: 'A' },
    { start_time: 5, end_time: 10, speaker: null },
  ];
  it('finds the containing occurrence (inclusive), else the fallback', () => {
    expect(speakerAtTime(occ, 3, 'F')).toBe('A');
    expect(speakerAtTime(occ, 7, 'F')).toBe('');
    expect(speakerAtTime(occ, 11, 'F')).toBe('F');
  });
});
