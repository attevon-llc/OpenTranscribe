/**
 * fuzzyMatcher — a thin, opinionated wrapper around fuse.js.
 *
 * This is NOT a custom matching algorithm: fuse.js does all the work. The wrapper
 * only (a) centralizes one set of sensible defaults so every fuzzy surface in the
 * app behaves consistently, and (b) folds diacritics/case so accented locales
 * (es/fr/pt/de …) match regardless of how the user types the query.
 *
 * Highlighting is intentionally NOT derived from fuse's match ranges — those are
 * computed against the normalized text and would misalign with the original label.
 * Consumers highlight the display string separately (see `$lib/utils/searchHighlight`).
 */
import Fuse, { type IFuseOptions } from 'fuse.js';

/**
 * Case-fold and strip combining diacritical marks so "vídeo" ⇄ "video".
 * Uses Unicode NFD decomposition then removes the combining-marks block.
 */
export function normalizeText(value: string): string {
  return value.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
}

export interface FuzzyKey {
  /** Dotted path into the item, e.g. `"label"` or `"keywords"`. */
  name: string;
  /** Relative weight; higher = more influential. Default 1. */
  weight?: number;
}

export interface FuzzyIndexOptions {
  /** Searchable fields (string paths or weighted key objects). */
  keys: Array<string | FuzzyKey>;
  /** 0 = exact only, 1 = match anything. Default 0.4. */
  threshold?: number;
  /** Minimum query length that can produce a match. Default 2. */
  minMatchCharLength?: number;
  /** Default cap on results per search. */
  limit?: number;
}

export interface FuzzyResult<T> {
  item: T;
  /** fuse score: 0 = perfect, 1 = worst. */
  score: number;
  /** Index of the item in the source collection. */
  refIndex: number;
}

export interface FuzzyIndex<T> {
  search(query: string, limit?: number): FuzzyResult<T>[];
  setCollection(items: T[]): void;
}

function resolvePath(obj: unknown, segments: string[]): unknown {
  let current: unknown = obj;
  for (const segment of segments) {
    if (current == null || typeof current !== 'object') return undefined;
    current = (current as Record<string, unknown>)[segment];
  }
  return current;
}

/**
 * fuse.js value extractor that normalizes every indexed string. Because indexing
 * happens through this, the stored tokens are accent-folded; we normalize the
 * query the same way in `search()` so both sides line up.
 */
function normalizingGetFn(obj: unknown, path: string | string[]): string | string[] {
  const segments = Array.isArray(path) ? path : String(path).split('.');
  const value = resolvePath(obj, segments);
  if (value == null) return '';
  if (Array.isArray(value)) return value.map((entry) => normalizeText(String(entry)));
  return normalizeText(String(value));
}

/**
 * Build a reusable fuzzy index over `items`. The returned object is cheap to hold
 * and can be refreshed in place via `setCollection` (e.g. on locale change).
 */
export function createFuzzyIndex<T>(items: T[], options: FuzzyIndexOptions): FuzzyIndex<T> {
  const defaultLimit = options.limit;
  const fuseOptions: IFuseOptions<T> = {
    keys: options.keys as IFuseOptions<T>['keys'],
    threshold: options.threshold ?? 0.4,
    minMatchCharLength: options.minMatchCharLength ?? 2,
    ignoreLocation: true,
    includeScore: true,
    shouldSort: true,
    // fuse's typing for getFn is stricter than our normalize helper; the runtime
    // contract (return string | string[]) is satisfied.
    getFn: normalizingGetFn as unknown as IFuseOptions<T>['getFn'],
  };

  const fuse = new Fuse(items, fuseOptions);

  return {
    search(query: string, limit?: number): FuzzyResult<T>[] {
      const normalized = normalizeText(query.trim());
      if (!normalized) return [];
      const effectiveLimit = limit ?? defaultLimit;
      const raw = fuse.search(
        normalized,
        effectiveLimit != null ? { limit: effectiveLimit } : undefined
      );
      return raw.map((result) => ({
        item: result.item,
        score: result.score ?? 0,
        refIndex: result.refIndex,
      }));
    },
    setCollection(next: T[]): void {
      fuse.setCollection(next);
    },
  };
}
