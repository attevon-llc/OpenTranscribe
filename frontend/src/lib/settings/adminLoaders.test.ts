import { describe, it, expect, vi, beforeEach } from 'vitest';

const h = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  approvals: vi.fn(),
  requests: vi.fn(),
}));
vi.mock('$lib/axios', () => ({ default: { get: h.get, post: h.post } }));
vi.mock('$lib/api/userApprovals', () => ({ UserApprovalsApi: { list: h.approvals } }));
vi.mock('$lib/api/supportAccess', () => ({ SupportAccessApi: { listRequests: h.requests } }));

import {
  countPendingApprovals,
  countSupportRequests,
  fetchAdminUsers,
  fetchSystemStats,
  initialSystemStats,
  recoverUserFilesRequest,
} from './adminLoaders';

beforeEach(() => vi.clearAllMocks());

describe('countPendingApprovals', () => {
  it('counts the queue, and counts 0 when the endpoint refuses (no phantom badge)', async () => {
    h.approvals.mockResolvedValue([{}, {}, {}]);
    expect(await countPendingApprovals()).toBe(3);
    h.approvals.mockRejectedValue(new Error('403'));
    expect(await countPendingApprovals()).toBe(0);
  });
});

describe('countSupportRequests', () => {
  it('is 0 without asking outside multi-tenant mode', async () => {
    expect(await countSupportRequests(false, true)).toBe(0);
    expect(h.requests).not.toHaveBeenCalled();
  });

  it('adds the organization queue only for an org admin', async () => {
    h.requests.mockResolvedValue({ total: 2 });
    expect(await countSupportRequests(true, false)).toBe(2);
    expect(h.requests).toHaveBeenCalledTimes(1);
    expect(h.requests).toHaveBeenCalledWith('workspace', {
      status: 'pending',
      limit: 1,
      offset: 0,
    });
    h.requests.mockClear();
    expect(await countSupportRequests(true, true)).toBe(4);
    expect(h.requests.mock.calls.map((c) => c[0])).toEqual(['workspace', 'org']);
  });

  it('is 0 when either request fails', async () => {
    h.requests.mockRejectedValue(new Error('x'));
    expect(await countSupportRequests(true, true)).toBe(0);
  });
});

describe('fetchSystemStats', () => {
  it('returns the stats and treats a failed index probe as absent, not as an error', async () => {
    h.get.mockImplementation((url: string) => {
      if (url === '/system/stats') return Promise.resolve({ data: { users: { total: 5 } } });
      if (url === '/search/reindex/status') return Promise.reject(new Error('404'));
      return Promise.resolve({ data: { os: { status: 'green', doc_count: 1 } } });
    });
    expect(await fetchSystemStats()).toEqual({
      stats: { users: { total: 5 } },
      indexStatus: null,
      indexHealth: { os: { status: 'green', doc_count: 1 } },
    });
  });

  it('propagates a failure of the main stats request', async () => {
    h.get.mockImplementation((url: string) =>
      url === '/system/stats' ? Promise.reject(new Error('500')) : Promise.resolve({ data: {} })
    );
    await expect(fetchSystemStats()).rejects.toThrow('500');
  });
});

describe('simple loaders', () => {
  it('fetches the admin users list and posts the recovery', async () => {
    h.get.mockResolvedValue({ data: [{ id: 1 }] });
    expect(await fetchAdminUsers()).toEqual([{ id: 1 }]);
    h.post.mockResolvedValue({});
    await recoverUserFilesRequest('u-1');
    expect(h.post).toHaveBeenCalledWith('/tasks/system/recover-user-files/u-1');
  });
});

describe('initialSystemStats', () => {
  it('localises the placeholders but keeps the backend N/A sentinel untranslated', () => {
    const stats = initialSystemStats((k) => `T:${k}`);
    expect(stats.system.uptime).toBe('T:common.unknown');
    expect(stats.models.whisper.name).toBe('T:common.notAvailable');
    expect(stats.system.gpus[0]).toMatchObject({ available: false, utilization_percent: 'N/A' });
  });
});
