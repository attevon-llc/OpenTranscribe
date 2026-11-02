/**
 * Saving one transcript segment's text from the file-detail page's inline editor, and
 * invalidating what is derived from the transcript once a write commits.
 */
import axiosInstance from '$lib/axios';

/**
 * `PUT /files/{fileId}/transcript/segments/{segmentUuid}`. Resolves the server's row (with
 * the text it stored), or `null` for an empty response; rejects on any failure.
 */
export async function saveSegmentText(
  fileId: string,
  segmentUuid: string,
  text: string
): Promise<{ text?: string } | null> {
  const response = await axiosInstance.put(`/files/${fileId}/transcript/segments/${segmentUuid}`, {
    text,
  });
  return response.data || null;
}

/** The i18n key telling the reader why a segment save failed. */
export function segmentSaveErrorKey(status: number | undefined): string {
  if (status === 405) return 'fileDetail.transcriptEditingNotSupported';
  if (status === 404) return 'fileDetail.transcriptSegmentNotFound';
  if (status === 422) return 'fileDetail.invalidSegmentData';
  return 'fileDetail.failedToSaveSegment';
}

/**
 * Drop the server's cached processed videos (burned-in subtitles) so the next download
 * uses the updated transcript. Fire-and-forget: a failure only costs a stale cache.
 */
export function clearProcessedVideoCache(fileUuid: unknown): void {
  axiosInstance.delete(`/files/${fileUuid}/cache`).catch((error: unknown) => {
    console.warn('Could not clear video cache:', error);
  });
}
