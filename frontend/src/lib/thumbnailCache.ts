/**
 * Client-side thumbnail cache using blob URLs.
 *
 * Presigned MinIO URLs change on every API call (different signature/expiry),
 * so the browser can't cache them. This module fetches thumbnails once and
 * stores them as blob URLs keyed by file UUID. Blob URLs persist in memory
 * across component mounts, making gallery navigation instant.
 *
 * Memory: ~20KB per thumbnail × 2500 files = ~50MB max (acceptable).
 */

const cache = new Map<string, string>();
const inflight = new Map<string, Promise<string>>();

/**
 * Svelte action for thumbnail img elements.
 * Loads from cache or fetches and caches the thumbnail.
 */
export function cachedThumbnail(node: HTMLImageElement, params: { uuid: string; url: string }) {
  let { uuid, url } = params;

  function loadThumbnail() {
    const cached = cache.get(uuid);
    if (cached) {
      node.src = cached;
      return;
    }

    // Show presigned URL immediately
    node.src = url;

    // Fetch and cache in background
    if (url && !inflight.has(uuid)) {
      const promise = fetchAndCache(uuid, url);
      inflight.set(uuid, promise);
      promise
        .then((blobUrl) => {
          // Update img src to blob URL (no network request)
          if (node.isConnected) {
            node.src = blobUrl;
          }
        })
        .catch(() => {
          // Keep presigned URL as fallback
        })
        .finally(() => inflight.delete(uuid));
    }
  }

  loadThumbnail();

  return {
    update(newParams: { uuid: string; url: string }) {
      uuid = newParams.uuid;
      url = newParams.url;
      loadThumbnail();
    },
    destroy() {
      // Blob URLs are kept in cache for reuse, not revoked here
    },
  };
}

async function fetchAndCache(uuid: string, url: string): Promise<string> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Thumbnail fetch failed: ${response.status}`);
  const blob = await response.blob();
  const blobUrl = URL.createObjectURL(blob);
  cache.set(uuid, blobUrl);
  return blobUrl;
}

/**
 * Clear all cached thumbnails (e.g., on logout).
 */
export function clearThumbnailCache() {
  for (const blobUrl of cache.values()) {
    URL.revokeObjectURL(blobUrl);
  }
  cache.clear();
}
