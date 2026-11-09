import { describe, it, expect, vi, beforeEach } from 'vitest';

const h = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: { get: h.get } }));

import { fetchFilesPage, pageMeta } from './filesPage';

const params = new URLSearchParams('page=1');
const file = (uuid: string) => ({ uuid }) as never;
beforeEach(() => vi.clearAllMocks());

describe('fetchFilesPage', () => {
  it('reads /files paginated bodies', async () => {
    h.get.mockResolvedValue({
      data: {
        items: [file('a')],
        page: 1,
        page_size: 50,
        total: 9,
        total_pages: 1,
        has_more: false,
      },
    });
    const page = await fetchFilesPage(null, params);
    expect(h.get).toHaveBeenCalledWith('/files', { params });
    expect(page).toMatchObject({ source: 'files', empty: false, items: [{ uuid: 'a' }] });
  });

  it('reads a collection page from the collection route', async () => {
    h.get.mockResolvedValue({ data: [file('a')] });
    const page = await fetchFilesPage('c1', params);
    expect(h.get).toHaveBeenCalledWith('/collections/c1/media', { params });
    expect(page).toMatchObject({ source: 'collection', raw: null, items: [{ uuid: 'a' }] });
  });

  it('flags an unrecognised body as empty', async () => {
    h.get.mockResolvedValue({ data: { weird: true } });
    expect(await fetchFilesPage(null, params)).toMatchObject({ items: [], empty: true });
  });
});

describe('pageMeta', () => {
  const fallback = { page: 1, pageSize: 2, total: 2, totalPages: 1, hasMore: false };

  it('passes a /files page through untouched', () => {
    const page = {
      items: [],
      source: 'files',
      empty: false,
      raw: { page: 3, page_size: 10, total: 99, total_pages: 10, has_more: true },
    } as never;
    expect(pageMeta(page, fallback)).toEqual({
      page: 3,
      pageSize: 10,
      total: 99,
      totalPages: 10,
      hasMore: true,
    });
  });

  it('defaults the fields a collection page omits, and records nothing for a flat collection list', () => {
    const paged = { items: [], source: 'collection', empty: false, raw: { items: [] } } as never;
    expect(pageMeta(paged, fallback)).toEqual({ ...fallback, hasMore: false });
    const flat = { items: [], source: 'collection', empty: false, raw: null } as never;
    expect(pageMeta(flat, fallback)).toBeNull();
  });

  it('treats a flat /files array as one whole page, unless appending', () => {
    const flat = { items: [], source: 'files', empty: false, raw: null } as never;
    expect(pageMeta(flat, fallback)).toEqual(fallback);
    expect(pageMeta(flat, fallback, false)).toBeNull();
  });

  it('records nothing for an empty body', () => {
    expect(pageMeta({ items: [], source: 'files', empty: true, raw: null }, fallback)).toBeNull();
  });
});
