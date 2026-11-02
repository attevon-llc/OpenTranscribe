import { SEARCH_SOURCES, searchStore, type SearchSource, type SearchState } from '$stores/search';

/** The slice of the search store that shapes a request. */
type RequestState = Pick<
  SearchState,
  | 'sortBy'
  | 'sortOrder'
  | 'searchMode'
  | 'selectedSources'
  | 'selectedSpeakers'
  | 'selectedTags'
  | 'selectedCollectionId'
  | 'selectedLanguage'
  | 'selectedFileTypes'
  | 'dateFrom'
  | 'dateTo'
  | 'durationRange'
  | 'pageSize'
>;

export interface SearchUrlState {
  query: string;
  page: number;
  sort: string;
  sortOrder: 'asc' | 'desc';
  mode: string;
  /** `null` means "no URL override" — keep the store's default (all four). */
  sources: SearchSource[] | null;
  speakers: string[];
  tags: string[];
  /** A collection UUID, as the info card's collection chips link it. */
  collection: string | null;
}

/**
 * Issue #760: `sources=` (repeated) replaces the exclusive `type=` param. The legacy `type=` is
 * still read so links already in history/docs resolve, mapped through the old two-value scheme.
 */
export function parseSourcesParam(searchParams: URLSearchParams): SearchSource[] | null {
  const raw = searchParams.getAll('sources');
  if (raw.length) {
    const valid = raw.filter((s): s is SearchSource =>
      (SEARCH_SOURCES as readonly string[]).includes(s)
    );
    return valid.length ? valid : null;
  }
  const legacyType = searchParams.get('type');
  if (legacyType === 'summaries') return ['summary'];
  if (legacyType === 'all') return [...SEARCH_SOURCES];
  if (legacyType === 'transcripts') return ['content', 'title', 'speaker'];
  return null;
}

export function parseSearchUrl(searchParams: URLSearchParams): SearchUrlState {
  return {
    query: searchParams.get('q') || '',
    page: parseInt(searchParams.get('page') || '1'),
    sort: searchParams.get('sort') || 'relevance',
    sortOrder: (searchParams.get('sort_order') || 'desc') as 'asc' | 'desc',
    mode: searchParams.get('mode') || 'hybrid',
    sources: parseSourcesParam(searchParams),
    speakers: searchParams.getAll('speakers'),
    tags: searchParams.getAll('tags'),
    collection: searchParams.get('collection'),
  };
}

/** The `/search?...` query string that reproduces this search; defaults are omitted. */
export function buildPageUrlParams(
  query: string,
  pageNum: number,
  s: RequestState
): URLSearchParams {
  const params = new URLSearchParams();
  params.set('q', query);
  if (pageNum > 1) params.set('page', String(pageNum));
  if (s.sortBy !== 'relevance') params.set('sort', s.sortBy);
  if (s.sortOrder !== 'desc') params.set('sort_order', s.sortOrder);
  if (s.searchMode !== 'hybrid') params.set('mode', s.searchMode);
  // Omit `sources=` when the selection equals the default (all four) — a reload with no
  // override falls back to that same default (see parseSourcesParam).
  const isDefaultSources =
    s.selectedSources.length === SEARCH_SOURCES.length &&
    SEARCH_SOURCES.every((src) => s.selectedSources.includes(src));
  if (!isDefaultSources) {
    s.selectedSources.forEach((src) => params.append('sources', src));
  }
  s.selectedSpeakers.forEach((sp) => params.append('speakers', sp));
  s.selectedTags.forEach((tag) => params.append('tags', tag));
  if (s.selectedCollectionId) params.set('collection', s.selectedCollectionId);
  return params;
}

/** The parameters of `GET /search`. */
export function buildApiParams(
  query: string,
  pageNum: number,
  s: RequestState
): Record<string, unknown> {
  const apiParams: Record<string, unknown> = {
    q: query,
    page: pageNum,
    page_size: s.pageSize,
    sort_by: s.sortBy,
    sort_order: s.sortOrder,
    search_mode: s.searchMode,
    // Always sent explicitly (issue #760) — the UI default (all four) differs from
    // `result_type`'s legacy default (transcripts only), so omitting this would silently drop
    // the Summary section despite it showing selected.
    sources: [...s.selectedSources],
    speakers: s.selectedSpeakers.length ? s.selectedSpeakers : undefined,
    tags: s.selectedTags.length ? s.selectedTags : undefined,
    date_from: s.dateFrom || undefined,
    date_to: s.dateTo || undefined,
  };

  // Gallery filter params
  if (s.selectedFileTypes.length) apiParams.file_type = s.selectedFileTypes;
  if (s.selectedLanguage) apiParams.language = s.selectedLanguage;
  // The sidebar's collection ids are UUIDs: `collection_uuid`, never the integer
  // `collection_id` (sending a UUID there answered 422 for every collection filter).
  if (s.selectedCollectionId) apiParams.collection_uuid = s.selectedCollectionId;
  if (s.durationRange.min !== null) apiParams.min_duration = s.durationRange.min;
  if (s.durationRange.max !== null) apiParams.max_duration = s.durationRange.max;
  return apiParams;
}

/** Repeat array values as `key=a&key=b` (what FastAPI list params expect); skip `undefined`. */
export function serializeApiParams(params: Record<string, unknown>): string {
  const searchParams = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined) return;
    if (Array.isArray(value)) {
      value.forEach((v) => searchParams.append(key, v));
    } else {
      searchParams.set(key, String(value));
    }
  });
  return searchParams.toString();
}

/** Identifies the search that produced the stored results, so a revisit can skip the request. */
export function searchCacheKey(query: string, pageNum: number, s: SearchState): string {
  return JSON.stringify({
    q: query,
    page: pageNum,
    sort: s.sortBy,
    sortOrder: s.sortOrder,
    mode: s.searchMode,
    sources: [...s.selectedSources].sort(),
    speakers: s.selectedSpeakers,
    tags: s.selectedTags,
    dateFrom: s.dateFrom,
    dateTo: s.dateTo,
    fileTypes: s.selectedFileTypes,
    collectionId: s.selectedCollectionId,
    durationRange: s.durationRange,
  });
}

export interface FilterSidebarDetail {
  tags?: string[];
  speaker?: string[];
  collectionId?: string | null;
  language?: string | null;
  dates?: { from: Date | null; to: Date | null } | null;
  durationRange?: { min: number | null; max: number | null };
  fileTypes?: string[];
  statuses?: string[];
}

/** Map a FilterSidebar change event onto the search store; only the keys present apply. */
export function applyFilterDetail(detail: FilterSidebarDetail): void {
  if (detail.tags !== undefined) searchStore.setTags(detail.tags);
  if (detail.speaker !== undefined) searchStore.setSpeakers(detail.speaker);
  if (detail.collectionId !== undefined) searchStore.setCollectionId(detail.collectionId);
  if (detail.language !== undefined) searchStore.setLanguage(detail.language);
  if (detail.dates !== undefined) {
    const dateFrom = detail.dates?.from ? detail.dates.from.toISOString().split('T')[0] : '';
    const dateTo = detail.dates?.to ? detail.dates.to.toISOString().split('T')[0] : '';
    searchStore.setDateRange(dateFrom, dateTo);
  }
  if (detail.durationRange !== undefined) searchStore.setDurationRange(detail.durationRange);
  if (detail.fileTypes !== undefined) searchStore.setFileTypes(detail.fileTypes);
  if (detail.statuses !== undefined) searchStore.setStatuses(detail.statuses);
}

/** Clear every sidebar filter in the search store. */
export function resetStoreFilters(): void {
  searchStore.setSpeakers([]);
  searchStore.setTags([]);
  searchStore.setDateRange('', '');
  searchStore.setFileTypes([]);
  searchStore.setCollectionId(null);
  searchStore.setLanguage(null);
  searchStore.setDurationRange({ min: null, max: null });
  searchStore.setStatuses([]);
}
