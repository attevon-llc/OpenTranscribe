/**
 * Shared fixtures for the search component tests: an en.json-backed `$t` (so assertions
 * read real copy and a missing key shows up as the raw key) and a `SearchHit` builder.
 */
import type { SearchHit, SearchOccurrence } from '$stores/search';

export async function enLocaleMock() {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    t: readable((key: string, vars?: Record<string, unknown>) => {
      const template = en[key] ?? key;
      if (!vars) return template;
      return Object.entries(vars).reduce(
        (acc, [k, v]) => acc.replace(new RegExp(`{{${k}}}`, 'g'), String(v)),
        template
      );
    }),
  };
}

export function occurrence(overrides: Partial<SearchOccurrence> = {}): SearchOccurrence {
  return {
    snippet: 'we should <mark>ship</mark> it',
    speaker: 'Alice',
    start_time: 12,
    end_time: 18,
    chunk_index: 0,
    score: 1,
    match_type: 'content',
    has_keyword_match: true,
    highlight_type: 'keyword',
    ...overrides,
  };
}

export function searchHit(overrides: Partial<SearchHit> = {}): SearchHit {
  return {
    file_uuid: 'file-1',
    file_id: 1,
    title: 'Roadmap review',
    speakers: ['Alice', 'Bob'],
    tags: ['planning'],
    collections: [{ uuid: 'coll-1', name: 'Q4 board' }],
    upload_time: '2026-10-01T12:00:00Z',
    language: 'en',
    content_type: 'video/mp4',
    relevance_score: 0.9,
    occurrences: [
      occurrence(),
      occurrence({ chunk_index: 1, start_time: 75, snippet: 'second <mark>ship</mark> line' }),
      occurrence({ chunk_index: 2, start_time: 140, snippet: 'third <mark>ship</mark> line' }),
    ],
    total_occurrences: 3,
    title_highlighted: '',
    keyword_occurrences: 3,
    semantic_only: false,
    semantic_confidence: '',
    match_sources: ['content', 'semantic'],
    relevance_percent: 87,
    duration: 3725,
    file_size: 52_428_800,
    semantic_occurrences: 0,
    has_both_match_types: false,
    ...overrides,
  };
}
