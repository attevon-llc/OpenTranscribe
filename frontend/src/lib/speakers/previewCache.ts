import type { SpeakerMediaPreviewData } from '$lib/api/speakerClusters';

const PREVIEW_CACHE_TTL_MS = 5 * 60 * 1000;

/** Short-lived cache of speaker media previews, filled on hover and read on play. */
export function createPreviewCache(
  fetchPreview: (speakerUuid: string) => Promise<SpeakerMediaPreviewData>,
  ttlMs: number = PREVIEW_CACHE_TTL_MS
) {
  const entries = new Map<string, { data: SpeakerMediaPreviewData; ts: number }>();
  const inFlight = new Set<string>();

  function fresh(speakerUuid: string): SpeakerMediaPreviewData | null {
    const cached = entries.get(speakerUuid);
    return cached && Date.now() - cached.ts < ttlMs ? cached.data : null;
  }

  return {
    /** Warm the cache for a hovered speaker; failures are silent. */
    prefetch(speakerUuid: string): void {
      if (fresh(speakerUuid) || inFlight.has(speakerUuid)) return;
      inFlight.add(speakerUuid);
      fetchPreview(speakerUuid)
        .then((data) => {
          entries.set(speakerUuid, { data, ts: Date.now() });
        })
        .catch(() => {
          /* silent prefetch failure */
        })
        .finally(() => {
          inFlight.delete(speakerUuid);
        });
    },
    /** The cached preview if fresh, else fetch (and cache) it; rejects if the fetch does. */
    async load(speakerUuid: string): Promise<SpeakerMediaPreviewData> {
      const hit = fresh(speakerUuid);
      if (hit) return hit;
      const data = await fetchPreview(speakerUuid);
      entries.set(speakerUuid, { data, ts: Date.now() });
      return data;
    },
  };
}
