/**
 * Media URL Service - Secure presigned URL management
 *
 * This module handles fetching and caching presigned URLs for media streaming.
 * Presigned URLs are the industry standard (AWS S3, Google Cloud Storage, Azure)
 * for secure content delivery with time-limited access.
 *
 * Features:
 * - Automatic URL caching with expiration tracking
 * - URL refresh before expiration for long playback
 * - Batch fetching for gallery thumbnails (reduces API calls)
 */

import axiosInstance from '$lib/axios';

export interface StreamUrlResponse {
  url: string;
  expires_in: number;
  content_type: string;
  is_public: boolean;
}

export type MediaType = 'video' | 'thumbnail' | 'audio';

interface CachedUrl {
  url: string;
  expiresAt: number;
  contentType: string;
}

// In-memory cache for presigned URLs with expiration tracking
const urlCache = new Map<string, CachedUrl>();

// Safety buffer: refresh URLs 30 seconds before expiration
const EXPIRY_BUFFER_MS = 30000;

/**
 * Generate a cache key for a file and media type
 */
function getCacheKey(fileId: string, mediaType: MediaType): string {
  return `${fileId}-${mediaType}`;
}

/**
 * Get a presigned URL for secure media streaming.
 *
 * This is the primary method for accessing media files. It:
 * 1. Returns cached URL if still valid
 * 2. Fetches a new presigned URL from the backend if cache expired
 * 3. Caches the new URL for subsequent requests
 *
 * @param fileId - UUID of the media file
 * @param mediaType - Type of media: 'video', 'thumbnail', or 'audio'
 * @returns Presigned URL string
 * @throws Error if fetching fails
 */
export async function getMediaStreamUrl(
  fileId: string,
  mediaType: MediaType = 'video'
): Promise<string> {
  const cacheKey = getCacheKey(fileId, mediaType);
  const cached = urlCache.get(cacheKey);
  const now = Date.now();

  // Return cached URL if not expired (with safety buffer)
  if (cached && cached.expiresAt > now + EXPIRY_BUFFER_MS) {
    return cached.url;
  }

  // Fetch new presigned URL from backend
  const response = await axiosInstance.get<StreamUrlResponse>(`/files/${fileId}/stream-url`, {
    params: { media_type: mediaType },
  });

  const { url, expires_in, content_type } = response.data;

  // Cache with expiration timestamp
  urlCache.set(cacheKey, {
    url,
    expiresAt: now + expires_in * 1000,
    contentType: content_type,
  });

  return url;
}

/**
 * Get cached URL info including content type.
 * Returns null if not cached or expired.
 */
export function getCachedUrlInfo(fileId: string, mediaType: MediaType): CachedUrl | null {
  const cacheKey = getCacheKey(fileId, mediaType);
  const cached = urlCache.get(cacheKey);
  const now = Date.now();

  if (cached && cached.expiresAt > now + EXPIRY_BUFFER_MS) {
    return cached;
  }

  return null;
}

/**
 * Clear cached URLs for a specific file or all files.
 *
 * Use this when:
 * - User logs out (clear all)
 * - File is deleted (clear specific file)
 * - File access changes (clear specific file)
 *
 * @param fileId - Optional file UUID. If not provided, clears all cached URLs.
 */
export function clearMediaUrlCache(fileId?: string): void {
  if (fileId) {
    urlCache.delete(`${fileId}-video`);
    urlCache.delete(`${fileId}-thumbnail`);
    urlCache.delete(`${fileId}-audio`);
  } else {
    urlCache.clear();
  }
}

/**
 * Batch fetch presigned URLs for multiple files.
 *
 * Optimized for gallery views where many thumbnails need to be loaded.
 * - Checks cache first to avoid redundant API calls
 * - Fetches uncached URLs in parallel
 * - Handles individual failures gracefully (doesn't fail entire batch)
 *
 * @param fileIds - Array of file UUIDs
 * @param mediaType - Type of media (default: 'thumbnail' for gallery use)
 * @returns Map of fileId -> presigned URL
 */
export async function getMediaStreamUrlsBatch(
  fileIds: string[],
  mediaType: MediaType = 'thumbnail'
): Promise<Map<string, string>> {
  const results = new Map<string, string>();
  const uncached: string[] = [];
  const now = Date.now();

  // Check cache first
  for (const fileId of fileIds) {
    const cacheKey = getCacheKey(fileId, mediaType);
    const cached = urlCache.get(cacheKey);
    if (cached && cached.expiresAt > now + EXPIRY_BUFFER_MS) {
      results.set(fileId, cached.url);
    } else {
      uncached.push(fileId);
    }
  }

  // Fetch uncached URLs in parallel
  if (uncached.length > 0) {
    const fetchPromises = uncached.map(async (fileId) => {
      try {
        const url = await getMediaStreamUrl(fileId, mediaType);
        return { fileId, url, success: true };
      } catch (error) {
        console.warn(`Failed to get ${mediaType} URL for ${fileId}:`, error);
        return { fileId, url: '', success: false };
      }
    });

    const fetchResults = await Promise.all(fetchPromises);

    for (const result of fetchResults) {
      if (result.success && result.url) {
        results.set(result.fileId, result.url);
      }
    }
  }

  return results;
}

/**
 * Create a URL refresher for long video playback.
 *
 * For videos longer than the URL expiration time, this creates a function
 * that automatically refreshes the URL before it expires.
 *
 * @param fileId - UUID of the media file
 * @param onRefresh - Callback when URL is refreshed (receives new URL)
 * @param expiresIn - Initial expiration time in seconds
 * @returns Object with stop() method to cancel auto-refresh
 */
export function createUrlRefresher(
  fileId: string,
  onRefresh: (newUrl: string) => void,
  expiresIn: number
): { stop: () => void } {
  let timeoutId: ReturnType<typeof setTimeout> | null = null;

  const scheduleRefresh = (expiresInSeconds: number) => {
    // Refresh 30 seconds before expiration
    const refreshInMs = (expiresInSeconds - 30) * 1000;

    if (refreshInMs > 0) {
      timeoutId = setTimeout(async () => {
        try {
          // Clear cache to force refresh
          clearMediaUrlCache(fileId);
          const newUrl = await getMediaStreamUrl(fileId, 'video');
          onRefresh(newUrl);

          // Get the new expiration from cache
          const cached = getCachedUrlInfo(fileId, 'video');
          if (cached) {
            const newExpiresIn = Math.floor((cached.expiresAt - Date.now()) / 1000);
            scheduleRefresh(newExpiresIn);
          }
        } catch (error) {
          console.error('Failed to refresh video URL:', error);
        }
      }, refreshInMs);
    }
  };

  // Start the refresh cycle
  scheduleRefresh(expiresIn);

  return {
    stop: () => {
      if (timeoutId) {
        clearTimeout(timeoutId);
        timeoutId = null;
      }
    },
  };
}
