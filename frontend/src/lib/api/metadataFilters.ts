import axiosInstance from '$lib/axios';
import { apiCache, cacheKey, CacheTTL } from '$lib/apiCache';

/**
 * `GET /files/metadata-filters` over everything the caller can see (`ownership=all`): the
 * gallery facets' bounds and options. One cache entry, shared by the filter sidebar and the
 * "Legal hold" tag/collection, so both read the same answer and it is fetched once.
 */
export interface MetadataFilters {
  duration?: { min?: number | null; max?: number | null };
  languages?: string[];
  /** Files under a legal hold that the caller can see (legal-hold plan §5). */
  held_count?: number;
  [key: string]: unknown;
}

export function fetchMetadataFilters(): Promise<MetadataFilters> {
  return apiCache.getOrFetch(
    cacheKey.metadataFilters(),
    async () => {
      const response = await axiosInstance.get('/files/metadata-filters', {
        params: { ownership: 'all' },
      });
      return response.data as MetadataFilters;
    },
    CacheTTL.METADATA
  );
}
