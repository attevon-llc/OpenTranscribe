/**
 * Deciding what the search page does when it mounts with a query: reuse the results the store
 * already holds (back navigation) or search again, and rebuilding the sidebar's date range
 * from the store's ISO dates.
 */

export type RestoreAction =
  /** The store holds the results of exactly this search: show them and restore the scroll. */
  | 'reuse-cached'
  /** The store holds results for this query under different params (e.g. back navigation). */
  | 'keep-results'
  | 'search'
  | 'none';

export function searchRestoreAction(
  restoredQuery: string,
  cacheKey: string,
  store: { lastSearchParams: string | null; resultsLength: number; query: string }
): RestoreAction {
  if (!restoredQuery) return 'none';
  if (store.lastSearchParams === cacheKey && store.resultsLength > 0) return 'reuse-cached';
  if (store.resultsLength > 0 && store.query === restoredQuery) return 'keep-results';
  return 'search';
}

const dayStart = (isoDate: string | null): Date | null =>
  isoDate ? new Date(isoDate + 'T00:00:00') : null;

/** The sidebar's date range from the store's `YYYY-MM-DD` bounds, or null when neither is set. */
export function dateRangeFromStore(
  dateFrom: string | null,
  dateTo: string | null
): { from: Date | null; to: Date | null } | null {
  if (!dateFrom && !dateTo) return null;
  return { from: dayStart(dateFrom), to: dayStart(dateTo) };
}
