import {
  getMediaStreamUrl,
  getCachedUrlInfo,
  createUrlRefresher,
  clearMediaUrlCache,
} from '$lib/api/mediaUrl';

/**
 * Fetch a fresh stream URL for a result's clip and keep it fresh while it plays.
 *
 * The refresher re-signs the URL before it expires (using the URL's real expiry, not a
 * hardcoded interval) so long playback does not 401. Rejects if the URL cannot be fetched.
 */
export async function openPreviewMedia(
  fileUuid: string,
  onUrlRefreshed: (url: string) => void
): Promise<{ url: string; refresher: { stop: () => void } }> {
  clearMediaUrlCache(fileUuid);
  const url = await getMediaStreamUrl(fileUuid, 'video');
  const info = getCachedUrlInfo(fileUuid, 'video');
  const expiresIn = info ? Math.max(60, Math.floor((info.expiresAt - Date.now()) / 1000)) : 300;
  return { url, refresher: createUrlRefresher(fileUuid, onUrlRefreshed, expiresIn) };
}

/** The speaker of the occurrence containing `time`, else the clip's own speaker. */
export function speakerAtTime(
  occurrences: Array<{ start_time: number; end_time: number; speaker?: string | null }>,
  time: number,
  fallback: string
): string {
  for (const occ of occurrences) {
    if (time >= occ.start_time && time <= occ.end_time) return occ.speaker || '';
  }
  return fallback;
}
