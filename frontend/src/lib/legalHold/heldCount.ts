/**
 * How many files under a legal hold the caller can see (`held_count` from
 * `/files/metadata-filters`, legal-hold plan §5). The gallery's computed "Legal hold" tag chip
 * and collection entry render only while it is above zero: there is nothing to edit or delete,
 * and the entry disappears by itself once the last hold is released.
 */
import { writable } from 'svelte/store';
import { apiCache } from '$lib/apiCache';
import { fetchMetadataFilters } from '$lib/api/metadataFilters';

export const heldCount = writable(0);

/**
 * Re-read the count. `fresh` drops the cached answer first (after this page placed a hold);
 * otherwise the shared cache entry the filter sidebar also reads is reused. A failed read keeps
 * the last known count: the chip is a shortcut to a filter, not the hold record.
 */
export async function refreshHeldCount(fresh = false): Promise<void> {
  if (fresh) apiCache.invalidate('metadata:');
  try {
    const data = await fetchMetadataFilters();
    heldCount.set(Math.max(0, Number(data.held_count) || 0));
  } catch (error) {
    console.warn('Could not read the legal-hold count:', error);
  }
}

/** Session change (`clearUserState`): the next user's count is read fresh. */
export function resetHeldCount(): void {
  heldCount.set(0);
}
