/**
 * The file-detail player's media URL: a short-lived presigned URL (MinIO-signed, 5 minutes
 * by default) plus a refresher that re-issues it before it expires during long playback.
 */
import {
  clearMediaUrlCache,
  createUrlRefresher,
  getCachedUrlInfo,
  getMediaStreamUrl,
  type PlaybackMode,
} from '$lib/api/mediaUrl';

export interface MediaStreamTarget {
  url: string;
  /**
   * What the URL serves, which can differ from the upload: a converted copy of audio no
   * browser decodes, or only the audio track of a video no browser shows. The player picks
   * `<audio>` vs `<video>` from it, so the page sets it before the URL.
   */
  contentType: string;
  playbackMode: PlaybackMode | null;
}

/**
 * @param onRefresh Receives each refreshed URL (the page assigns it to the player).
 */
export function createMediaStream(onRefresh: (url: string) => void) {
  let urlRefresher: { stop: () => void } | null = null;

  function stop(): void {
    if (urlRefresher) {
      urlRefresher.stop();
      urlRefresher = null;
    }
  }

  /**
   * Resolve the stream URL for `fileId` and (re)start its refresher. Rejects when the media
   * is unavailable; the previous refresher is stopped either way.
   *
   * @param keepCached First load only. The only cached entry can then be the gallery's hover
   *   prefetch (seconds old, and `getMediaStreamUrl` already refuses one near expiry);
   *   clearing it made a second identical request.
   */
  async function open(fileId: string, keepCached = false): Promise<MediaStreamTarget> {
    stop();
    if (!keepCached) clearMediaUrlCache(fileId);

    const url = await getMediaStreamUrl(fileId, 'video');
    const info = getCachedUrlInfo(fileId, 'video');

    // Refresh on the URL's real expiry (MEDIA_URL_EXPIRE_SECONDS) rather than a hardcoded
    // interval — avoids needlessly re-fetching and re-setting the video src mid-playback.
    const expiresIn = info ? Math.max(60, Math.floor((info.expiresAt - Date.now()) / 1000)) : 300;
    urlRefresher = createUrlRefresher(fileId, onRefresh, expiresIn);

    return { url, contentType: info?.contentType ?? '', playbackMode: info?.playback ?? null };
  }

  return { open, stop };
}
