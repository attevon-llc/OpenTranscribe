/**
 * `searchAdminFiles` must ask the route of the caller's own authority: an organization
 * administrator's picker searches `/org-admin/holds/files/search` (their organization's files),
 * an instance administrator's `/admin/holds/files/search` (every tenant). Never the gallery list,
 * which in multi-tenant mode shows an administrator their own and shared files only.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const axios = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: axios, isRequestCancelled: () => false }));

import { FILE_SEARCH_LIMIT, searchAdminFiles } from './fileSearch';

beforeEach(() => {
  vi.clearAllMocks();
  axios.get.mockResolvedValue({ data: { files: [{ uuid: 'a' }], total: 1 } });
});

describe('searchAdminFiles', () => {
  it('searches the organization route for an organization administrator', async () => {
    await expect(searchAdminFiles('organization', 'board')).resolves.toEqual([{ uuid: 'a' }]);
    expect(axios.get).toHaveBeenCalledWith('/org-admin/holds/files/search', {
      params: { q: 'board', limit: FILE_SEARCH_LIMIT },
    });
  });

  it('searches the platform route for an instance administrator', async () => {
    await searchAdminFiles('platform', 'board');
    expect(axios.get).toHaveBeenCalledWith('/admin/holds/files/search', {
      params: { q: 'board', limit: FILE_SEARCH_LIMIT },
    });
    expect(axios.get).not.toHaveBeenCalledWith('/files', expect.anything());
  });

  it('returns nothing, rather than throwing, on a reply without files', async () => {
    axios.get.mockResolvedValue({ data: {} });
    await expect(searchAdminFiles('platform', 'x')).resolves.toEqual([]);
  });
});
