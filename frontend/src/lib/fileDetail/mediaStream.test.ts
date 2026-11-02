import { describe, it, expect, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({
  getMediaStreamUrl: vi.fn(),
  getCachedUrlInfo: vi.fn(),
  createUrlRefresher: vi.fn(),
  clearMediaUrlCache: vi.fn(),
}));
vi.mock('$lib/api/mediaUrl', () => mocks);

import { createMediaStream } from './mediaStream';

const stopFns: Array<ReturnType<typeof vi.fn>> = [];

beforeEach(() => {
  vi.clearAllMocks();
  stopFns.length = 0;
  mocks.getMediaStreamUrl.mockResolvedValue('https://s3/x');
  mocks.createUrlRefresher.mockImplementation(() => {
    const stop = vi.fn();
    stopFns.push(stop);
    return { stop };
  });
});

describe('createMediaStream', () => {
  it('returns the URL with the served type and refreshes on its real expiry', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(1_000_000);
    mocks.getCachedUrlInfo.mockReturnValue({
      contentType: 'audio/mp4',
      playback: 'audio_only',
      expiresAt: 1_000_000 + 600_000,
    });
    const onRefresh = vi.fn();

    const target = await createMediaStream(onRefresh).open('f1');

    expect(mocks.clearMediaUrlCache).toHaveBeenCalledWith('f1');
    expect(target).toEqual({
      url: 'https://s3/x',
      contentType: 'audio/mp4',
      playbackMode: 'audio_only',
    });
    expect(mocks.createUrlRefresher).toHaveBeenCalledWith('f1', onRefresh, 600);
    vi.restoreAllMocks();
  });

  it('keeps the prefetched URL on first load, floors the refresh at 60s, defaults to 300s', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(1_000_000);
    mocks.getCachedUrlInfo.mockReturnValueOnce({ expiresAt: 1_000_000 + 5_000 });
    const stream = createMediaStream(vi.fn());

    await stream.open('f1', true);
    expect(mocks.clearMediaUrlCache).not.toHaveBeenCalled();
    expect(mocks.createUrlRefresher.mock.calls[0][2]).toBe(60);

    mocks.getCachedUrlInfo.mockReturnValueOnce(null);
    expect(await stream.open('f1')).toMatchObject({ contentType: '', playbackMode: null });
    expect(mocks.createUrlRefresher.mock.calls[1][2]).toBe(300);
    vi.restoreAllMocks();
  });

  it('stops the previous refresher on re-open and on stop()', async () => {
    mocks.getCachedUrlInfo.mockReturnValue(null);
    const stream = createMediaStream(vi.fn());

    await stream.open('f1');
    await stream.open('f1');
    expect(stopFns[0]).toHaveBeenCalledTimes(1);

    stream.stop();
    stream.stop();
    expect(stopFns[1]).toHaveBeenCalledTimes(1);
  });

  it('rejects when the media is unavailable, with no refresher left running', async () => {
    mocks.getCachedUrlInfo.mockReturnValue(null);
    const stream = createMediaStream(vi.fn());
    await stream.open('f1');
    mocks.getMediaStreamUrl.mockRejectedValueOnce(new Error('404'));

    await expect(stream.open('f1')).rejects.toThrow('404');
    expect(stopFns[0]).toHaveBeenCalledTimes(1);
    expect(mocks.createUrlRefresher).toHaveBeenCalledTimes(1);
  });
});
