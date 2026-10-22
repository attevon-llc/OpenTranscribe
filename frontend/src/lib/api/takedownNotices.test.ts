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
