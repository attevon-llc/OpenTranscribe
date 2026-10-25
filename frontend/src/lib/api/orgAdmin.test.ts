import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

import axiosInstance from '$lib/axios';
import { OrgAdminApi } from './orgAdmin';

const get = vi.mocked(axiosInstance.get);
const post = vi.mocked(axiosInstance.post);

describe('OrgAdminApi (issue #1253)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('lists from the organization path with the paging params', async () => {
    get.mockResolvedValue({ data: { files: [], total: 0 } } as never);

    await OrgAdminApi.listQuarantinedFiles({ limit: 10, offset: 20, include_legal_holds: true });

    expect(get).toHaveBeenCalledWith('/org-admin/files/quarantined', {
      params: { limit: 10, offset: 20, include_legal_holds: true },
    });
  });

  it('quarantines with a reason and no legal-hold field (an organization cannot place one)', async () => {
    post.mockResolvedValue({ data: {} } as never);

    await OrgAdminApi.quarantineFile('abc', 'policy 4.2');

    expect(post).toHaveBeenCalledWith('/org-admin/files/abc/quarantine', { reason: 'policy 4.2' });
  });

  it('releases with an optional audit-only reason', async () => {
    post.mockResolvedValue({ data: {} } as never);

    await OrgAdminApi.releaseFile('abc');
    await OrgAdminApi.releaseFile('abc', 'withdrawn');

    expect(post).toHaveBeenNthCalledWith(1, '/org-admin/files/abc/release', {});
    expect(post).toHaveBeenNthCalledWith(2, '/org-admin/files/abc/release', {
      reason: 'withdrawn',
    });
  });

  it('lists the organization audit log with the member by uuid and the paging params', async () => {
    get.mockResolvedValue({ data: { logs: [{ id: 1 }], total: 7 } } as never);

    const result = await OrgAdminApi.listAuditLogs({
      user_uuid: 'u-1',
      resource_uuid: 'r-1',
      limit: 50,
      offset: 100,
    });

    expect(get).toHaveBeenCalledWith('/org-admin/audit-logs', {
      params: { user_uuid: 'u-1', resource_uuid: 'r-1', limit: 50, offset: 100 },
    });
    expect(result).toEqual({ logs: [{ id: 1 }], total: 7 });
  });

  it('exports as a blob with the format and the same filters', async () => {
    const blob = new Blob(['x']);
    get.mockResolvedValue({ data: blob } as never);

    const result = await OrgAdminApi.exportAuditLogs('csv', { event_type: 'content.export' });

    expect(get).toHaveBeenCalledWith('/org-admin/audit-logs/export', {
      params: { event_type: 'content.export', format: 'csv' },
      responseType: 'blob',
    });
    expect(result).toBe(blob);
  });

  it('lists holds with the active-only filter and unwraps the page', async () => {
    get.mockResolvedValue({ data: { holds: [{ uuid: 'h-1' }], total: 3 } } as never);

    const result = await OrgAdminApi.listHolds({ active_only: false, limit: 200 });

    expect(get).toHaveBeenCalledWith('/org-admin/holds', {
      params: { active_only: false, limit: 200 },
    });
    expect(result).toEqual({ holds: [{ uuid: 'h-1' }], total: 3 });
  });

  it('places a hold with the body as given and releases with only a reason', async () => {
    post.mockResolvedValue({ data: { uuid: 'h-1' } } as never);

    await OrgAdminApi.placeHold({
      scope: 'collection',
      collection_uuid: 'c-1',
      reason: 'Matter 14 hold',
    });
    await OrgAdminApi.releaseHold('h-1', 'Matter closed');

    expect(post).toHaveBeenNthCalledWith(1, '/org-admin/holds', {
      scope: 'collection',
      collection_uuid: 'c-1',
      reason: 'Matter 14 hold',
    });
    expect(post).toHaveBeenNthCalledWith(2, '/org-admin/holds/h-1/release', {
      reason: 'Matter closed',
    });
  });
});
