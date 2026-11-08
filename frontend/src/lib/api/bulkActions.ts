import axiosInstance from '$lib/axios';

/**
 * `POST /files/management/bulk-action`, for selections of any size.
 *
 * The endpoint processes its list synchronously inside one request, so it caps
 * `file_uuids` at {@link BULK_ACTION_MAX_FILES} (`BULK_ACTION_MAX_FILES` in
 * `backend/app/api/endpoints/files/bulk_actions.py`) and answers 422 above it. A larger
 * selection is sent here as consecutive requests of at most that many files, and the
 * per-file results are concatenated in order, so callers see one result per file exactly
 * as before. The URL stays literal because `backend/tests/unit/test_route_has_a_caller.py`
 * finds a route's caller by its URL string.
 */
export const BULK_ACTION_MAX_FILES = 500;

/** One file's outcome; a refused file is a result, not a failed request. */
export interface BulkFileResult {
  file_uuid: string;
  success: boolean;
  message: string;
  error?: string | null;
}

export interface BulkActionBody {
  file_uuids: string[];
  action: string;
  [field: string]: unknown;
}

export async function postBulkAction<T>(body: BulkActionBody): Promise<T[]> {
  if (body.file_uuids.length <= BULK_ACTION_MAX_FILES) {
    const response = await axiosInstance.post<T[]>('/files/management/bulk-action', body);
    return response.data;
  }
  const results: T[] = [];
  for (let start = 0; start < body.file_uuids.length; start += BULK_ACTION_MAX_FILES) {
    const response = await axiosInstance.post<T[]>('/files/management/bulk-action', {
      ...body,
      file_uuids: body.file_uuids.slice(start, start + BULK_ACTION_MAX_FILES),
    });
    results.push(...(response.data ?? []));
  }
  return results;
}
