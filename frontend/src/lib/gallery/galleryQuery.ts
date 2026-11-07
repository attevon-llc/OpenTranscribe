/**
 * The gallery's `GET /files` (and collection media) query string, built from its filter state.
 *
 * Filtering, sorting and pagination are server-driven (`components/gallery/CLAUDE.md`); this is
 * the one place that turns the page's filter state into query parameters, so a new filter is a
 * field here and a line of state on the page, not another `params.append` in a 1,400-line script.
 */
import type { HiddenMode } from '$lib/api/hiddenMedia';
import type { DateRange, DurationRange } from '$lib/types/media';

export interface GalleryQueryState {
  page: number;
  pageSize: number;
  sortBy: string;
  sortOrder: 'asc' | 'desc';
  searchQuery: string;
  selectedTags: string[];
  selectedSpeakers: string[];
  dateRange: DateRange;
  durationRange: DurationRange;
  selectedFileTypes: string[];
  selectedStatuses: string[];
  /** The backend defaults to 'mine' when omitted, so it is always sent. */
  ownershipFilter: 'all' | 'mine' | 'shared';
  /** Owner UUIDs (issue #966), orthogonal to the ownership bucket. */
  selectedOwners: string[];
  /** The caller's hidden files (issue #1102). */
  hiddenMode: HiddenMode;
  /** The computed "Legal hold" tag/collection: `held=true` lists held files only. */
  held: boolean;
}

export function buildGalleryQuery(state: GalleryQueryState): URLSearchParams {
  const params = new URLSearchParams();
  params.append('page', state.page.toString());
  params.append('page_size', state.pageSize.toString());
  params.append('sort_by', state.sortBy);
  params.append('sort_order', state.sortOrder);
  if (state.searchQuery) params.append('search', state.searchQuery);
  state.selectedTags.forEach((tag) => params.append('tag', tag));
  state.selectedSpeakers.forEach((speaker) => params.append('speaker', speaker));
  if (state.dateRange.from) params.append('from_date', state.dateRange.from.toISOString());
  if (state.dateRange.to) params.append('to_date', state.dateRange.to.toISOString());
  if (state.durationRange.min !== null) {
    params.append('min_duration', state.durationRange.min.toString());
  }
  if (state.durationRange.max !== null) {
    params.append('max_duration', state.durationRange.max.toString());
  }
  state.selectedFileTypes.forEach((fileType) => params.append('file_type', fileType));
  state.selectedStatuses.forEach((status) => params.append('status', status));
  if (state.ownershipFilter) params.append('ownership', state.ownershipFilter);
  state.selectedOwners.forEach((ownerUuid) => params.append('owner', ownerUuid));
  params.append('hidden', state.hiddenMode);
  if (state.held) params.append('held', 'true');
  return params;
}
