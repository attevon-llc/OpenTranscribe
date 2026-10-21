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
});
