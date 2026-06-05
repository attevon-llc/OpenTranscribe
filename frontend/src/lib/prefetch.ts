/**
 * Prefetch service for hover-based and post-login data preloading.
 *
 * Provides debounced hover prefetch for gallery/search cards,
 * post-login dashboard data prefetch, and search next-page prefetch.
 */

import axiosInstance from '$lib/axios';
import { apiCache, CacheTTL, cacheKey } from '$lib/apiCache';
import { getMediaStreamUrl } from '$lib/api/mediaUrl';

/** Track in-flight prefetch UUIDs to avoid duplicate requests */
const inflight = new Set<string>();

/** Track recently failed UUIDs to avoid retry spam (cached for 10s) */
const failedCache = new Map<string, number>();
const FAILED_CACHE_TTL = 10_000;

let pendingTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * Prefetch file details and stream URL on hover.
 * Debounced at 200ms to handle virtual scroll thrashing.
 *
 * Pass the file's `status` when known: stream URLs only exist for completed
 * files, so prefetching one for an error/processing file is a guaranteed 404
 * (browser logs it as a console error on every gallery hover).
 */
export function prefetchFileDetails(fileUuid: string, status?: string): void {
  cancelPrefetch();

  pendingTimer = setTimeout(() => {
    pendingTimer = null;

    // Skip if already in-flight or recently failed
    if (inflight.has(fileUuid)) return;
    const failedAt = failedCache.get(fileUuid);
    if (failedAt && Date.now() - failedAt < FAILED_CACHE_TTL) return;

    // Skip if already cached
    const cached = apiCache.get(cacheKey.fileDetail(fileUuid));
    if (cached) return;

    inflight.add(fileUuid);

    const requests: Promise<unknown>[] = [
      apiCache.getOrFetch(
        cacheKey.fileDetail(fileUuid),
        () => axiosInstance.get(`/files/${fileUuid}`).then((r) => r.data),
        CacheTTL.FILES
      ),
    ];
    if (status === undefined || status === 'completed') {
      requests.push(getMediaStreamUrl(fileUuid, 'video').catch(() => null));
    }

    // Fire requests in parallel, silently
    Promise.all(requests)
      .catch(() => {
        failedCache.set(fileUuid, Date.now());
      })
      .finally(() => {
        inflight.delete(fileUuid);
      });
  }, 200);
}

/**
 * Cancel any pending prefetch (call on mouseleave).
 */
export function cancelPrefetch(): void {
  if (pendingTimer) {
    clearTimeout(pendingTimer);
    pendingTimer = null;
  }
}

/**
 * Prefetch dashboard data (file list + user settings) after login.
 * Fires in parallel, fails silently.
 */
export function prefetchDashboardData(): void {
  // Prefetch the first page of files
  const filesKey = cacheKey.files(1, '');
  apiCache
    .getOrFetch(filesKey, () => axiosInstance.get('/files').then((r) => r.data), CacheTTL.FILES)
    .catch(() => {});

  // Prefetch collections
  apiCache
    .getOrFetch(
      cacheKey.collections(),
      () => axiosInstance.get('/collections').then((r) => r.data),
      CacheTTL.COLLECTIONS
    )
    .catch(() => {});
}

/**
 * Prefetch speakers page data on hover.
 * Fires all three tab endpoints in parallel, fails silently.
 */
export function prefetchSpeakersData(): void {
  apiCache
    .getOrFetch(
      'speakers:clusters',
      () =>
        axiosInstance
          .get('/speaker-clusters', { params: { page: 1, per_page: 20 } })
          .then((r) => r.data),
      CacheTTL.SPEAKERS
    )
    .catch(() => {});

  apiCache
    .getOrFetch(
      'speakers:profiles',
      () => axiosInstance.get('/speaker-profiles/profiles').then((r) => r.data),
      CacheTTL.SPEAKERS
    )
    .catch(() => {});

  apiCache
    .getOrFetch(
      'speakers:inbox',
      () =>
        axiosInstance
          .get('/speaker-clusters/unverified/inbox', { params: { page: 1, per_page: 20 } })
          .then((r) => r.data),
      CacheTTL.SPEAKERS
    )
    .catch(() => {});
}

/**
 * Prefetch file status data on hover of the File Status nav link.
 * Fires status summary + first page of tasks in parallel, fails silently.
 */
export function prefetchFileStatusData(): void {
  apiCache
    .getOrFetch(
      cacheKey.status(),
      () => axiosInstance.get('/my-files/status').then((r) => r.data),
      CacheTTL.STATUS
    )
    .catch(() => {});

  apiCache
    .getOrFetch(
      'tasks:page1',
      () => axiosInstance.get('/tasks?page=1&page_size=25').then((r) => r.data),
      CacheTTL.STATUS
    )
    .catch(() => {});
}

/**
 * Prefetch the next page of search results after current page loads.
 * Delayed by 1 second to avoid competing with current page rendering.
 */
export function prefetchNextSearchPage(
  currentQuery: string,
  currentPage: number,
  totalPages: number,
  searchParams: Record<string, unknown>
): void {
  if (currentPage >= totalPages) return;

  setTimeout(() => {
    const nextPage = currentPage + 1;
    const prefetchKey = `search:${currentQuery}:page:${nextPage}`;

    // Skip if already cached
    if (apiCache.get(prefetchKey)) return;

    apiCache
      .getOrFetch(
        prefetchKey,
        () =>
          axiosInstance
            .get('/search', {
              params: { ...searchParams, page: nextPage },
              paramsSerializer: (params: Record<string, unknown>) => {
                const sp = new URLSearchParams();
                Object.entries(params).forEach(([key, value]) => {
                  if (value === undefined) return;
                  if (Array.isArray(value)) {
                    value.forEach((v) => sp.append(key, String(v)));
                  } else {
                    sp.set(key, String(value));
                  }
                });
                return sp.toString();
              },
            })
            .then((r) => r.data),
        CacheTTL.FILES
      )
      .catch(() => {});
  }, 1000);
}
