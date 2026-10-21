/**
 * Stale-while-revalidate cache for the read-mostly GETs behind settings sections.
 *
 * A section that was opened a minute ago should not flash a skeleton the second time: it shows
 * what it last saw at once and refreshes behind it. The skeleton is for a cold miss only. This
 * is deliberately a separate, tiny store rather than `apiCache`: `apiCache` drops an entry the
 * moment its TTL passes (no stale read), and its callers want hard freshness.
 *
 * Rules that keep it honest:
 * - Entries are in-memory only and cleared by `purgeTenantDataCaches()` on logout, login and
 *   support-session edges, so another user never sees them.
 * - `maxAgeMs` bounds how stale a shown value may be; past it the section starts cold.
 * - `invalidateSectionData` (after a save) makes the next read a cold miss, and a request that
 *   was already in flight when it was invalidated is not stored.
 * - Only cheap, read-mostly endpoints are registered with `prefetchSectionData`; nothing
 *   expensive (system statistics, index health) is ever fetched without the user asking.
 *
 * Imported lazily (settings chunks and `clearUserState`), never by the root layout. `$lib/axios` is a
 * static import on purpose: a dynamic one makes the bundler emit a namespace object into the root
 * layout's static chunk, which is budgeted.
 */
import axiosInstance from '$lib/axios';
import type { SettingsSection } from '$stores/settingsModalStore';

/** Past this a remembered value is treated as absent rather than shown stale. */
const DEFAULT_MAX_AGE_MS = 10 * 60 * 1000;

interface Entry {
  data: unknown;
  at: number;
}

const entries = new Map<string, Entry>();
const inflight = new Map<string, Promise<unknown>>();
/** Bumped by invalidate/clear so a response that raced them is dropped, not stored. */
let epoch = 0;

/** The remembered value for `key`, or undefined on a cold miss. */
export function peekSectionData<T>(key: string, maxAgeMs = DEFAULT_MAX_AGE_MS): T | undefined {
  const entry = entries.get(key);
  if (!entry) return undefined;
  if (Date.now() - entry.at > maxAgeMs) {
    entries.delete(key);
    return undefined;
  }
  return entry.data as T;
}

/** Remember a value the caller already has (e.g. the body of a successful PUT). */
export function setSectionData<T>(key: string, data: T): void {
  entries.set(key, { data, at: Date.now() });
}

/**
 * Fetch and remember. Concurrent callers for one key share a request, so a prefetch that is
 * still in flight when the section mounts is joined rather than repeated.
 */
export function loadSectionData<T>(key: string, fetcher: () => Promise<T>): Promise<T> {
  const pending = inflight.get(key);
  if (pending) return pending as Promise<T>;
  const startedAt = epoch;
  const promise = fetcher()
    .then((data) => {
      if (startedAt === epoch) setSectionData(key, data);
      return data;
    })
    .finally(() => {
      if (inflight.get(key) === promise) inflight.delete(key);
    });
  inflight.set(key, promise);
  return promise;
}

/** Forget one key (after a save changed what it holds). */
export function invalidateSectionData(key: string): void {
  entries.delete(key);
  inflight.delete(key);
  epoch += 1;
}

/** Forget everything (logout, login, support-session edge). */
export function clearSectionData(): void {
  entries.clear();
  inflight.clear();
  epoch += 1;
}

/** Cheap structural equality for JSON payloads: lets a refresh skip a no-op re-render. */
export function sameSectionData(a: unknown, b: unknown): boolean {
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    return false;
  }
}

/** The condensed File Status summary; the full page reads the same endpoint. */
export async function fetchFileStatusSummary<T = unknown>(): Promise<T> {
  return (await axiosInstance.get('/my-files/status')).data;
}

export async function fetchRetentionConfig<T = unknown>(): Promise<T> {
  return (await axiosInstance.get('/admin/settings/retention-config')).data;
}

export const SectionDataKey = {
  pipeline: 'settings:pipeline',
  fileStatus: 'settings:file-status',
  retention: 'settings:retention',
  authConfig: 'settings:auth-config',
} as const;

type Tier = 'user' | 'admin' | 'super_admin';

/**
 * The warm-up fetchers, with the tier that may call each. These are the only requests ever made
 * on a section's behalf before the user opens it; each is a single small GET.
 */
const PREFETCHERS: Partial<
  Record<SettingsSection, { tier: Tier; key: string; fetch: () => Promise<unknown> }>
> = {
  'processing-pipeline': {
    tier: 'user',
    key: SectionDataKey.pipeline,
    fetch: () => import('$lib/api/pipeline').then((m) => m.getPipelineView()),
  },
  'file-status': {
    tier: 'user',
    key: SectionDataKey.fileStatus,
    fetch: fetchFileStatusSummary,
  },
  'data-management': {
    tier: 'admin',
    key: SectionDataKey.retention,
    fetch: fetchRetentionConfig,
  },
  authentication: {
    tier: 'super_admin',
    key: SectionDataKey.authConfig,
    fetch: () => import('$lib/api/authConfig').then((m) => m.AuthConfigApi.getAllConfigs()),
  },
};

const RANK: Record<Tier, number> = { user: 0, admin: 1, super_admin: 2 };

/** Does the section have a registered warm-up the caller's tier may run? */
export function hasSectionPrefetch(section: SettingsSection, role: Tier): boolean {
  const spec = PREFETCHERS[section];
  return !!spec && RANK[role] >= RANK[spec.tier];
}

/**
 * Warm the data behind `section`, if it has a registered cheap fetcher and `role` may call it.
 * A fresh entry is left alone; failures are swallowed (opening the section repeats the request
 * and shows the real error there).
 */
export function prefetchSectionData(section: SettingsSection, role: Tier): void {
  const spec = PREFETCHERS[section];
  if (!spec || RANK[role] < RANK[spec.tier]) return;
  if (peekSectionData(spec.key, 15_000) !== undefined) return;
  void loadSectionData(spec.key, spec.fetch).catch(() => {});
}
