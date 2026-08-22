/**
 * In-memory API response cache with TTL and push-based invalidation.
 *
 * This module implements a cache-aside pattern for frontend API calls.
 * Cache entries expire after a configurable TTL, and are also invalidated
 * in real-time via WebSocket push notifications from the backend.
 *
 * The cache also deduplicates concurrent in-flight requests to the same key.
 */

interface CacheEntry<T> {
  data: T;
  createdAt: number;
  expiresAt: number;
}

/** Default TTLs in milliseconds */
export const CacheTTL = {
  TAGS: 5 * 60 * 1000, // 5 minutes
  SPEAKERS: 5 * 60 * 1000, // 5 minutes
  METADATA: 5 * 60 * 1000, // 5 minutes
  FILES: 2 * 60 * 1000, // 2 minutes
  COLLECTIONS: 5 * 60 * 1000, // 5 minutes
  STATUS: 60 * 1000, // 1 minute
  GROUPS: 5 * 60 * 1000, // 5 minutes
  SHARES: 2 * 60 * 1000, // 2 minutes
  SHARED_COLLECTIONS: 3 * 60 * 1000, // 3 minutes
} as const;

class ApiCache {
  private cache = new Map<string, CacheEntry<unknown>>();
  private inflight = new Map<string, Promise<unknown>>();

  /** Metrics for debugging (dev console: __cacheStats()) */
  private hits = 0;
  private misses = 0;

  /**
   * Get a cached value. Returns null on miss or expiry.
   */
  get<T>(key: string): T | null {
    const entry = this.cache.get(key);
    if (!entry) {
      this.misses++;
      return null;
    }

    if (Date.now() > entry.expiresAt) {
      this.cache.delete(key);
      this.misses++;
      return null;
    }

    this.hits++;
    return entry.data as T;
  }

  /**
   * Store a value with TTL.
   */
  set<T>(key: string, data: T, ttlMs: number): void {
    const now = Date.now();
    this.cache.set(key, {
      data,
      createdAt: now,
      expiresAt: now + ttlMs,
    });
  }

  /**
   * Get-or-fetch: returns cached data if available, otherwise calls
   * fetchFn, caches the result, and returns it.
   *
   * Concurrent callers for the same key share a single in-flight request.
   */
  async getOrFetch<T>(key: string, fetchFn: () => Promise<T>, ttlMs: number): Promise<T> {
    // 1. Check cache
    const cached = this.get<T>(key);
    if (cached !== null) {
      return cached;
    }

    // 2. Deduplicate concurrent requests
    const pending = this.inflight.get(key);
    if (pending) {
      return pending as Promise<T>;
    }

    // 3. Fetch and cache
    const promise = fetchFn()
      .then((data) => {
        this.set(key, data, ttlMs);
        return data;
      })
      .finally(() => {
        this.inflight.delete(key);
      });

    this.inflight.set(key, promise);
    return promise;
  }

  /**
   * Invalidate all keys matching a prefix.
   */
  invalidate(prefix: string): number {
    let count = 0;
    for (const key of this.cache.keys()) {
      if (key.startsWith(prefix)) {
        this.cache.delete(key);
        count++;
      }
    }
    return count;
  }

  /**
   * Invalidate based on WebSocket scope (matches backend scopes).
   */
  invalidateByScope(scope: string): void {
    switch (scope) {
      case 'files':
        this.invalidate('files:');
        this.invalidate('status:');
        this.invalidate('prefetch:file:');
        break;
      case 'tags':
        this.invalidate('tags:');
        break;
      case 'speakers':
        this.invalidate('speakers:');
        break;
      case 'metadata':
        this.invalidate('metadata:');
        break;
      case 'collections':
        this.invalidate('collections:');
        break;
      case 'groups':
        this.invalidate('groups:');
        break;
      case 'shares':
        this.invalidate('shares:');
        break;
      case 'shared_collections':
        this.invalidate('shared-collections:');
        break;
      case 'all':
        this.clear();
        break;
    }
  }

  /**
   * Clear the entire cache.
   */
  clear(): void {
    this.cache.clear();
    this.hits = 0;
    this.misses = 0;
  }

  /**
   * Get cache statistics for debugging.
   */
  stats(): { size: number; hits: number; misses: number; hitRate: string } {
    const total = this.hits + this.misses;
    return {
      size: this.cache.size,
      hits: this.hits,
      misses: this.misses,
      hitRate: total > 0 ? `${((this.hits / total) * 100).toFixed(1)}%` : 'N/A',
    };
  }
}

/** Singleton cache instance */
export const apiCache = new ApiCache();

/** Cache key builders */
export const cacheKey = {
  tags: () => 'tags:all',
  /**
   * `q` is the server-side type-to-search term (`GET /speakers?for_filter=true&q=...`).
   * Keying on it keeps a query-scoped result from colliding with the unfiltered
   * default list under one cache entry — but the key still starts with
   * `speakers:`, so `apiCache.invalidateByScope('speakers')` (the WebSocket push
   * path) drops every query variant along with the default one.
   */
  speakers: (q?: string) => (q ? `speakers:filter:q:${q}` : 'speakers:filter'),
  metadataFilters: () => 'metadata:filters',
  files: (page: number, filterHash: string) => `files:page:${page}:${filterHash}`,
  collections: () => 'collections:all',
  status: () => 'status:summary',
  groups: () => 'groups:all',
  groupDetail: (uuid: string) => `groups:detail:${uuid}`,
  shares: (collectionUuid: string) => `shares:collection:${collectionUuid}`,
  sharedCollections: () => 'shared-collections:all',
  fileDetail: (uuid: string) => `prefetch:file:${uuid}`,
};

// Expose stats to dev console
if (typeof window !== 'undefined' && import.meta.env?.DEV) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).__cacheStats = () => {
    const s = apiCache.stats();
    console.log(`Cache: ${s.size} entries, ${s.hits} hits, ${s.misses} misses (${s.hitRate})`);
    return s;
  };
}
