/**
 * Time-range match classification for the consolidated transcript view (issue #755).
 *
 * This is deliberately a SEPARATE module from `$lib/utils/searchHighlight.ts`. That file's
 * `computeMatches`/`highlightTextWithMatches` answer "where does this literal query string
 * occur in the loaded text?" (file-detail's live find bar). This module answers a different
 * question: "which timed transcript segments does a server-ranked search hit's occurrence
 * overlap?" — the algorithm the search-result "view transcript" surface needs, because it
 * receives ranked occurrences (including semantic hits with no literal substring to find) as
 * a prop, not a query to search itself. See `frontend/src/lib/search/CLAUDE.md` — the two are
 * not duplicates of each other, and this file keeps them that way.
 */

import { escapeHtml } from '$lib/utils/sanitizeHtml';

export interface TimeRange {
  start: number;
  end: number;
}

export interface Occurrence {
  start_time: number;
  end_time: number;
  has_keyword_match: boolean;
}

export type MatchClassificationType = 'keyword' | 'semantic';

export interface ClassifiedCounts {
  keyword: number;
  semantic: number;
}

/** Split occurrences into keyword vs. semantic time ranges. */
export function buildTimeRanges(occurrences: Occurrence[]): {
  keywordRanges: TimeRange[];
  semanticRanges: TimeRange[];
} {
  const keywordRanges: TimeRange[] = [];
  const semanticRanges: TimeRange[] = [];
  for (const occ of occurrences || []) {
    const range = { start: occ.start_time, end: occ.end_time };
    if (occ.has_keyword_match) {
      keywordRanges.push(range);
    } else {
      semanticRanges.push(range);
    }
  }
  return { keywordRanges, semanticRanges };
}

/** Whether [start, end) overlaps any of the given ranges. */
export function overlapsAny(start: number, end: number, ranges: TimeRange[]): boolean {
  return ranges.some((r) => start < r.end && end > r.start);
}

/**
 * Classify every segment in `segments` against the occurrence ranges. Keyword wins over
 * semantic when a segment overlaps both, matching the pre-consolidation behaviour.
 */
export function classifySegments<
  T extends { uuid?: string | number | null; start_time: number; end_time: number },
>(segments: T[], occurrences: Occurrence[]): Record<string, MatchClassificationType> {
  const { keywordRanges, semanticRanges } = buildTimeRanges(occurrences);
  const result: Record<string, MatchClassificationType> = {};
  for (const segment of segments || []) {
    if (segment.uuid == null) continue;
    const uuid = String(segment.uuid);
    const start = Number(segment.start_time) || 0;
    const end = Number(segment.end_time) || 0;
    if (overlapsAny(start, end, keywordRanges)) {
      result[uuid] = 'keyword';
    } else if (overlapsAny(start, end, semanticRanges)) {
      result[uuid] = 'semantic';
    }
  }
  return result;
}

/** Count occurrences by type — used for the honest, cap-aware legend counts (issue #755 §4.3). */
export function countByType(occurrences: Occurrence[]): ClassifiedCounts {
  let keyword = 0;
  let semantic = 0;
  for (const occ of occurrences || []) {
    if (occ.has_keyword_match) keyword++;
    else semantic++;
  }
  return { keyword, semantic };
}

/**
 * Highlight a classified segment's text.
 *
 * - `semantic`: the whole segment is the relevant unit — wrap it entirely.
 * - `keyword`: highlight the literal query words (case-insensitive, whole-word). No stemming
 *   (issue #755 J4 — a hand-rolled English suffix stripper does not belong in a 12-locale
 *   product; #453 shipped multilingual retrieval).
 * - otherwise: escaped, unhighlighted text.
 */
export function highlightClassifiedText(
  text: string,
  type: MatchClassificationType | undefined,
  query: string
): string {
  if (!text) return '';
  if (type === 'semantic') {
    return `<span class="search-semantic-segment">${escapeHtml(text)}</span>`;
  }
  if (type === 'keyword') {
    return highlightKeywordWords(text, query);
  }
  return escapeHtml(text);
}

function highlightKeywordWords(text: string, query: string): string {
  const words = (query || '')
    .toLowerCase()
    .split(/\s+/)
    .filter((w) => w.length >= 2)
    .map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));

  if (words.length === 0) {
    return `<span class="search-keyword-match">${escapeHtml(text)}</span>`;
  }

  const pattern = new RegExp(`(${words.join('|')})`, 'gi');
  const escaped = escapeHtml(text);
  // `escapeHtml` never introduces the query's own characters as markup, so matching against
  // the escaped string with the same (unescaped) query words is safe here.
  return escaped.replace(pattern, (match) => `<span class="search-keyword-match">${match}</span>`);
}
