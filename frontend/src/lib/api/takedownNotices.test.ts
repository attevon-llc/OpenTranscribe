import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), patch: vi.fn() };
  return { default: axiosInstance };
});

import axiosInstance from '$lib/axios';
import { TakedownNoticesApi } from './takedownNotices';

const get = vi.mocked(axiosInstance.get);
const post = vi.mocked(axiosInstance.post);
const patch = vi.mocked(axiosInstance.patch);

describe('TakedownNoticesApi', () => {
  beforeEach(() => vi.clearAllMocks());

  it('records an outcome with only the status; the file state is never in the request', async () => {
    patch.mockResolvedValue({ data: {} } as never);
    await TakedownNoticesApi.recordOutcome('n-1', 'f-1', 'upheld');
    expect(patch).toHaveBeenCalledWith('/admin/takedown-notices/n-1/files/f-1', {
      status: 'upheld',
    });
  });

  it('notify sends the event and nothing that could name a recipient', async () => {
    post.mockResolvedValue({ data: { sent: 1 } } as never);
    await TakedownNoticesApi.notify('n-1');
    await TakedownNoticesApi.notify('n-1', 'released');
    expect(post.mock.calls).toEqual([
      ['/admin/takedown-notices/n-1/notify', { event: 'quarantined' }],
      ['/admin/takedown-notices/n-1/notify', { event: 'released' }],
    ]);
  });

  it('the owner route takes paging only, never a user selector', async () => {
    get.mockResolvedValue({ data: { takedowns: [], total: 0 } } as never);
    await TakedownNoticesApi.listMine({ limit: 1 });
    expect(get).toHaveBeenCalledWith('/takedown-notices/mine', {
      params: { limit: 1, offset: undefined },
    });
  });

  it('hasAnyMine asks once for repeated and concurrent calls, and retries after a failure', async () => {
    TakedownNoticesApi.resetMineCache();
    get.mockResolvedValue({ data: { takedowns: [], total: 2 } } as never);
    const [a, b] = await Promise.all([
      TakedownNoticesApi.hasAnyMine(),
      TakedownNoticesApi.hasAnyMine(),
    ]);
    expect([a, b]).toEqual([true, true]);
    await TakedownNoticesApi.hasAnyMine();
    expect(get).toHaveBeenCalledTimes(1);

    TakedownNoticesApi.resetMineCache();
    get.mockRejectedValueOnce(new Error('429'));
    await expect(TakedownNoticesApi.hasAnyMine()).rejects.toThrow();
    await Promise.resolve();
    get.mockResolvedValue({ data: { takedowns: [], total: 0 } } as never);
    expect(await TakedownNoticesApi.hasAnyMine()).toBe(false);
  });

  it('hasAnyMine survives a page reload per user, is purged with the cache, and a 429 stays quiet', async () => {
    sessionStorage.clear();
    TakedownNoticesApi.resetMineCache();
    get.mockResolvedValue({ data: { takedowns: [], total: 1 } } as never);
    expect(await TakedownNoticesApi.hasAnyMine('u-1')).toBe(true);
    expect(get).toHaveBeenCalledTimes(1);

    // A full page load drops module memory but keeps sessionStorage.
    vi.resetModules();
    const fresh = (await import('./takedownNotices')).TakedownNoticesApi;
    get.mockClear();
    expect(await fresh.hasAnyMine('u-1')).toBe(true);
    expect(get).not.toHaveBeenCalled();

    // Another user never reads it.
    vi.resetModules();
    const other = (await import('./takedownNotices')).TakedownNoticesApi;
    get.mockResolvedValue({ data: { takedowns: [], total: 0 } } as never);
    expect(await other.hasAnyMine('u-2')).toBe(false);
    expect(get).toHaveBeenCalledTimes(1);

    // The logout purge removes it.
    other.resetMineCache();
    expect(sessionStorage.getItem('opentr:takedownMine')).toBeNull();

    // A 429 rejects without persisting anything.
    get.mockRejectedValueOnce(Object.assign(new Error('429'), { response: { status: 429 } }));
    await expect(other.hasAnyMine('u-2')).rejects.toThrow();
    expect(sessionStorage.getItem('opentr:takedownMine')).toBeNull();
  });

  it('export reads the server-built filename and the truncation flag', async () => {
    get.mockResolvedValue({
      data: new Blob(['x']),
      headers: {
        'content-disposition': 'attachment; filename="takedown-notices-20261021.csv"',
        'x-export-truncated': 'true',
      },
    } as never);
    const out = await TakedownNoticesApi.export('csv');
    expect(get).toHaveBeenCalledWith('/admin/takedown-notices/export', {
      params: { format: 'csv' },
      responseType: 'blob',
    });
    expect(out.filename).toBe('takedown-notices-20261021.csv');
    expect(out.truncated).toBe(true);
  });

  it('export falls back to a fixed name and not-truncated when headers are absent', async () => {
    get.mockResolvedValue({ data: new Blob(['x']), headers: {} } as never);
    const out = await TakedownNoticesApi.export('json');
    expect(out.filename).toBe('takedown-notices.json');
    expect(out.truncated).toBe(false);
  });
});
