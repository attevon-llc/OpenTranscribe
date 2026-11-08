import axiosInstance from '$lib/axios';
import { postBulkAction } from '$lib/api/bulkActions';

/**
 * Per-user hide of media files (issue #1102).
 *
 * Hiding is the caller's own view preference: it takes a file out of their gallery, lists,
 * search and "All transcripts" chat, and changes nothing for anyone else. The file still opens
 * by direct link. `GET /files?hidden=` takes the same three modes.
 */
export type HiddenMode = 'exclude' | 'include' | 'only';

/** The bulk-action error code for a hide refused at the per-user cap. */
export const HIDDEN_LIMIT_ERROR = 'HIDDEN_LIMIT';

export interface HiddenBulkResult {
  file_uuid: string;
  success: boolean;
  message: string;
  error?: string | null;
}

export class HiddenMediaApi {
  /** Hide (`hidden = true`) or unhide one file. 404 for a file the caller cannot read. */
  static async setHidden(uuid: string, hidden: boolean): Promise<void> {
    if (hidden) {
      await axiosInstance.put(`/files/${uuid}/hidden`);
    } else {
      await axiosInstance.delete(`/files/${uuid}/hidden`);
    }
  }

  /** Hide or unhide many files; one result per file (a refusal is that file's result). */
  static async setHiddenMany(uuids: string[], hidden: boolean): Promise<HiddenBulkResult[]> {
    return postBulkAction<HiddenBulkResult>({
      file_uuids: uuids,
      action: hidden ? 'hide' : 'unhide',
    });
  }
}
