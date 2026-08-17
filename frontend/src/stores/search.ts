import { writable, derived } from 'svelte/store';

export interface SearchOccurrence {
  snippet: string;
  speaker: string;
  speaker_highlighted?: string;
  start_time: number;
  end_time: number;
  chunk_index: number;
  score: number;
  match_type: 'content' | 'title' | 'speaker';
  has_keyword_match: boolean;
  highlight_type: 'keyword' | 'semantic';
}

export interface SearchHit {
  file_uuid: string;
  file_id: number;
  title: string;
  speakers: string[];
  tags: string[];
  upload_time: string;
  language: string;
  content_type: string;
  relevance_score: number;
  occurrences: SearchOccurrence[];
  total_occurrences: number;
  title_highlighted: string;
  keyword_occurrences: number;
  semantic_only: boolean;
  semantic_confidence: string;
  match_sources: string[];
  relevance_percent: number;
  duration: number;
  file_size: number;
  semantic_occurrences: number;
  has_both_match_types: boolean;
}

export interface SearchResponse {
  query: string;
  results: SearchHit[];
  total_results: number;
  total_files: number;
  page: number;
  page_size: number;
  total_pages: number;
  search_time_ms: number;
  filters_applied: Record<string, any>;
  search_mode?: string;
}

export interface SearchState {
  query: string;
  results: SearchHit[];
  totalResults: number;
  totalFiles: number;
  page: number;
  pageSize: number;
  totalPages: number;
  searchTimeMs: number;
  filtersApplied: Record<string, any>;
  isLoading: boolean;
  error: string | null;
  sortBy: string;
  sortOrder: 'asc' | 'desc';
  searchMode: string;
  selectedSpeakers: string[];
  selectedTags: string[];
  dateFrom: string;
  dateTo: string;
  selectedFileTypes: string[];
  selectedCollectionId: string | null;
  durationRange: { min: number | null; max: number | null };
  fileSizeRange: { min: number | null; max: number | null };
  selectedStatuses: string[];
  titleFilter: string;
  lastSearchParams: string;
  scrollPosition: number;
}

const initialState: SearchState = {
  query: '',
  results: [],
  totalResults: 0,
  totalFiles: 0,
  page: 1,
  pageSize: 20,
  totalPages: 0,
  searchTimeMs: 0,
  filtersApplied: {},
  isLoading: false,
  error: null,
  sortBy: 'relevance',
  sortOrder: 'desc',
  searchMode: 'hybrid',
  selectedSpeakers: [],
  selectedTags: [],
  dateFrom: '',
  dateTo: '',
  selectedFileTypes: [],
  selectedCollectionId: null,
  durationRange: { min: null, max: null },
  fileSizeRange: { min: null, max: null },
  selectedStatuses: [],
  titleFilter: '',
  lastSearchParams: '',
  scrollPosition: 0,
};

function createSearchStore() {
  const { subscribe, set, update } = writable<SearchState>(initialState);

  return {
    subscribe,
    setQuery: (query: string) => update((s) => ({ ...s, query, page: 1 })),
    setPage: (page: number) => update((s) => ({ ...s, page: Math.max(1, page) })),
    setSortBy: (sortBy: string) => update((s) => ({ ...s, sortBy, page: 1 })),
    setSortOrder: (sortOrder: 'asc' | 'desc') => update((s) => ({ ...s, sortOrder, page: 1 })),
    setSort: (sortBy: string, sortOrder: 'asc' | 'desc') =>
      update((s) => ({ ...s, sortBy, sortOrder, page: 1 })),
    setSearchMode: (searchMode: string) => update((s) => ({ ...s, searchMode, page: 1 })),
    setLoading: (isLoading: boolean) => update((s) => ({ ...s, isLoading })),
    setError: (error: string | null) => update((s) => ({ ...s, error })),
    setSpeakers: (selectedSpeakers: string[]) =>
      update((s) => ({ ...s, selectedSpeakers, page: 1 })),
    setTags: (selectedTags: string[]) => update((s) => ({ ...s, selectedTags, page: 1 })),
    setDateRange: (dateFrom: string, dateTo: string) =>
      update((s) => ({ ...s, dateFrom, dateTo, page: 1 })),
    setFileTypes: (selectedFileTypes: string[]) =>
      update((s) => ({ ...s, selectedFileTypes, page: 1 })),
    setCollectionId: (selectedCollectionId: string | null) =>
      update((s) => ({ ...s, selectedCollectionId, page: 1 })),
    setDurationRange: (durationRange: { min: number | null; max: number | null }) =>
      update((s) => ({ ...s, durationRange, page: 1 })),
    setFileSizeRange: (fileSizeRange: { min: number | null; max: number | null }) =>
      update((s) => ({ ...s, fileSizeRange, page: 1 })),
    setStatuses: (selectedStatuses: string[]) =>
      update((s) => ({ ...s, selectedStatuses, page: 1 })),
    setTitleFilter: (titleFilter: string) => update((s) => ({ ...s, titleFilter, page: 1 })),
    setFilters: (filters: Partial<SearchState>) => update((s) => ({ ...s, ...filters, page: 1 })),
    setLastSearchParams: (lastSearchParams: string) => update((s) => ({ ...s, lastSearchParams })),
    setScrollPosition: (scrollPosition: number) => update((s) => ({ ...s, scrollPosition })),
    setResults: (response: SearchResponse) =>
      update((s) => ({
        ...s,
        results: response.results,
        totalResults: response.total_results,
        totalFiles: response.total_files,
        page: response.page,
        totalPages: response.total_pages,
        searchTimeMs: response.search_time_ms,
        filtersApplied: response.filters_applied,
        isLoading: false,
        error: null,
      })),
    reset: () => set(initialState),
  };
}

export const searchStore = createSearchStore();
export const searchResults = derived(searchStore, ($s) => $s.results);
export const isSearchLoading = derived(searchStore, ($s) => $s.isLoading);
export const searchQuery = derived(searchStore, ($s) => $s.query);
