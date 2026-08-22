/**
 * `searchStore` holds the search page's filters, results, and pagination — 20+ actions
 * over a 20-field state object, no API calls, pure client state. These tests pin the
 * store's own convention: every filter-mutating setter resets `page` to 1 (BC-28: `setQuery`
 * was the sole exception until fixed alongside this file), `setPage` clamps below 1, and
 * `setFilters`/`setResults` merge rather than replace unrelated fields.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import {
  searchStore,
  searchResults,
  isSearchLoading,
  searchQuery,
  type SearchState,
  type SearchResponse,
} from './search';

beforeEach(() => {
  searchStore.reset();
});

describe('page-resetting filter setters', () => {
  const cases: Array<{
    name: string;
    apply: () => void;
    expected: Partial<SearchState>;
  }> = [
    {
      name: 'setQuery',
      apply: () => searchStore.setQuery('hello'),
      expected: { query: 'hello' },
    },
    {
      name: 'setSortBy',
      apply: () => searchStore.setSortBy('date'),
      expected: { sortBy: 'date' },
    },
    {
      name: 'setSortOrder',
      apply: () => searchStore.setSortOrder('asc'),
      expected: { sortOrder: 'asc' },
    },
    {
      name: 'setSort',
      apply: () => searchStore.setSort('date', 'asc'),
      expected: { sortBy: 'date', sortOrder: 'asc' },
    },
    {
      name: 'setSearchMode',
      apply: () => searchStore.setSearchMode('semantic'),
      expected: { searchMode: 'semantic' },
    },
    {
      name: 'setResultType',
      apply: () => searchStore.setResultType('summaries'),
      expected: { resultType: 'summaries' },
    },
    {
      name: 'setSpeakers',
      apply: () => searchStore.setSpeakers(['alice']),
      expected: { selectedSpeakers: ['alice'] },
    },
    {
      name: 'setTags',
      apply: () => searchStore.setTags(['meeting']),
      expected: { selectedTags: ['meeting'] },
    },
    {
      name: 'setDateRange',
      apply: () => searchStore.setDateRange('2026-01-01', '2026-02-01'),
      expected: { dateFrom: '2026-01-01', dateTo: '2026-02-01' },
    },
    {
      name: 'setFileTypes',
      apply: () => searchStore.setFileTypes(['mp4']),
      expected: { selectedFileTypes: ['mp4'] },
    },
    {
      name: 'setCollectionId',
      apply: () => searchStore.setCollectionId('col-1'),
      expected: { selectedCollectionId: 'col-1' },
    },
    {
      name: 'setDurationRange',
      apply: () => searchStore.setDurationRange({ min: 10, max: 100 }),
      expected: { durationRange: { min: 10, max: 100 } },
    },
    {
      name: 'setFileSizeRange',
      apply: () => searchStore.setFileSizeRange({ min: 1, max: 2 }),
      expected: { fileSizeRange: { min: 1, max: 2 } },
    },
    {
      name: 'setStatuses',
      apply: () => searchStore.setStatuses(['completed']),
      expected: { selectedStatuses: ['completed'] },
    },
    {
      name: 'setTitleFilter',
      apply: () => searchStore.setTitleFilter('foo'),
      expected: { titleFilter: 'foo' },
    },
    {
      name: 'setFilters',
      apply: () => searchStore.setFilters({ query: 'bulk', selectedTags: ['x'] }),
      expected: { query: 'bulk', selectedTags: ['x'] },
    },
  ];

  it.each(cases)('$name resets page to 1 and applies its own field(s)', ({ apply, expected }) => {
    searchStore.setPage(5);
    expect(get(searchStore).page).toBe(5);

    apply();

    const state = get(searchStore);
    expect(state.page).toBe(1);
    expect(state).toMatchObject(expected);
  });
});

describe('non-page-resetting setters', () => {
  const cases: Array<{ name: string; apply: () => void; expected: Partial<SearchState> }> = [
    {
      name: 'setLoading',
      apply: () => searchStore.setLoading(true),
      expected: { isLoading: true },
    },
    {
      name: 'setError',
      apply: () => searchStore.setError('boom'),
      expected: { error: 'boom' },
    },
    {
      name: 'setLastSearchParams',
      apply: () => searchStore.setLastSearchParams('q=hello'),
      expected: { lastSearchParams: 'q=hello' },
    },
    {
      name: 'setScrollPosition',
      apply: () => searchStore.setScrollPosition(250),
      expected: { scrollPosition: 250 },
    },
  ];

  it.each(cases)('$name leaves page untouched', ({ apply, expected }) => {
    searchStore.setPage(5);

    apply();

    const state = get(searchStore);
    expect(state.page).toBe(5);
    expect(state).toMatchObject(expected);
  });
});

describe('setPage', () => {
  it('sets the page when given a positive value', () => {
    searchStore.setPage(5);
    expect(get(searchStore).page).toBe(5);
  });

  it('clamps a zero page to 1', () => {
    searchStore.setPage(0);
    expect(get(searchStore).page).toBe(1);
  });

  it('clamps a negative page to 1', () => {
    searchStore.setPage(-3);
    expect(get(searchStore).page).toBe(1);
  });
});

describe('setFilters', () => {
  it('merges the given fields, leaving unrelated fields untouched', () => {
    searchStore.setSpeakers(['alice']);
    searchStore.setTitleFilter('kickoff');

    searchStore.setFilters({ query: 'bulk-update' });

    const state = get(searchStore);
    expect(state.query).toBe('bulk-update');
    expect(state.selectedSpeakers).toEqual(['alice']);
    expect(state.titleFilter).toBe('kickoff');
  });
});

describe('setResults', () => {
  const response: SearchResponse = {
    query: 'hello',
    results: [
      {
        file_uuid: 'uuid-1',
        file_id: 1,
        title: 'Meeting',
        speakers: ['Alice'],
        tags: ['work'],
        upload_time: '2026-01-01T00:00:00Z',
        language: 'en',
        content_type: 'audio/mp4',
        relevance_score: 0.9,
        occurrences: [],
        total_occurrences: 0,
        title_highlighted: 'Meeting',
        keyword_occurrences: 0,
        semantic_only: false,
        semantic_confidence: 'high',
        match_sources: ['content'],
        relevance_percent: 90,
        duration: 120,
        file_size: 4096,
        semantic_occurrences: 0,
        has_both_match_types: false,
      },
    ],
    total_results: 1,
    total_files: 1,
    page: 3,
    page_size: 20,
    total_pages: 5,
    search_time_ms: 42,
    filters_applied: { query: 'hello' },
    search_mode: 'hybrid',
  };

  it('populates results and pagination fields from the response', () => {
    searchStore.setResults(response);

    const state = get(searchStore);
    expect(state.results).toEqual(response.results);
    expect(state.totalResults).toBe(1);
    expect(state.totalFiles).toBe(1);
    expect(state.page).toBe(3);
    expect(state.totalPages).toBe(5);
    expect(state.searchTimeMs).toBe(42);
    expect(state.filtersApplied).toEqual({ query: 'hello' });
  });

  it('clears loading and error state', () => {
    searchStore.setLoading(true);
    searchStore.setError('previous failure');

    searchStore.setResults(response);

    const state = get(searchStore);
    expect(state.isLoading).toBe(false);
    expect(state.error).toBeNull();
  });

  it('defaults summaryResults/summaryTotal to empty when the response carries none', () => {
    searchStore.setResults(response);

    const state = get(searchStore);
    expect(state.summaryResults).toEqual([]);
    expect(state.summaryTotal).toBe(0);
  });

  it('populates summaryResults/summaryTotal when the response carries them (result_type=summaries)', () => {
    const summaryResponse: SearchResponse = {
      ...response,
      results: [],
      summary_results: [
        {
          file_uuid: 'uuid-2',
          file_id: 2,
          title: 'Kickoff',
          matches: [{ key_path: 'bluf', snippet: 'Ship it Friday.' }],
        },
      ],
      summary_total: 1,
    };

    searchStore.setResults(summaryResponse);

    const state = get(searchStore);
    expect(state.summaryResults).toEqual(summaryResponse.summary_results);
    expect(state.summaryTotal).toBe(1);
  });

  it('resets a stale summaryResults page when a later response omits summaries', () => {
    searchStore.setResults({
      ...response,
      summary_results: [{ file_uuid: 'uuid-2', file_id: 2, title: 'Kickoff', matches: [] }],
      summary_total: 1,
    });
    expect(get(searchStore).summaryTotal).toBe(1);

    searchStore.setResults(response);

    const state = get(searchStore);
    expect(state.summaryResults).toEqual([]);
    expect(state.summaryTotal).toBe(0);
  });
});

describe('reset', () => {
  it('restores every field to its initial value', () => {
    const initial = get(searchStore);

    searchStore.setQuery('hello');
    searchStore.setPage(5);
    searchStore.setSpeakers(['alice']);
    searchStore.setLoading(true);
    searchStore.setError('boom');
    searchStore.setScrollPosition(999);

    searchStore.reset();

    expect(get(searchStore)).toEqual(initial);
  });
});

describe('derived stores', () => {
  it('searchQuery mirrors the store query field', () => {
    expect(get(searchQuery)).toBe('');

    searchStore.setQuery('hello');

    expect(get(searchQuery)).toBe('hello');
  });

  it('isSearchLoading mirrors the store loading field', () => {
    expect(get(isSearchLoading)).toBe(false);

    searchStore.setLoading(true);

    expect(get(isSearchLoading)).toBe(true);
  });

  it('searchResults mirrors the store results field', () => {
    expect(get(searchResults)).toEqual([]);

    const response: SearchResponse = {
      query: 'hello',
      results: [
        {
          file_uuid: 'uuid-1',
          file_id: 1,
          title: 'Meeting',
          speakers: [],
          tags: [],
          upload_time: '2026-01-01T00:00:00Z',
          language: 'en',
          content_type: 'audio/mp4',
          relevance_score: 0.5,
          occurrences: [],
          total_occurrences: 0,
          title_highlighted: 'Meeting',
          keyword_occurrences: 0,
          semantic_only: false,
          semantic_confidence: 'low',
          match_sources: [],
          relevance_percent: 50,
          duration: 60,
          file_size: 1024,
          semantic_occurrences: 0,
          has_both_match_types: false,
        },
      ],
      total_results: 1,
      total_files: 1,
      page: 1,
      page_size: 20,
      total_pages: 1,
      search_time_ms: 10,
      filters_applied: {},
    };

    searchStore.setResults(response);

    expect(get(searchResults)).toEqual(response.results);
  });
});
