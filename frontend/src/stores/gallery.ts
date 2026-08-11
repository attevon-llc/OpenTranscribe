import { writable, derived } from 'svelte/store';
import { browser } from '$app/environment';
import type { MediaFile } from '$lib/types/media';

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
  activeTab: 'gallery' | 'status';
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
  filterFileSizeRange: { min: number | null; max: number | null };
  filterSelectedFileTypes: string[];
  filterSelectedStatuses: string[];
  filterOwnershipFilter: 'all' | 'mine' | 'shared';
  filterSortBy: string;
  filterSortOrder: 'asc' | 'desc';
}

export interface GalleryActions {
  setActiveTab: (tab: 'gallery' | 'status') => void;
  setViewMode: (mode: ViewMode) => void;
  toggleSelection: () => void;
  setSelecting: (selecting: boolean) => void;
  toggleFileSelection: (fileId: string) => void; // UUID
  handleMultiSelect: (fileId: string, ctrlKey: boolean, shiftKey: boolean) => void;
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
  appendFiles: (newFiles: MediaFile[], metadata: PaginationMetadata) => void;
  resetPagination: () => void;
  setLoadingMore: (loading: boolean) => void;
}

// Initial state
const initialState: GalleryState = {
  activeTab: 'gallery',
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
  filterSearchQuery: '',
  filterSelectedTags: [],
  filterSelectedSpeakers: [],
  filterSelectedCollectionId: null,
  filterDateRange: { from: null, to: null },
  filterDurationRange: { min: null, max: null },
  filterFileSizeRange: { min: null, max: null },
  filterSelectedFileTypes: [],
  filterSelectedStatuses: [],
  filterOwnershipFilter: 'all',
  filterSortBy: 'upload_time',
  filterSortOrder: 'desc',
};

// Create the store
function createGalleryStore() {
  const { subscribe, set, update } = writable<GalleryState>(initialState);

  // Action stores for triggering operations
  const uploadTrigger = writable<number>(0);
  const collectionsTrigger = writable<number>(0);
  const tagsTrigger = writable<number>(0);
  const addToCollectionTrigger = writable<number>(0);
  const addTagsTrigger = writable<number>(0);
  const removeTagsTrigger = writable<number>(0);
  const deleteSelectedTrigger = writable<number>(0);
  const reprocessTrigger = writable<number>(0);
  const summarizeTrigger = writable<number>(0);
  const redactTrigger = writable<number>(0);
  const retryFailedTrigger = writable<number>(0);
  const exportTrigger = writable<string>(''); // format string
  const speakerIdTrigger = writable<number>(0);
  const cancelProcessingTrigger = writable<number>(0);

  return {
    subscribe,
    // State management actions
    setActiveTab: (tab: 'gallery' | 'status') => {
      update((state) => ({ ...state, activeTab: tab }));
    },

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
        const newSelected = new Set(state.selectedFiles);
        if (newSelected.has(fileId)) {
          newSelected.delete(fileId);
        } else {
          newSelected.add(fileId);
        }
        return { ...state, selectedFiles: newSelected, lastSelectedId: fileId };
      });
    },

    handleMultiSelect: (fileId: string, ctrlKey: boolean, shiftKey: boolean) => {
      update((state) => {
        const newSelected = new Set(state.selectedFiles);

        if (shiftKey && state.lastSelectedId) {
          // Range select: find indices of anchor and target in files array
          const fileIds = state.files.map((f) => f.uuid);
          const anchorIdx = fileIds.indexOf(state.lastSelectedId);
          const targetIdx = fileIds.indexOf(fileId);

          if (anchorIdx !== -1 && targetIdx !== -1) {
            const start = Math.min(anchorIdx, targetIdx);
            const end = Math.max(anchorIdx, targetIdx);
            for (let i = start; i <= end; i++) {
              newSelected.add(fileIds[i]);
            }
          } else {
            // Fallback: just toggle the item
            if (newSelected.has(fileId)) {
              newSelected.delete(fileId);
            } else {
              newSelected.add(fileId);
            }
          }
        } else {
          // Ctrl/Cmd+click or shift without anchor: toggle individual
          if (newSelected.has(fileId)) {
            newSelected.delete(fileId);
          } else {
            newSelected.add(fileId);
          }
        }

        const isSelecting = newSelected.size > 0;
        return {
          ...state,
          selectedFiles: newSelected,
          lastSelectedId: fileId,
          isSelecting,
        };
      });
    },

    selectAllFiles: () => {
      update((state) => {
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
      update((state) => ({ ...state, files: files || [] }));
    },

    toggleFilters: () => {
      update((state) => ({ ...state, showFilters: !state.showFilters }));
    },

    // Action triggers (for UI operations)
    triggerUpload: () => {
      uploadTrigger.update((n) => n + 1);
    },

    triggerTags: () => {
      tagsTrigger.update((n) => n + 1);
    },
    triggerCollections: () => {
      collectionsTrigger.update((n) => n + 1);
    },

    triggerAddToCollection: () => {
      addToCollectionTrigger.update((n) => n + 1);
    },

    // Bulk tagging is two triggers, not one carrying a direction: the toolbar
    // has two menu entries, and a single trigger would have to be reset between
    // fires the way `exportTrigger` is.
    triggerAddTags: () => {
      addTagsTrigger.update((n) => n + 1);
    },

    triggerRemoveTags: () => {
      removeTagsTrigger.update((n) => n + 1);
    },

    triggerDeleteSelected: () => {
      deleteSelectedTrigger.update((n) => n + 1);
    },

    triggerReprocess: () => {
      reprocessTrigger.update((n) => n + 1);
    },

    triggerSummarize: () => {
      summarizeTrigger.update((n) => n + 1);
    },

    triggerRedact: () => {
      redactTrigger.update((n) => n + 1);
    },

    triggerRetryFailed: () => {
      retryFailedTrigger.update((n) => n + 1);
    },

    triggerExport: (format: string) => {
      exportTrigger.set(format);
    },

    triggerSpeakerId: () => {
      speakerIdTrigger.update((n) => n + 1);
    },

    triggerCancelProcessing: () => {
      cancelProcessingTrigger.update((n) => n + 1);
    },

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

    saveFilters: (filters: {
      searchQuery: string;
      selectedTags: string[];
      selectedSpeakers: string[];
      selectedCollectionId: string | null;
      dateRange: { from: Date | null; to: Date | null };
      durationRange: { min: number | null; max: number | null };
      fileSizeRange: { min: number | null; max: number | null };
      selectedFileTypes: string[];
      selectedStatuses: string[];
      ownershipFilter: 'all' | 'mine' | 'shared';
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
        filterFileSizeRange: { ...filters.fileSizeRange },
        filterSelectedFileTypes: [...filters.selectedFileTypes],
        filterSelectedStatuses: [...filters.selectedStatuses],
        filterOwnershipFilter: filters.ownershipFilter,
        filterSortBy: filters.sortBy,
        filterSortOrder: filters.sortOrder,
      }));
    },

    resetFilters: () => {
      update((state) => ({
        ...state,
        filterSearchQuery: '',
        filterSelectedTags: [],
        filterSelectedSpeakers: [],
        filterSelectedCollectionId: null,
        filterDateRange: { from: null, to: null },
        filterDurationRange: { min: null, max: null },
        filterFileSizeRange: { min: null, max: null },
        filterSelectedFileTypes: [],
        filterSelectedStatuses: [],
        filterOwnershipFilter: 'all',
        filterSortBy: 'upload_time',
        filterSortOrder: 'desc',
      }));
    },

    // Subscribe to action triggers (skip initial values)
    onUploadTrigger: (callback: (value: number) => void) => {
      let hasInitialized = false;
      return uploadTrigger.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },

    onTagsTrigger: (callback: (value: number) => void) => {
      let hasInitialized = false;
      return tagsTrigger.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },
    onCollectionsTrigger: (callback: (value: number) => void) => {
      let hasInitialized = false;
      return collectionsTrigger.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },

    onAddToCollectionTrigger: (callback: (value: number) => void) => {
      let hasInitialized = false;
      return addToCollectionTrigger.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },

    onAddTagsTrigger: (callback: (value: number) => void) => {
      let hasInitialized = false;
      return addTagsTrigger.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },

    onRemoveTagsTrigger: (callback: (value: number) => void) => {
      let hasInitialized = false;
      return removeTagsTrigger.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },

    onDeleteSelectedTrigger: (callback: (value: number) => void) => {
      let hasInitialized = false;
      return deleteSelectedTrigger.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },

    onReprocessTrigger: (callback: (value: number) => void) => {
      let hasInitialized = false;
      return reprocessTrigger.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },

    onSummarizeTrigger: (callback: (value: number) => void) => {
      let hasInitialized = false;
      return summarizeTrigger.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },

    onRedactTrigger: (callback: (value: number) => void) => {
      let hasInitialized = false;
      return redactTrigger.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },

    onRetryFailedTrigger: (callback: (value: number) => void) => {
      let hasInitialized = false;
      return retryFailedTrigger.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },

    onExportTrigger: (callback: (format: string) => void) => {
      let hasInitialized = false;
      return exportTrigger.subscribe((value) => {
        if (hasInitialized && value !== '') {
          callback(value);
          // Reset so the same format can be triggered again
          exportTrigger.set('');
        }
        hasInitialized = true;
      });
    },

    onSpeakerIdTrigger: (callback: (value: number) => void) => {
      let hasInitialized = false;
      return speakerIdTrigger.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },

    onCancelProcessingTrigger: (callback: (value: number) => void) => {
      let hasInitialized = false;
      return cancelProcessingTrigger.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },
  };
}

export const galleryStore = createGalleryStore();

// Derived stores for convenient access
export const galleryState = derived(galleryStore, ($store) => $store);
export const isGalleryPage = derived(galleryStore, ($store) => $store.activeTab === 'gallery');
export const galleryViewMode = derived(galleryStore, ($store) => $store.viewMode);
export const selectedCount = derived(galleryStore, ($store) => $store.selectedFiles.size);
export const allFilesSelected = derived(
  galleryStore,
  ($store) => $store.files.length > 0 && $store.selectedFiles.size === $store.files.length
);
export const hasMoreFiles = derived(galleryStore, ($store) => $store.hasMoreFiles);
export const isLoadingMore = derived(galleryStore, ($store) => $store.isLoadingMore);
export const galleryTotalCount = derived(galleryStore, ($store) => $store.totalFiles);
