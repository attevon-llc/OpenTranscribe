import { describe, it, expect, vi, beforeEach } from 'vitest';

const get = vi.hoisted(() => vi.fn());
vi.mock('$lib/axios', () => ({ default: { get } }));

import { getServerVersion } from './version';

beforeEach(() => {
  get.mockReset();
});

describe('getServerVersion', () => {
  it('reads /version through the shared client (baseURL supplies /api)', async () => {
    get.mockResolvedValue({ data: { version: 'v0.5.1', git_sha: 'abc1234', build_time: 'x' } });

    await expect(getServerVersion()).resolves.toEqual({ version: 'v0.5.1', git_sha: 'abc1234' });
    expect(get).toHaveBeenCalledWith('/version');
  });

  it('normalises missing fields to empty strings', async () => {
    get.mockResolvedValue({ data: { version: 'v0.5.1' } });
    await expect(getServerVersion()).resolves.toEqual({ version: 'v0.5.1', git_sha: '' });

    get.mockResolvedValue({ data: null });
    await expect(getServerVersion()).resolves.toEqual({ version: '', git_sha: '' });
  });

  it('propagates a failed request so the caller decides how to degrade', async () => {
    get.mockRejectedValue(new Error('network'));
    await expect(getServerVersion()).rejects.toThrow('network');
  });
});
