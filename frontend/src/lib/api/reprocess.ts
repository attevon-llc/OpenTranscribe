/**
 * API client for re-running pipeline stages on already-processed files.
 *
 * The body is the server's `ReprocessRequest` (`stages`, speaker range, `whisper_model`; build it
 * with `buildReprocessBody` in `$lib/fileDetail/reprocessStages`). Both routes refuse a re-run
 * that cannot run (media missing, no local speaker detection) with a 409 carrying the reason.
 */
import axiosInstance from '$lib/axios';
import { postBulkAction } from '$lib/api/bulkActions';

export interface BulkReprocessResult {
  success: boolean;
}

/** Re-run stages on one file. */
export async function reprocessFile(
  fileUuid: string,
  body: Record<string, unknown>
): Promise<void> {
  await axiosInstance.post(`/files/${fileUuid}/reprocess`, body);
}

/** Re-run stages on several files; the reply has one result per file. */
export async function reprocessFiles(
  fileUuids: string[],
  body: Record<string, unknown>
): Promise<BulkReprocessResult[]> {
  return postBulkAction<BulkReprocessResult>({
    ...body,
    file_uuids: fileUuids,
    action: 'reprocess',
  });
}
