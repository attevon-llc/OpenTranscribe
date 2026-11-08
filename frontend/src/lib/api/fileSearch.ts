/**
 * Find files by title or file name for an administrator's pickers (legal hold, quarantine).
 *
 * Deliberately the gallery's own list endpoint, `GET /files`, not a second search: the server
 * applies the same tenant gate, availability rule and admin reach as the gallery, so a picker
 * can only offer files its caller can already see there. `search` matches title and file name;
 * hidden files are included because hiding is a personal view preference, not a reason a file
 * cannot be held or taken down.
 */
import axiosInstance from '$lib/axios';
import type { MediaFile } from '$lib/types/media';

/** Results per search: a short list to choose from, not a page to browse. */
export const FILE_SEARCH_LIMIT = 10;

export async function searchFiles(query: string, limit = FILE_SEARCH_LIMIT): Promise<MediaFile[]> {
  const { data } = await axiosInstance.get('/files', {
    params: {
      search: query,
      ownership: 'all',
      hidden: 'include',
      page: 1,
      page_size: limit,
      sort_by: 'upload_time',
      sort_order: 'desc',
    },
  });
  return Array.isArray(data?.items) ? (data.items as MediaFile[]) : [];
}
