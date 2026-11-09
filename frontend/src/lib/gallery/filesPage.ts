/**
 * One page of the gallery's file list, normalised across the shapes the two endpoints return:
 * `GET /files` (paginated `{items,...}`, or a legacy flat array) and
 * `GET /collections/{id}/media` (paginated, or a flat array while it had no pagination).
 */
import axiosInstance from '$lib/axios';
import type { MediaFile } from '$lib/types/media';

export interface PageMeta {
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
  hasMore: boolean;
}

interface RawPage {
  items?: MediaFile[];
  page?: number;
  page_size?: number;
  total?: number;
  total_pages?: number;
  has_more?: boolean;
}

export interface FilesPage {
  items: MediaFile[];
  /** The server's pagination fields, absent for a flat array or an unrecognised body. */
  raw: RawPage | null;
  /** Which endpoint answered: collection pages default their missing fields, `/files` ones do not. */
  source: 'collection' | 'files';
  /** True when the body was neither a page nor an array. */
  empty: boolean;
}

export async function fetchFilesPage(
  collectionId: string | null,
  params: URLSearchParams
): Promise<FilesPage> {
  if (collectionId !== null) {
    const { data } = await axiosInstance.get(`/collections/${collectionId}/media`, { params });
    // The collections endpoint may not paginate yet; handle both formats.
    return {
      items: data.items || data,
      raw: data.items ? data : null,
      source: 'collection',
      empty: false,
    };
  }
  const { data } = await axiosInstance.get('/files', { params });
  if (data && data.items) return { items: data.items, raw: data, source: 'files', empty: false };
  if (Array.isArray(data)) return { items: data, raw: null, source: 'files', empty: false };
  return { items: [], raw: null, source: 'files', empty: true };
}

/**
 * The pagination to record for a loaded page, or null when the body carried none.
 * `fallback` fills the fields a collection page may omit; a flat `/files` array is one whole page
 * (recorded only when `recordFlat`: appending a next page leaves the totals alone).
 */
export function pageMeta(page: FilesPage, fallback: PageMeta, recordFlat = true): PageMeta | null {
  const r = page.raw;
  if (page.source === 'collection') {
    if (!r) return null;
    return {
      page: r.page || fallback.page,
      pageSize: r.page_size || fallback.pageSize,
      total: r.total || fallback.total,
      totalPages: r.total_pages || fallback.totalPages,
      hasMore: r.has_more || false,
    };
  }
  if (r) {
    return {
      page: r.page as number,
      pageSize: r.page_size as number,
      total: r.total as number,
      totalPages: r.total_pages as number,
      hasMore: r.has_more as boolean,
    };
  }
  return page.empty || !recordFlat ? null : { ...fallback, hasMore: false };
}
