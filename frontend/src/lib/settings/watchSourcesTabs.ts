/**
 * Tabs of the Watch Sources panel. Sources are a per-user feature; the email
 * mailers (they hold SMTP/Graph credentials) and the deployment-wide defaults are
 * super_admin endpoints, so those tabs do not exist below that tier — a plain
 * admin would only get two swallowed 403s behind an empty-looking tab.
 */

export type WatchSourcesTabId = 'sources' | 'email' | 'global';

export function watchSourcesTabs(isSuperAdmin: boolean): WatchSourcesTabId[] {
  return isSuperAdmin ? ['sources', 'email', 'global'] : ['sources'];
}

/** A tab id that is no longer offered (role demoted mid-session) falls back to the first. */
export function resolveWatchSourcesTab(
  requested: WatchSourcesTabId,
  tabs: WatchSourcesTabId[]
): WatchSourcesTabId {
  return tabs.includes(requested) ? requested : tabs[0];
}
