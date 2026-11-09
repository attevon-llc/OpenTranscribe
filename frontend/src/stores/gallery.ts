import { writable, derived, get } from 'svelte/store';
import { browser } from '$app/env';
import type { MediaFile } from '$lib/types/media';
import type { HiddenMode } from '$lib/api/hiddenMedia';
import { selectionAfterClick, toggled } from '$lib/gallery/selection';
import { createGalleryTriggers } from './galleryActionTriggers';

// LocalStorage key for view mode persistence
const VIEW_MODE_STORAGE_KEY = 'gallery-view-mode';

// Helper to get persisted view mode
function getPersistedViewMode(): 'grid' | 'list' {
  if (!browser) return 'grid';
  const stored = localStorage.getItem(VIEW_MODE_STORAGE_KEY);
  if (stored === 'list' || stored === 'grid') {
    return stored;
  }
  return 'grid';
}

// Pagination metadata interface
interface PaginationMetadata {
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
  hasMore: boolean;
}

// Types for gallery state
export type ViewMode = 'grid' | 'list';

export interface GalleryState {
  viewMode: ViewMode;
  isSelecting: boolean;
  selectedFiles: Set<string>; // UUIDs
  lastSelectedId: string | null; // For shift+click range selection
  files: MediaFile[];
  showFilters: boolean;
  currentPage: number; // 0 = nothing loaded
  pageSize: number;
  totalFiles: number;
  totalPages: number;
  hasMoreFiles: boolean;
  isLoadingMore: boolean;
  scrollTop: number; // For scroll position restoration on back navigation

  // Filter persistence (survives client-side navigation)
  filterSearchQuery: string;
  filterSelectedTags: string[];
  filterSelectedSpeakers: string[];
  filterSelectedCollectionId: string | null;
  filterDateRange: { from: Date | null; to: Date | null };
  filterDurationRange: { min: number | null; max: number | null };
  filterSelectedFileTypes: string[];
  filterSelectedStatuses: string[];
  filterOwnershipFilter: 'all' | 'mine' | 'shared';
  filterSelectedOwners: string[]; // Owner UUIDs (issue #966)
  filterHiddenMode: HiddenMode; // The caller's hidden files (issue #1102)
  filterHeld: boolean; // The computed "Legal hold" tag/collection: held files only (held=true)
  filterSortBy: string;
  filterSortOrder: 'asc' | 'desc';
}

export interface GalleryActions {
  setViewMode: (mode: ViewMode) => void;
  toggleSelection: () => void;
  setSelecting: (selecting: boolean) => void;
  toggleFileSelection: (fileId: string) => void; // UUID
  handleMultiSelect: (fileId: string, shiftKey: boolean) => void;
  selectAllFiles: () => void;
  clearSelection: () => void;
  setFiles: (files: MediaFile[]) => void;
  toggleFilters: () => void;
  triggerUpload: () => void;
  triggerCollections: () => void;
  /** Open the tag surface; the gallery picks manage-vs-apply by selection. */
  triggerTags: () => void;
  triggerAddToCollection: () => void;
  triggerAddTags: () => void;
  triggerRemoveTags: () => void;
  triggerDeleteSelected: () => void;
  triggerReprocess: () => void;
  triggerSummarize: () => void;
  triggerRedact: () => void;
  triggerRetryFailed: () => void;
  triggerExport: (format: string) => void;
  triggerSpeakerId: () => void;
  triggerCancelProcessing: () => void;
  /** Admin-only takedown (issue #576). Every status is quarantinable, deliberately
   * — do not filter to `completed` the way the chat/summarize entries do. */
  triggerQuarantine: () => void;
  /** Some files were hidden or unhidden for the caller (issue #1102): reload the list. */
  notifyHiddenChanged: () => void;
  /** Which of the caller's hidden files the gallery lists (issue #1102); reloads the list. */
  setHiddenMode: (mode: HiddenMode) => void;
  /** Admin-only: place a legal hold on the selection (legal-hold plan, Lane D). */
  triggerPlaceHold: () => void;
  /**
   * Show only held files (the computed "Legal hold" tag and collection), or stop. The held set
   * is itself a collection, so turning it on clears a selected collection. `notify: false`
   * when the caller reloads the list itself.
   */
  setHeldFilter: (held: boolean, notify?: boolean) => void;
  appendFiles: (newFiles: MediaFile[], metadata: PaginationMetadata) => void;
  resetPagination: () => void;
  setLoadingMore: (loading: boolean) => void;
}

/** The filter fields at rest: nothing selected, newest upload first. Fresh values per call. */
function defaultFilters() {
  return {
    filterSearchQuery: '',
    filterSelectedTags: [] as string[],
    filterSelectedSpeakers: [] as string[],
    filterSelectedCollectionId: null as string | null,
    filterDateRange: { from: null, to: null } as GalleryState['filterDateRange'],
    filterDurationRange: { min: null, max: null } as GalleryState['filterDurationRange'],
    filterSelectedFileTypes: [] as string[],
    filterSelectedStatuses: [] as string[],
    filterOwnershipFilter: 'all' as GalleryState['filterOwnershipFilter'],
    filterSelectedOwners: [] as string[],
    filterHiddenMode: 'exclude' as HiddenMode,
    filterHeld: false,
    filterSortBy: 'upload_time',
    filterSortOrder: 'desc' as GalleryState['filterSortOrder'],
  };
}

// Initial state
const initialState: GalleryState = {
  viewMode: getPersistedViewMode(),
  isSelecting: false,
  selectedFiles: new Set<string>(), // UUIDs
  lastSelectedId: null,
  files: [],
  showFilters: true,
  currentPage: 0,
  pageSize: 100,
  totalFiles: 0,
  totalPages: 0,
  hasMoreFiles: false,
  isLoadingMore: false,
  scrollTop: 0,
  ...defaultFilters(),
};

type UpdateGallery = (fn: (state: GalleryState) => GalleryState) => void;

// Paging: appending a fetched page, the loading flag, and scroll restoration.
function createPaginationActions(update: UpdateGallery) {
  return {
    appendFiles: (newFiles: MediaFile[], metadata: PaginationMetadata) => {
      update((state) => {
        // Guard against undefined/null data from failed API responses
        const safeNewFiles = Array.isArray(newFiles) ? newFiles : [];
        let files: MediaFile[];
        if (metadata.page === 1) {
          files = safeNewFiles;
        } else {
          // Deduplicate by uuid to prevent keyed {#each} errors when items
          // shift between pages during bulk processing status changes
          const existingUuids = new Set((state.files || []).map((f) => f.uuid));
          const uniqueNewFiles = safeNewFiles.filter((f) => !existingUuids.has(f.uuid));
          files = [...state.files, ...uniqueNewFiles];
        }
        return {
          ...state,
          files,
          currentPage: metadata.page,
          pageSize: metadata.pageSize,
          totalFiles: metadata.total,
          totalPages: metadata.totalPages,
          hasMoreFiles: metadata.hasMore,
        };
      });
    },

    resetPagination: () => {
      update((state) => ({
        ...state,
        currentPage: 0,
        totalFiles: 0,
        totalPages: 0,
        hasMoreFiles: false,
        isLoadingMore: false,
      }));
    },

    setLoadingMore: (loading: boolean) => {
      update((state) => ({ ...state, isLoadingMore: loading }));
    },

    setScrollTop: (scrollTop: number) => {
      update((state) => ({ ...state, scrollTop }));
    },
  };
}

// The persisted filter fields: saving the sidebar's selection and clearing it.
function createFilterActions(update: UpdateGallery) {
  return {
    saveFilters: (filters: {
      searchQuery: string;
      selectedTags: string[];
      selectedSpeakers: string[];
      selectedCollectionId: string | null;
      dateRange: { from: Date | null; to: Date | null };
      durationRange: { min: number | null; max: number | null };
      selectedFileTypes: string[];
      selectedStatuses: string[];
      ownershipFilter: 'all' | 'mine' | 'shared';
      selectedOwners: string[];
      sortBy: string;
      sortOrder: 'asc' | 'desc';
    }) => {
      update((state) => ({
        ...state,
        filterSearchQuery: filters.searchQuery,
        filterSelectedTags: [...filters.selectedTags],
        filterSelectedSpeakers: [...filters.selectedSpeakers],
        filterSelectedCollectionId: filters.selectedCollectionId,
        filterDateRange: { ...filters.dateRange },
        filterDurationRange: { ...filters.durationRange },
        filterSelectedFileTypes: [...filters.selectedFileTypes],
        filterSelectedStatuses: [...filters.selectedStatuses],
        filterOwnershipFilter: filters.ownershipFilter,
        filterSelectedOwners: [...filters.selectedOwners],
        filterSortBy: filters.sortBy,
        filterSortOrder: filters.sortOrder,
      }));
    },

    resetFilters: () => {
      update((state) => ({ ...state, ...defaultFilters() }));
    },
  };
}

// Create the store
function createGalleryStore() {
  const { subscribe, update } = writable<GalleryState>(initialState);

  // UI-operation triggers; the held trigger hands its observers the filter's current value.
  const { fireHiddenChanged, fireHeldChanged, ...publicTriggers } = createGalleryTriggers(
    () => get({ subscribe }).filterHeld
  );

  return {
    subscribe,
    // State management actions
    setViewMode: (mode: ViewMode) => {
      if (browser) {
        localStorage.setItem(VIEW_MODE_STORAGE_KEY, mode);
      }
      update((state) => ({ ...state, viewMode: mode }));
    },

    setSelecting: (selecting: boolean) => {
      update((state) => ({
        ...state,
        isSelecting: selecting,
        selectedFiles: selecting ? state.selectedFiles : new Set<string>(),
        lastSelectedId: selecting ? state.lastSelectedId : null,
      }));
    },

    toggleSelection: () => {
      update((state) => ({
        ...state,
        isSelecting: !state.isSelecting,
        selectedFiles: !state.isSelecting ? state.selectedFiles : new Set<string>(),
        lastSelectedId: !state.isSelecting ? state.lastSelectedId : null,
      }));
    },

    toggleFileSelection: (fileId: string) => {
      update((state) => {
        const selectedFiles = toggled(state.selectedFiles, fileId);
        return {
          ...state,
          selectedFiles,
          lastSelectedId: fileId,
          isSelecting: selectedFiles.size > 0,
        };
      });
    },

    handleMultiSelect: (fileId: string, shiftKey: boolean) => {
      update((state) => {
        const selectedFiles = selectionAfterClick(
          state.selectedFiles,
          state.files.map((f) => f.uuid),
          state.lastSelectedId,
          fileId,
          shiftKey
        );
        return {
          ...state,
          selectedFiles,
          lastSelectedId: fileId,
          isSelecting: selectedFiles.size > 0,
        };
      });
    },

    selectAllFiles: () => {
      update((state) => {
        // An empty list makes `size === length` (0 === 0) true, so "select all" of
        // nothing was read as "already all selected" and took the deselect branch —
        // clicking Select All while the file list is still loading silently selected
        // nothing instead of either selecting the (eventually non-empty) list or
        // visibly doing nothing.
        if (state.files.length === 0) {
          return state;
        }
        const allSelected = state.selectedFiles.size === state.files.length;
        const newSelected = allSelected
          ? new Set<string>()
          : new Set(state.files.map((f) => f.uuid));
        return { ...state, selectedFiles: newSelected };
      });
    },

    clearSelection: () => {
      update((state) => ({
        ...state,
        isSelecting: false,
        selectedFiles: new Set<string>(),
        lastSelectedId: null,
      }));
    },

    setFiles: (files: MediaFile[]) => {
      update((state) => {
        const safeFiles = files || [];
        const fileUuids = new Set(safeFiles.map((f) => f.uuid));
        const selectedFiles = new Set([...state.selectedFiles].filter((id) => fileUuids.has(id)));
        return { ...state, files: safeFiles, selectedFiles };
      });
    },

    toggleFilters: () => {
      update((state) => ({ ...state, showFilters: !state.showFilters }));
    },

    setHiddenMode: (mode: HiddenMode) => {
      update((state) => ({ ...state, filterHiddenMode: mode }));
      fireHiddenChanged();
    },

    setHeldFilter: (held: boolean, notify = true) => {
      update((state) => ({
        ...state,
        filterHeld: held,
        filterSelectedCollectionId: held ? null : state.filterSelectedCollectionId,
      }));
      if (notify) fireHeldChanged();
    },

    ...createPaginationActions(update),
    ...createFilterActions(update),
    ...publicTriggers,
  };
}

export const galleryStore = createGalleryStore();

// Derived stores for convenient access
export const galleryState = derived(galleryStore, ($store) => $store);
export const galleryViewMode = derived(galleryStore, ($store) => $store.viewMode);
export const selectedCount = derived(galleryStore, ($store) => $store.selectedFiles.size);
export const allFilesSelected = derived(
  galleryStore,
  ($store) => $store.files.length > 0 && $store.selectedFiles.size === $store.files.length
);
export const hasMoreFiles = derived(galleryStore, ($store) => $store.hasMoreFiles);
export const isLoadingMore = derived(galleryStore, ($store) => $store.isLoadingMore);
