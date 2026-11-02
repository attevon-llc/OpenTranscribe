import { apiCache } from '$lib/apiCache';
import { listClusters, listProfiles, getUnverifiedSpeakers } from '$lib/api/speakerClusters';
import type { SpeakerProfile } from '$lib/types/speakerCluster';
import { CLUSTER_PAGE_SIZE } from './pageHelpers';

/** Take the prefetched value for `key` (consuming it) when `eligible`, else null. */
function takePrefetched<T>(key: string, eligible: boolean): T | null {
  const cached = eligible ? apiCache.get<T>(key) : null;
  if (cached) apiCache.invalidate(key);
  return cached;
}

/** One page of clusters; the default first page may come from the route prefetch. */
export async function fetchClusterPage(page: number, search: string) {
  const cached = takePrefetched<Awaited<ReturnType<typeof listClusters>>>(
    'speakers:clusters',
    page === 1 && !search
  );
  return cached ?? (await listClusters(page, CLUSTER_PAGE_SIZE, search || undefined));
}

export async function fetchProfiles(): Promise<SpeakerProfile[]> {
  const cached = takePrefetched<SpeakerProfile[]>('speakers:profiles', true);
  return cached ?? (await listProfiles());
}

export async function fetchInboxPage(page: number) {
  const cached = takePrefetched<Awaited<ReturnType<typeof getUnverifiedSpeakers>>>(
    'speakers:inbox',
    page === 1
  );
  return cached ?? (await getUnverifiedSpeakers(page, 20));
}
