/**
 * `searchFiles` must be the gallery's own list request (same endpoint, same server-side
 * scoping), not a second search: these pin the parameters that decide what it can return.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const axios = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: axios, isRequestCancelled: () => false }));

import { FILE_SEARCH_LIMIT, searchFiles } from './fileSearch';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('searchFiles', () => {
  it('asks GET /files for every file the caller can see, hidden ones included, by title or name', async () => {
    axios.get.mockResolvedValue({ data: { items: [{ uuid: 'a' }], total: 1 } });

    const found = await searchFiles('board');

    expect(found).toEqual([{ uuid: 'a' }]);
    expect(axios.get).toHaveBeenCalledWith('/files', {
      params: expect.objectContaining({
        search: 'board',
        ownership: 'all',
        hidden: 'include',
        page: 1,
        page_size: FILE_SEARCH_LIMIT,
      }),
    });
  });

  it('returns nothing, rather than throwing, on a reply without items', async () => {
    axios.get.mockResolvedValue({ data: {} });
    await expect(searchFiles('x')).resolves.toEqual([]);
  });
});
