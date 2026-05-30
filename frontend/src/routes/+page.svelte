<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import { get } from 'svelte/store';

  import { fade, scale } from 'svelte/transition';
  import { websocketStore } from '$stores/websocket';
  import { toastStore } from '$stores/toast';
  import { uploadsStore } from '$stores/uploads';
  import { isAuthenticated } from '$stores/auth';
  import CardGridSkeleton from '$components/ui/CardGridSkeleton.svelte';
  import { galleryStore, galleryState, hasMoreFiles, isLoadingMore, galleryTotalCount, galleryViewMode } from '$stores/gallery';
  import { t } from '$stores/locale';
  import ConfirmationModal from '../components/ConfirmationModal.svelte';
  import SelectiveReprocessModal from '../components/SelectiveReprocessModal.svelte';
  import GalleryCountChip from '$components/gallery/GalleryCountChip.svelte';
  import GallerySortDropdown from '$components/gallery/GallerySortDropdown.svelte';
  import GalleryViewToggle from '$components/gallery/GalleryViewToggle.svelte';
  import GalleryActionButtons from '$components/gallery/GalleryActionButtons.svelte';
  import VirtualList from '$components/gallery/VirtualList.svelte';
  import VirtualGrid from '$components/gallery/VirtualGrid.svelte';
  import Spinner from '../components/ui/Spinner.svelte';
  import EmptyState from '../components/ui/EmptyState.svelte';
  import type { MediaFile, DurationRange, DateRange } from '$lib/types/media';

  // Modal state
  let showUploadModal = false;

  // Confirmation modal state
  let showConfirmModal = false;
  let confirmModalTitle = '';
  let confirmModalMessage = '';
  let confirmCallback: (() => void) | null = null;

  // Bulk reprocess modal state
  let showBulkReprocessModal = false;
  let bulkReprocessFiles: any[] = [];
  let bulkReprocessing = false;

  // Define types
  interface FilterEvent {
    detail: {
      search: string;
      tags: string[];
      speaker: string[];
      collectionId: string | null;  // UUID
      dates: DateRange;
      durationRange?: DurationRange;
      fileSizeRange?: { min: number | null; max: number | null };
      fileTypes?: string[];
      statuses?: string[];
      ownership?: 'all' | 'mine' | 'shared';
    };
  }


  import axiosInstance from '../lib/axios';
  import { apiCache } from '$lib/apiCache';

  // Import components
  import FileUploader from '../components/FileUploader.svelte';
  import FilterSidebar from '../components/FilterSidebar.svelte';
  import CollectionsPanel from '../components/CollectionsPanel.svelte';
  import UserFileStatus from '../components/UserFileStatus.svelte';

  // Media files state
  let files: MediaFile[] = [];
  let loading: boolean = true;
  let error: string | null = null;

  // Animation and smooth update state
  let fileMap = new Map<string, MediaFile>();
  let pendingNewFiles = new Set<string>();
  let pendingDeletions = new Set<string>();
  let refreshTimeouts = new Map<string, number>();

  // Scroll container ref for virtual scrolling
  let scrollableContentEl: HTMLElement | null = null;

  // Continuously save scroll position for back-nav restoration
  let scrollSaveRaf: number | null = null;
  function onGalleryScroll() {
    if (scrollSaveRaf !== null) return;
    scrollSaveRaf = requestAnimationFrame(() => {
      scrollSaveRaf = null;
      if (scrollableContentEl) {
        galleryStore.setScrollTop(scrollableContentEl.scrollTop);
      }
    });
  }

  // Infinite scroll state
  let infiniteScrollSentinel: HTMLElement | null = null;
  let intersectionObserver: IntersectionObserver | null = null;

  // View state — restore from gallery store for navigation persistence
  let selectedCollectionId: string | null = $galleryState.filterSelectedCollectionId;
  let showCollectionsModal = false;

  // Use gallery store for state management
  $: activeTab = $galleryState.activeTab;
  $: isSelecting = $galleryState.isSelecting;
  $: selectedFiles = $galleryState.selectedFiles;

  // Component refs
  let filterSidebarRef: any;

  // WebSocket subscription
  let unsubscribeFileStatus: (() => void) | undefined;

  // Define WebSocket update type
  interface FileStatusUpdate {
    file_id: string;  // UUID
    status: 'pending' | 'processing' | 'completed' | 'error';
    progress?: number;
  }

  // Filter state — restore from gallery store for navigation persistence
  let searchQuery: string = $galleryState.filterSearchQuery;
  let selectedTags: string[] = [...$galleryState.filterSelectedTags];
  let selectedSpeakers: string[] = [...$galleryState.filterSelectedSpeakers];
  let dateRange = { ...$galleryState.filterDateRange };
  let durationRange = { ...$galleryState.filterDurationRange };
  let fileSizeRange = { ...$galleryState.filterFileSizeRange };
  let selectedFileTypes: string[] = [...$galleryState.filterSelectedFileTypes];
  let selectedStatuses: string[] = [...$galleryState.filterSelectedStatuses];
  let ownershipFilter: 'all' | 'mine' | 'shared' = $galleryState.filterOwnershipFilter;
  $: showFilters = $galleryState.showFilters;

  // Sort state — restore from gallery store
  let sortBy: string = $galleryState.filterSortBy;
  let sortOrder: 'asc' | 'desc' = $galleryState.filterSortOrder;

  // Observe the sentinel element when it becomes available (after files load)
  $: if (infiniteScrollSentinel && intersectionObserver) {
    intersectionObserver.observe(infiniteScrollSentinel);
  }

  /**
   * Show confirmation modal
   * @param {string} title - The modal title
   * @param {string} message - The confirmation message
   * @param {() => void} callback - The callback to execute on confirmation
   */
  function showConfirmation(title: string, message: string, callback: () => void) {
    confirmModalTitle = title;
    confirmModalMessage = message;
    confirmCallback = callback;
    showConfirmModal = true;
  }

  /**
   * Handle confirmation modal confirm
   */
  function handleConfirmModalConfirm() {
    if (confirmCallback) {
      confirmCallback();
      confirmCallback = null;
    }
    showConfirmModal = false;
  }

  /**
   * Handle confirmation modal cancel
   */
  function handleConfirmModalCancel() {
    confirmCallback = null;
    showConfirmModal = false;
  }

  /**
   * Handle force delete of processing files
   */
  async function handleForceDelete(conflictFileIds: string[]) {
    try {
      // Try force deletion for conflicted files
      const forceResponse = await axiosInstance.post('/files/management/bulk-action', {
        file_uuids: conflictFileIds,  // Use file_uuids (UUIDs)
        action: 'delete',
        force: true
      });

      const forceResults = forceResponse.data;
      const forceSuccessful = forceResults.filter((r: any) => r.success);

      if (forceSuccessful.length > 0) {
        toastStore.success($t('gallery.forceDeletedSuccess', { count: forceSuccessful.length }));
        // Remove force deleted files smoothly
        const forceSuccessfulIds = forceSuccessful.map((r: any) => r.file_uuid);  // Use file_uuid from response
        removeFilesSmooth(forceSuccessfulIds);
      }
    } catch (forceErr) {
      console.error('Error force deleting files:', forceErr);
      toastStore.error($t('gallery.forceDeleteFailed'));
    }

    clearSelection();
  }

  // Build query parameters for API calls
  function buildQueryParams(page: number = 1): URLSearchParams {
    const params = new URLSearchParams();

    // Pagination
    params.append('page', page.toString());
    params.append('page_size', '100');

    // Sort
    params.append('sort_by', sortBy);
    params.append('sort_order', sortOrder);

    // Search
    if (searchQuery) {
      params.append('search', searchQuery);
    }

    // Tags
    if (selectedTags.length > 0) {
      selectedTags.forEach(tag => params.append('tag', tag));
    }

    // Speakers
    if (selectedSpeakers.length > 0) {
      selectedSpeakers.forEach(speaker => params.append('speaker', speaker));
    }

    // Date range
    if (dateRange.from) {
      params.append('from_date', dateRange.from.toISOString());
    }
    if (dateRange.to) {
      params.append('to_date', dateRange.to.toISOString());
    }

    // Duration range
    if (durationRange.min !== null) {
      params.append('min_duration', durationRange.min.toString());
    }
    if (durationRange.max !== null) {
      params.append('max_duration', durationRange.max.toString());
    }

    // File size range
    if (fileSizeRange.min !== null) {
      params.append('min_file_size', fileSizeRange.min.toString());
    }
    if (fileSizeRange.max !== null) {
      params.append('max_file_size', fileSizeRange.max.toString());
    }

    // File types
    if (selectedFileTypes.length > 0) {
      selectedFileTypes.forEach(fileType => params.append('file_type', fileType));
    }

    // Statuses
    if (selectedStatuses.length > 0) {
      selectedStatuses.forEach(status => params.append('status', status));
    }

    // Ownership filter (backend defaults to 'mine' if omitted)
    if (ownershipFilter) {
      params.append('ownership', ownershipFilter);
    }

    return params;
  }

  // Fetch media files with smooth update support and pagination
  async function fetchFiles(isInitialLoad: boolean = false, skipAnimation: boolean = false) {
    // Reset pagination on new fetch
    galleryStore.resetPagination();

    // Always show loading state so the count chip displays "loading..." instead
    // of prematurely showing "no matches" while the query is in progress
    loading = true;
    error = null;

    try {
      const params = buildQueryParams(1);

      let response: any;

      // If a collection is selected, fetch files from that collection
      if (selectedCollectionId !== null) {
        response = await axiosInstance.get(`/collections/${selectedCollectionId}/media`, { params });
        // Collections endpoint might not have pagination yet, handle both formats
        const newFiles = response.data.items || response.data;
        files = newFiles;
        galleryStore.setFiles(newFiles);

        // Update pagination metadata if available
        if (response.data.items) {
          galleryStore.appendFiles(newFiles, {
            page: response.data.page || 1,
            pageSize: response.data.page_size || newFiles.length,
            total: response.data.total || newFiles.length,
            totalPages: response.data.total_pages || 1,
            hasMore: response.data.has_more || false,
          });
        }
      } else {
        response = await axiosInstance.get('/files', { params });
        const data = response.data;

        // Handle both paginated response (object with items) and flat list (legacy/fallback)
        if (data && data.items) {
          files = data.items;
          galleryStore.setFiles(data.items);
          galleryStore.appendFiles(data.items, {
            page: data.page,
            pageSize: data.page_size,
            total: data.total,
            totalPages: data.total_pages,
            hasMore: data.has_more,
          });
        } else if (Array.isArray(data)) {
          // Fallback for non-paginated response
          files = data;
          galleryStore.setFiles(data);
          galleryStore.appendFiles(data, {
            page: 1,
            pageSize: data.length,
            total: data.length,
            totalPages: 1,
            hasMore: false,
          });
        } else {
          files = [];
          galleryStore.setFiles([]);
        }
      }

      updateFileMap();

      // Note: scroll position restoration for back-nav is handled in onMount
      // before fetchFiles is called, using the cached files from the store.

    } catch (err: any) {
      console.error('Error fetching files:', err);
      if (err?.code === 'ECONNABORTED') {
        error = $t('gallery.queryTimeout');
      } else {
        error = $t('gallery.loadFilesFailed');
      }
    } finally {
      loading = false;
    }
  }

  // Load more files for infinite scroll
  async function loadMoreFiles() {
    if (!$hasMoreFiles || $isLoadingMore || loading) return;

    galleryStore.setLoadingMore(true);

    try {
      const nextPage = $galleryState.currentPage + 1;
      const params = buildQueryParams(nextPage);

      let response: any;

      if (selectedCollectionId !== null) {
        response = await axiosInstance.get(`/collections/${selectedCollectionId}/media`, { params });
        const newFiles = (response.data.items || response.data) as MediaFile[];
        // Deduplicate: only add files not already loaded (prevents duplicate key errors)
        const uniqueNew = newFiles.filter((f: MediaFile) => !fileMap.has(f.uuid));
        files = [...files, ...uniqueNew];
        galleryStore.setFiles(files);

        if (response.data.items) {
          galleryStore.appendFiles(newFiles, {
            page: response.data.page || nextPage,
            pageSize: response.data.page_size || newFiles.length,
            total: response.data.total || files.length,
            totalPages: response.data.total_pages || nextPage,
            hasMore: response.data.has_more || false,
          });
        }
      } else {
        response = await axiosInstance.get('/files', { params });
        const data = response.data;

        if (data && data.items) {
          // Deduplicate: only add files not already loaded (prevents duplicate key errors)
          const uniqueItems = data.items.filter((f: MediaFile) => !fileMap.has(f.uuid));
          files = [...files, ...uniqueItems];
          galleryStore.setFiles(files);
          galleryStore.appendFiles(data.items, {
            page: data.page,
            pageSize: data.page_size,
            total: data.total,
            totalPages: data.total_pages,
            hasMore: data.has_more,
          });
        } else if (Array.isArray(data)) {
          // Fallback for non-paginated response
          const uniqueItems = data.filter((f: MediaFile) => !fileMap.has(f.uuid));
          files = [...files, ...uniqueItems];
          galleryStore.setFiles(files);
        }
      }

      updateFileMap();
    } catch (err) {
      console.error('Error loading more:', err);
      toastStore.error($t('gallery.loadMoreError'));
    } finally {
      galleryStore.setLoadingMore(false);
    }
  }

  // Update file map for efficient lookups
  function updateFileMap() {
    fileMap.clear();
    files.forEach(file => {
      fileMap.set(file.uuid, file);
    });
  }

  // Smooth file list updates without full re-render
  function updateFilesSmooth(newFiles: MediaFile[]) {
    const newFileMap = new Map<string, MediaFile>();
    newFiles.forEach(file => newFileMap.set(file.uuid, file));

    // Identify truly new files (not in current list)
    const existingIds = new Set(files.map(f => f.uuid));
    const newIds = newFiles.map(f => f.uuid).filter(uuid => !existingIds.has(uuid));

    // Only mark files as new if this is not an initial load
    // (Don't highlight on page navigation, only on actual new uploads)
    if (files.length > 0 && newIds.length > 0) {
      // Mark new files for entrance animation
      newIds.forEach(uuid => pendingNewFiles.add(uuid));
      pendingNewFiles = new Set(pendingNewFiles);

      // Clear new file markers after animation
      setTimeout(() => {
        newIds.forEach(uuid => pendingNewFiles.delete(uuid));
        pendingNewFiles = new Set(pendingNewFiles);
      }, 600);
    }

    // Update the files array
    files = newFiles;
    galleryStore.setFiles(newFiles);
    updateFileMap();
  }

  // Add a single new file smoothly to the top
  function addNewFileSmooth(newFile: MediaFile) {
    // Check if file already exists
    if (fileMap.has(newFile.uuid)) {
      // Update existing file
      files = files.map(f => f.uuid === newFile.uuid ? newFile : f);
      fileMap.set(newFile.uuid, newFile);
      return;
    }

    // Only add to top if on first page
    if ($galleryState.currentPage === 1) {
      // Add new file to the beginning
      pendingNewFiles.add(newFile.uuid);
      pendingNewFiles = new Set(pendingNewFiles);
      files = [newFile, ...files];
      fileMap.set(newFile.uuid, newFile);
      galleryStore.setFiles(files);

      // Increment total count
      galleryStore.appendFiles([], {
        page: $galleryState.currentPage,
        pageSize: $galleryState.pageSize,
        total: $galleryState.totalFiles + 1,
        totalPages: $galleryState.totalPages,
        hasMore: $galleryState.hasMoreFiles,
      });

      // Clear new file marker after animation
      setTimeout(() => {
        pendingNewFiles.delete(newFile.uuid);
        pendingNewFiles = new Set(pendingNewFiles);
      }, 600);
    } else {
      // Show toast notification instead
      toastStore.info($t('gallery.newFileAvailable'));

      // Increment total count only
      galleryStore.appendFiles([], {
        page: $galleryState.currentPage,
        pageSize: $galleryState.pageSize,
        total: $galleryState.totalFiles + 1,
        totalPages: Math.ceil(($galleryState.totalFiles + 1) / $galleryState.pageSize),
        hasMore: true, // New file means there might be more
      });
    }
  }

  // Throttled refresh function to prevent spam
  function throttledRefresh(key: string, delay: number = 500) {
    if (refreshTimeouts.has(key)) {
      clearTimeout(refreshTimeouts.get(key) as any);
    }

    const timeoutId = setTimeout(() => {
      fetchFiles(false, true); // Skip animation for throttled refreshes
      refreshTimeouts.delete(key);
    }, delay);

    refreshTimeouts.set(key, timeoutId as any);
  }

  // Remove files smoothly with animation
  function removeFilesSmooth(fileIds: string[]) {
    // Mark files for deletion animation
    const actualDeletions = fileIds.filter(id => fileMap.has(id));
    actualDeletions.forEach(id => {
      pendingDeletions.add(id);
    });
    pendingDeletions = new Set(pendingDeletions);

    // Wait for exit animation to complete, then remove from array
    setTimeout(() => {
      files = files.filter(file => !fileIds.includes(file.uuid));
      fileIds.forEach(id => {
        fileMap.delete(id);
        pendingDeletions.delete(id);
      });
      pendingDeletions = new Set(pendingDeletions);
      galleryStore.setFiles(files);

      // Decrement total count
      if (actualDeletions.length > 0) {
        const newTotal = Math.max(0, $galleryState.totalFiles - actualDeletions.length);
        galleryStore.appendFiles([], {
          page: $galleryState.currentPage,
          pageSize: $galleryState.pageSize,
          total: newTotal,
          totalPages: Math.ceil(newTotal / $galleryState.pageSize),
          hasMore: $galleryState.currentPage < Math.ceil(newTotal / $galleryState.pageSize),
        });
      }
    }, 250); // Match fade out animation duration
  }

  // Remove single file smoothly
  function removeFileSmooth(fileId: string) {
    removeFilesSmooth([fileId]);
  }

  // Handle filter changes
  function applyFilters(event: FilterEvent) {
    const { search, tags, speaker, collectionId, dates, durationRange: duration, fileSizeRange: fileSize, fileTypes, statuses, ownership } = event.detail;

    searchQuery = search;
    selectedTags = tags;
    selectedSpeakers = speaker;
    selectedCollectionId = collectionId;
    dateRange = dates;

    // Handle advanced filters if provided
    if (duration !== undefined) durationRange = duration;
    if (fileSize !== undefined) fileSizeRange = fileSize;
    if (fileTypes !== undefined) selectedFileTypes = fileTypes;
    if (statuses !== undefined) selectedStatuses = statuses;
    if (ownership !== undefined) ownershipFilter = ownership;

    fetchFiles();
  }

  // Handle sort change
  function handleSortChange(event: CustomEvent<{ sortBy: string; sortOrder: 'asc' | 'desc' }>) {
    sortBy = event.detail.sortBy;
    sortOrder = event.detail.sortOrder;
    fetchFiles();
  }



  // Toggle file selection
  function toggleFileSelection(fileId: string, event: Event) {
    if (event) {
      event.stopPropagation();
      event.preventDefault();
    }

    galleryStore.toggleFileSelection(fileId);
  }

  // Select all files
  function selectAllFiles() {
    galleryStore.selectAllFiles();
  }

  // Clear selection
  function clearSelection() {
    galleryStore.clearSelection();
  }

  // Toggle filter sidebar for mobile
  function toggleFilters() {
    galleryStore.toggleFilters();
  }

  // Toggle upload modal
  function toggleUploadModal() {
    showUploadModal = !showUploadModal;
  }


  // Reset all filters
  function resetFilters() {
    searchQuery = '';
    selectedTags = [];
    selectedSpeakers = [];
    selectedCollectionId = null;
    dateRange = { from: null, to: null };
    durationRange = { min: null, max: null };
    fileSizeRange = { min: null, max: null };
    selectedFileTypes = [];
    selectedStatuses = [];
    ownershipFilter = 'all';
    sortBy = 'upload_time';
    sortOrder = 'desc';

    // Clear persisted filter state in the store
    galleryStore.resetFilters();

    // Skip animation when resetting filters to avoid highlighting previously filtered items
    fetchFiles(false, true);
  }

  // Delete selected files with enhanced error handling
  async function deleteSelectedFiles() {
    if (selectedFiles.size === 0) return;

    showConfirmation(
      $t('gallery.deleteConfirmTitle'),
      $t('gallery.deleteConfirmMessage', { count: selectedFiles.size }),
      () => executeDeleteSelectedFiles()
    );
  }

  /**
   * Execute delete selected files after confirmation
   */
  async function executeDeleteSelectedFiles() {
    try {
      // Don't show loading state - use smooth animations instead

      // Use bulk action API for better error handling
      const response = await axiosInstance.post('/files/management/bulk-action', {
        file_uuids: Array.from(selectedFiles),  // Use file_uuids (UUIDs)
        action: 'delete',
        force: false
      });

      const results = response.data;
      const successful = results.filter((r: any) => r.success);
      const failed = results.filter((r: any) => !r.success);

      // Handle conflicts (files that can't be deleted)
      const conflicts = failed.filter((r: any) => r.error === 'HTTP_ERROR' && r.message.includes('FILE_NOT_SAFE_TO_DELETE'));

      if (conflicts.length > 0) {
        const conflictFileIds = conflicts.map((c: any) => c.file_uuid);  // Use file_uuid from response
        showConfirmation(
          $t('gallery.forceDeleteTitle'),
          $t('gallery.forceDeleteMessage', { count: conflicts.length }),
          () => handleForceDelete(conflictFileIds)
        );
        return; // Exit early to wait for user confirmation
      }

      // Report on regular deletion results
      if (successful.length > 0) {
        toastStore.success($t('gallery.deleteSuccess', { count: successful.length }));
        // Remove successfully deleted files smoothly
        const successfulIds = successful.map((r: any) => r.file_uuid);  // Use file_uuid from response
        removeFilesSmooth(successfulIds);
      }

      if (failed.length > 0) {
        toastStore.error($t('gallery.deleteFailed', { count: failed.length }));
      }

      clearSelection();

    } catch (err: any) {
      console.error('Error deleting files:', err);
      const errorMessage = err.response?.data?.detail || $t('gallery.deleteError');
      toastStore.error(errorMessage);
    } finally {
      // No need to set loading = false since we didn't set it to true
    }
  }

  // Bulk reprocess selected files — opens SelectiveReprocessModal in bulk mode
  function bulkReprocess() {
    const selected = $galleryState.selectedFiles;
    if (selected.size === 0) return;

    const reprocessable = files.filter(f => selected.has(f.uuid) && ['completed', 'error', 'cancelled'].includes(f.status));
    if (reprocessable.length === 0) {
      toastStore.warning($t('gallery.bulk.noCompletedFiles'));
      return;
    }

    bulkReprocessFiles = reprocessable;
    showBulkReprocessModal = true;
  }

  function handleBulkReprocessComplete() {
    showBulkReprocessModal = false;
    bulkReprocessFiles = [];
    bulkReprocessing = false;
    // Keep selection — user may want to perform additional actions
  }

  // Bulk summarize selected files
  async function bulkSummarize() {
    const selected = $galleryState.selectedFiles;
    if (selected.size === 0) return;

    const completed = files.filter(f => selected.has(f.uuid) && f.status === 'completed');
    if (completed.length === 0) {
      toastStore.warning($t('gallery.bulk.noCompletedFiles'));
      return;
    }

    try {
      const response = await axiosInstance.post('/files/management/bulk-action', {
        file_uuids: completed.map(f => f.uuid),
        action: 'summarize'
      });
      const results = response.data;
      const successful = results.filter((r: any) => r.success);
      const failed = results.filter((r: any) => !r.success);

      // Check if LLM is not configured
      const llmNotAvailable = failed.some((r: any) => r.error === 'LLM_NOT_AVAILABLE');
      if (llmNotAvailable) {
        toastStore.error($t('gallery.bulk.llmNotConfigured'));
        return;
      }

      if (successful.length > 0) {
        toastStore.success($t('gallery.bulk.summarizeStarted', { count: successful.length }));
      }
      if (failed.length > 0 && !llmNotAvailable) {
        toastStore.error($t('gallery.bulk.summarizeFailed', { count: failed.length }));
      }
    } catch (err) {
      console.error('Bulk summarize error:', err);
      toastStore.error($t('gallery.bulk.summarizeFailed', { count: completed.length }));
    }
  }

  // Bulk retry failed files
  async function bulkRetryFailed() {
    const selected = $galleryState.selectedFiles;
    if (selected.size === 0) return;

    const failedFiles = files.filter(f => selected.has(f.uuid) && f.status === 'error');
    if (failedFiles.length === 0) {
      toastStore.warning($t('gallery.bulk.noFailedFiles'));
      return;
    }

    try {
      const response = await axiosInstance.post('/files/management/bulk-action', {
        file_uuids: failedFiles.map(f => f.uuid),
        action: 'retry'
      });
      const results = response.data;
      const successful = results.filter((r: any) => r.success);
      const failed = results.filter((r: any) => !r.success);

      if (successful.length > 0) {
        toastStore.success($t('gallery.bulk.retryStarted', { count: successful.length }));
      }
      if (failed.length > 0) {
        toastStore.error($t('gallery.bulk.retryFailedError', { count: failed.length }));
      }
    } catch (err) {
      console.error('Bulk retry error:', err);
      toastStore.error($t('gallery.bulk.retryFailedError', { count: failedFiles.length }));
    }
  }

  // EventSource for the in-flight bulk subtitle export (async + presigned ZIP)
  let bulkExportStream: EventSource | null = null;

  function triggerAnchorDownload(href: string, filename: string) {
    const a = document.createElement('a');
    a.href = href;
    if (filename) a.download = filename;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // Bulk export selected files as a single ZIP — built on the download worker and
  // delivered as a presigned URL over SSE (no archive bytes proxied through the API).
  async function bulkExport(format: string) {
    const selected = $galleryState.selectedFiles;
    if (selected.size === 0) return;

    const completed = files.filter(f => selected.has(f.uuid) && f.status === 'completed');
    if (completed.length === 0) {
      toastStore.warning($t('gallery.bulk.noCompletedFiles'));
      return;
    }

    const ext = format === 'webvtt' ? 'vtt' : format;
    const total = completed.length;

    toastStore.info($t('gallery.bulk.exportStarted', { count: total, format: ext.toUpperCase() }));

    try {
      const { data } = await axiosInstance.post('/files/bulk-export/prepare', {
        file_uuids: completed.map(f => f.uuid),
        subtitle_format: format,
        include_speakers: true,
      });

      bulkExportStream?.close();
      const es = new EventSource(`/api/files/bulk-export-stream?job=${data.job_id}`);
      bulkExportStream = es;

      es.addEventListener('ready', (e: MessageEvent) => {
        const d = JSON.parse(e.data);
        triggerAnchorDownload(d.url, d.filename ?? `transcripts_${format}.zip`);
        toastStore.success($t('gallery.bulk.exportComplete', {
          count: d.exported ?? total,
          format: ext.toUpperCase(),
        }));
        if (d.skipped > 0) {
          toastStore.warning($t('gallery.bulk.exportSkipped', { count: d.skipped }));
        }
        es.close();
        bulkExportStream = null;
      });

      // The browser fires 'error' for BOTH a server `event: error` (has e.data) and
      // transient connection drops (no e.data). Only fail on a real payload or a closed stream.
      es.addEventListener('error', (e: MessageEvent) => {
        if (e.data || es.readyState === EventSource.CLOSED) {
          toastStore.error($t('gallery.bulk.exportFailed', { count: total }));
          es.close();
          bulkExportStream = null;
        }
      });
    } catch (err) {
      console.error('Bulk export failed to start:', err);
      toastStore.error($t('gallery.bulk.exportFailed', { count: total }));
    }
    // Don't clear selection — user may want to export in multiple formats
  }

  // Bulk speaker identification
  async function bulkSpeakerId() {
    const selected = $galleryState.selectedFiles;
    if (selected.size === 0) return;

    const completed = files.filter(f => selected.has(f.uuid) && f.status === 'completed');
    if (completed.length === 0) {
      toastStore.warning($t('gallery.bulk.noCompletedFiles'));
      return;
    }

    try {
      let successCount = 0;
      let failCount = 0;

      for (const file of completed) {
        try {
          await axiosInstance.post(`/files/${file.uuid}/identify-speakers`);
          successCount++;
        } catch {
          failCount++;
        }
      }

      if (successCount > 0) {
        toastStore.success($t('gallery.bulk.speakerIdStarted', { count: successCount }));
      }
      if (failCount > 0) {
        toastStore.error($t('gallery.bulk.speakerIdFailed', { count: failCount }));
      }
    } catch (err) {
      console.error('Bulk speaker ID error:', err);
      toastStore.error($t('gallery.bulk.speakerIdFailed', { count: completed.length }));
    }
  }

  // Bulk cancel processing
  async function bulkCancelProcessing() {
    const selected = $galleryState.selectedFiles;
    if (selected.size === 0) return;

    const processing = files.filter(f => selected.has(f.uuid) && f.status === 'processing');
    if (processing.length === 0) {
      toastStore.warning($t('gallery.bulk.noProcessingFiles'));
      return;
    }

    try {
      const response = await axiosInstance.post('/files/management/bulk-action', {
        file_uuids: processing.map(f => f.uuid),
        action: 'cancel'
      });
      const results = response.data;
      const successful = results.filter((r: any) => r.success);
      const failed = results.filter((r: any) => !r.success);

      if (successful.length > 0) {
        toastStore.success($t('gallery.bulk.cancelStarted', { count: successful.length }));
      }
      if (failed.length > 0) {
        toastStore.error($t('gallery.bulk.cancelFailed', { count: failed.length }));
      }
    } catch (err) {
      console.error('Bulk cancel error:', err);
      toastStore.error($t('gallery.bulk.cancelFailed', { count: processing.length }));
    }
  }

  // File upload completed handler
  function handleUploadComplete(event: CustomEvent) {
    // For URL uploads, don't immediately refresh - let WebSocket notifications handle it
    // This prevents showing loading state while YouTube/URL processing happens in background
    const isUrl = event.detail?.isUrl || false;
    const isDuplicate = event.detail?.isDuplicate || false;

    if (!isUrl) {
      // For file uploads, do a smooth refresh since files are ready to display
      throttledRefresh('upload-complete', 300);
    }
    // For URL uploads, the gallery will refresh when WebSocket notifications arrive

    // Only close the modal if not a duplicate file
    // For duplicates, we want to keep the modal open to show the notification
    if (!isDuplicate) {
      showUploadModal = false;
    }
  }



  // Enhanced error notification using backend categorization
  function showEnhancedErrorNotification(file: MediaFile) {
    // Use backend-provided error categorization
    if (file.error_category && file.error_suggestions) {
      const suggestions = file.error_suggestions.map(s => `• ${s}`).join('\n');
      toastStore.error(
        $t('gallery.processingFailedWithSuggestions', {
          message: file.user_message || $t('gallery.processingFailed', { filename: file.title || file.filename }),
          suggestions: suggestions
        }),
        file.error_category === 'file_quality' ? 10000 : 8000
      );
    } else {
      // Minimal fallback for unexpected cases
      toastStore.error($t('gallery.processingFailed', { filename: file.title || file.filename }));
    }
  }

  // Subscribe to WebSocket file status updates to update file status in real-time
  // Track processed notifications to avoid duplicate processing
  let processedNotificationIds = new Set<string>();
  let previousActiveUploadCount = 0;
  let pendingNotifications: any[] = [];
  let wsFlushTimer: ReturnType<typeof setTimeout> | null = null;

  // Periodic cleanup to prevent processedNotificationIds from growing too large
  const notificationCleanupInterval = setInterval(() => {
    if (processedNotificationIds.size > 1000) {
      processedNotificationIds.clear();
    }
  }, 300000); // Clean up every 5 minutes

  // Flush pending notifications in a single batched update
  function flushPendingNotifications() {
    wsFlushTimer = null;
    if (pendingNotifications.length === 0) return;
    const batch = pendingNotifications;
    pendingNotifications = [];
    processNotificationBatch(batch);
  }

  function processNotificationBatch(unprocessedNotifications: any[]) {
    // Process all notifications, collecting updates into fileMap without
    // triggering Svelte reactivity per-notification (prevents freeze with bulk processing)
    let filesUpdated = false;
    let needsRefreshIds: string[] = [];

    unprocessedNotifications.forEach(notification => {
      if (notification.type === 'file_deleted' && notification.data?.file_id) {
        const fileId = String(notification.data.file_id);
        removeFileSmooth(fileId);
      }
      else if (notification.type === 'transcription_status' && notification.data?.file_id) {
        const fileId = String(notification.data.file_id);
        const status = notification.data.status;
        const existing = fileMap.get(fileId);

        if (existing) {
          const updatedFile = {
            ...existing,
            status: status,
            display_status: status === 'processing' ? $t('common.processing') :
                           status === 'completed' ? $t('common.completed') :
                           status === 'pending' ? $t('common.pending') :
                           status === 'error' ? $t('common.error') : status
          };
          fileMap.set(fileId, updatedFile);
          filesUpdated = true;
        }

        if (status === 'error') {
          needsRefreshIds.push(fileId);
        }
      }
      else if (notification.type === 'cache_invalidate') {
        // Push-based cache invalidation from backend
        const scope = notification.data?.scope;
        if (scope === 'files' || scope === 'all') {
          apiCache.invalidate('files:');
          throttledRefresh('cache-invalidate', 500);
        }
      }
      else if (notification.type === 'file_upload' || notification.type === 'file_created') {
        if (notification.data?.file) {
          addNewFileSmooth(notification.data.file);
        } else {
          throttledRefresh('new-file', 500);
        }
      }
      else if (notification.type === 'file_updated' && notification.data?.file_id) {
        const fileId = String(notification.data.file_id);
        const existing = fileMap.get(fileId);

        if (existing) {
          let updatedFile;
          if (notification.data.file) {
            updatedFile = { ...existing, ...notification.data.file };
          } else {
            const newStatus = notification.data.status || existing.status;
            updatedFile = {
              ...existing,
              status: newStatus,
              display_status: notification.data.display_status || existing.display_status,
              thumbnail_url: notification.data.thumbnail_url || existing.thumbnail_url,
            };
          }
          fileMap.set(fileId, updatedFile);
          filesUpdated = true;
        }
        // File not in currently loaded pages — ignore silently.
      }
    });

    // Single batched update: rebuild files array from fileMap once
    if (filesUpdated) {
      files = files.map(f => fileMap.get(f.uuid) || f);
      galleryStore.setFiles(files);
    }

    // Schedule refreshes for error files (batched)
    if (needsRefreshIds.length > 0) {
      throttledRefresh('error-batch', 1000);
    }
  }

  function setupWebSocketUpdates() {
    unsubscribeFileStatus = websocketStore.subscribe(($ws) => {
      if ($ws.notifications.length > 0) {
        const unprocessedNotifications = $ws.notifications.filter(notification =>
          !processedNotificationIds.has(notification.id)
        );

        if (unprocessedNotifications.length > 0) {
          unprocessedNotifications.forEach(notification => {
            processedNotificationIds.add(notification.id);
          });

          // Queue notifications and debounce processing to batch updates
          // This prevents per-notification re-renders during bulk processing
          pendingNotifications.push(...unprocessedNotifications);
          if (wsFlushTimer) clearTimeout(wsFlushTimer);
          wsFlushTimer = setTimeout(flushPendingNotifications, 250);
        }
      }
    });

    // Also subscribe to upload store to detect when uploads finish
    const unsubscribeUploads = uploadsStore.subscribe(($uploads) => {
      const currentActiveUploadCount = $uploads.uploads.filter(u =>
        ['uploading', 'processing', 'preparing'].includes(u.status)
      ).length;

      // If active upload count decreased to 0, do a smooth refresh
      if (previousActiveUploadCount > 0 && currentActiveUploadCount === 0) {
        throttledRefresh('uploads-complete', 800); // Slightly longer delay for upload completion
      }

      previousActiveUploadCount = currentActiveUploadCount;
    });

    // Return cleanup function for uploads subscription
    const originalUnsubscribe = unsubscribeFileStatus;
    unsubscribeFileStatus = () => {
      if (originalUnsubscribe) originalUnsubscribe();
      if (unsubscribeUploads) unsubscribeUploads();
      // Clear processed notification IDs to prevent memory leaks
      processedNotificationIds.clear();
    };
  }

  onMount(() => {
    // DEFENSE-IN-DEPTH: refuse to render/fetch if unauthenticated.
    // The layout is the primary guard and should prevent this component
    // from mounting at all, but if it somehow does, block all API calls
    // and WebSocket subscriptions to avoid data leaks.
    if (!get(isAuthenticated)) {
      return;
    }

    // Update document title
    document.title = $t('gallery.pageTitle');

    // Collapse filter sidebar on mobile to avoid covering the content
    if (window.innerWidth <= 768 && $galleryState.showFilters) {
      galleryStore.toggleFilters();
    }

    // Setup WebSocket subscription for real-time updates
    setupWebSocketUpdates();

    // Listen for events to open Add Media modal
    const handleOpenModalEvent = (event: CustomEvent) => {
      showUploadModal = true;
      // Dispatch a separate event for the FileUploader after the modal is shown
      setTimeout(() => {
        if (event.detail?.activeTab) {
          window.dispatchEvent(new CustomEvent('setFileUploaderTab', {
            detail: { activeTab: event.detail.activeTab }
          }));
        }
      }, 50);
    };

    // Listen for direct file upload from recording popup
    const handleUploadRecordedFile = (event: CustomEvent) => {
      if (event.detail?.file) {
        showUploadModal = true;
        // Trigger file upload directly
        setTimeout(() => {
          window.dispatchEvent(new CustomEvent('directFileUpload', {
            detail: { file: event.detail.file }
          }));
        }, 100);
      }
    };

    window.addEventListener('openAddMediaModal', handleOpenModalEvent as EventListener);
    window.addEventListener('uploadRecordedFile', handleUploadRecordedFile as EventListener);

    // On back navigation, restore files from store instead of re-fetching
    const savedScrollTop = $galleryState.scrollTop;
    const savedFiles = $galleryState.files;
    if (savedFiles.length > 0 && savedScrollTop > 0) {
      // Restore cached files so VirtualGrid has data to render
      files = [...savedFiles];
      updateFileMap();
      loading = false;

      // Repeatedly attempt scroll restoration until VirtualGrid has
      // computed its full height and the scroll actually takes effect.
      let attempts = 0;
      const tryRestore = () => {
        if (!scrollableContentEl || attempts > 30) return;
        attempts++;
        scrollableContentEl.scrollTop = savedScrollTop;
        if (Math.abs(scrollableContentEl.scrollTop - savedScrollTop) > 10) {
          requestAnimationFrame(tryRestore);
        }
      };
      tick().then(() => requestAnimationFrame(tryRestore));
    } else {
      fetchFiles(true); // Fresh load
    }

    // Setup infinite scroll observer (will observe sentinel via reactive statement)
    intersectionObserver = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting && $hasMoreFiles && !$isLoadingMore) {
            loadMoreFiles();
          }
        });
      },
      {
        root: null,
        rootMargin: '200px',  // Trigger 200px before bottom
        threshold: 0.1,
      }
    );

    // Return cleanup function
    return () => {
      window.removeEventListener('openAddMediaModal', handleOpenModalEvent as EventListener);
      window.removeEventListener('uploadRecordedFile', handleUploadRecordedFile as EventListener);
      if (intersectionObserver) {
        intersectionObserver.disconnect();
      }
    };
  });

  onDestroy(() => {
    // Scroll position is saved continuously by onGalleryScroll handler.
    // Do NOT read scrollableContentEl.scrollTop here — DOM is already
    // detached and it returns 0, overwriting the saved value.

    // Save filter state for back navigation restoration
    galleryStore.saveFilters({
      searchQuery,
      selectedTags,
      selectedSpeakers,
      selectedCollectionId,
      dateRange,
      durationRange,
      fileSizeRange,
      selectedFileTypes,
      selectedStatuses,
      ownershipFilter,
      sortBy,
      sortOrder,
    });

    // Clean up WebSocket subscription when component is destroyed
    if (unsubscribeFileStatus) {
      unsubscribeFileStatus();
    }

    // Clean up periodic notification cleanup interval
    clearInterval(notificationCleanupInterval);

    // Clean up scroll save rAF
    if (scrollSaveRaf !== null) {
      cancelAnimationFrame(scrollSaveRaf);
    }

    // Clean up any pending refresh timeouts
    refreshTimeouts.forEach((timeoutId) => {
      clearTimeout(timeoutId);
    });
    refreshTimeouts.clear();

    // Close any in-flight bulk export SSE stream
    bulkExportStream?.close();
  });

  // Set up store-based action triggers
  onMount(() => {
    // Subscribe to gallery action triggers (initial values are now handled in store)
    const unsubscribeUpload = galleryStore.onUploadTrigger(() => {
      toggleUploadModal();
    });

    const unsubscribeCollections = galleryStore.onCollectionsTrigger(() => {
      showCollectionsModal = true;
    });

    const unsubscribeAddToCollection = galleryStore.onAddToCollectionTrigger(() => {
      showCollectionsModal = true;
    });

    const unsubscribeDeleteSelected = galleryStore.onDeleteSelectedTrigger(() => {
      deleteSelectedFiles();
    });

    const unsubscribeReprocess = galleryStore.onReprocessTrigger(() => {
      bulkReprocess();
    });

    const unsubscribeSummarize = galleryStore.onSummarizeTrigger(() => {
      bulkSummarize();
    });

    const unsubscribeRetryFailed = galleryStore.onRetryFailedTrigger(() => {
      bulkRetryFailed();
    });

    const unsubscribeExport = galleryStore.onExportTrigger((format) => {
      bulkExport(format);
    });

    const unsubscribeSpeakerId = galleryStore.onSpeakerIdTrigger(() => {
      bulkSpeakerId();
    });

    const unsubscribeCancelProcessing = galleryStore.onCancelProcessingTrigger(() => {
      bulkCancelProcessing();
    });

    // Cleanup subscriptions
    return () => {
      unsubscribeUpload();
      unsubscribeCollections();
      unsubscribeAddToCollection();
      unsubscribeDeleteSelected();
      unsubscribeReprocess();
      unsubscribeSummarize();
      unsubscribeRetryFailed();
      unsubscribeExport();
      unsubscribeSpeakerId();
      unsubscribeCancelProcessing();
    };
  });
</script>

<!-- Main container with fixed height -->
<div class="media-library-container">
  {#if activeTab === 'gallery'}
    <div class="gallery-tab-wrapper">
      <!-- Mobile filter overlay backdrop -->
      {#if showFilters}
        <!-- svelte-ignore a11y-click-events-have-key-events -->
        <!-- svelte-ignore a11y-no-static-element-interactions -->
        <div
          class="filter-overlay-backdrop"
          on:click={toggleFilters}
          transition:fade={{ duration: 200 }}
        ></div>
      {/if}

      <!-- Left Sidebar: Filters (Sticky) -->
      <div class="filter-sidebar {showFilters ? 'show' : ''}">
        <!-- Filters Toggle Button (always visible) -->
        <div class="filter-toggle-container">
          <button
            class="filter-toggle-btn {showFilters ? 'expanded' : 'collapsed'}"
            on:click={toggleFilters}
            title={showFilters ? $t('gallery.hideFiltersPanel') : $t('gallery.showFiltersPanel')}
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line>
              <line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line>
              <line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line>
              <line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line>
              <line x1="17" y1="16" x2="23" y2="16"></line>
            </svg>
          </button>
        </div>

        <!-- Filter Content (hidden when collapsed) -->
        {#if showFilters}
          <div class="filter-content">
            <FilterSidebar
              bind:this={filterSidebarRef}
              searchQuery={searchQuery}
              selectedTags={selectedTags}
              selectedSpeakers={selectedSpeakers}
              selectedCollectionId={selectedCollectionId}
              dateRange={dateRange}
              durationRange={durationRange}
              fileSizeRange={fileSizeRange}
              selectedFileTypes={selectedFileTypes}
              selectedStatuses={selectedStatuses}
              ownershipFilter={ownershipFilter}
              on:filter={applyFilters}
              on:reset={resetFilters}
            />
          </div>
        {/if}
      </div>

      <!-- Right Content: Scrollable Media Grid -->
      <div class="content-area">
        <div class="scrollable-content" bind:this={scrollableContentEl} on:scroll={onGalleryScroll}>
      <!-- Gallery Header (sticky) - always visible for action buttons -->
      <div class="gallery-header">
        <div class="gallery-header-left">
          <!-- Mobile filter toggle button (visible only on mobile) -->
          <button
            class="mobile-filter-toggle"
            on:click={toggleFilters}
            title={showFilters ? $t('gallery.hideFiltersPanel') : $t('gallery.showFiltersPanel')}
            aria-label={showFilters ? $t('gallery.hideFiltersPanel') : $t('gallery.showFiltersPanel')}
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line>
              <line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line>
              <line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line>
              <line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line>
              <line x1="17" y1="16" x2="23" y2="16"></line>
            </svg>
          </button>
          <GalleryActionButtons {files} />
        </div>
        {#if files.length > 0}
          <div class="gallery-header-right">
            <GallerySortDropdown
              {sortBy}
              {sortOrder}
              on:change={handleSortChange}
            />
            <GalleryViewToggle />
            <GalleryCountChip loading={loading} filesLoaded={files.length} />
          </div>
        {/if}
      </div>

      {#if loading}
        <CardGridSkeleton variant="media" count={12} />
      {:else if error}
        <div class="error-state">
          <p>{$t('gallery.connectionError')}</p>
          <button
            class="retry-button"
            on:click={() => fetchFiles()}
            title={$t('gallery.retryTooltip')}
          >{$t('gallery.retry')}</button>
        </div>
      {:else if files.length === 0}
        <EmptyState
          title={selectedCollectionId ? $t('gallery.noFilesInCollection') : $t('gallery.libraryEmpty')}
          description={$t('gallery.uploadFirstFile')}
        />
      {:else}
        {#if $galleryViewMode === 'list'}
          <!-- List View (Virtual Scrolling) -->
          <VirtualList
            items={files}
            scrollContainer={scrollableContentEl}
            {isSelecting}
            {selectedFiles}
            {pendingNewFiles}
            {pendingDeletions}
            on:errorclick={(e) => showEnhancedErrorNotification(e.detail)}
          />
        {:else}
          <!-- Grid View (Virtual Scrolling) -->
          <VirtualGrid
            items={files}
            scrollContainer={scrollableContentEl}
            {isSelecting}
            {selectedFiles}
            {pendingNewFiles}
            {pendingDeletions}
            on:errorclick={(e) => showEnhancedErrorNotification(e.detail)}
          />
        {/if}

        <!-- Infinite scroll sentinel -->
        <div bind:this={infiniteScrollSentinel} class="scroll-sentinel"></div>

        <!-- Loading indicator -->
        {#if $isLoadingMore}
          <div class="loading-more" transition:fade={{ duration: 200 }}>
            <Spinner size="large" />
            <p>{$t('gallery.loadingMore')}</p>
          </div>
        {/if}

        <!-- End of Results Indicator -->
        {#if !$hasMoreFiles && files.length > 0 && !loading}
          <div class="end-of-results" in:fade={{ duration: 300 }}>
            <div class="end-indicator-line"></div>
            <div class="end-indicator-content">
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <polyline points="20 6 9 17 4 12"></polyline>
              </svg>
              <span>{$t('gallery.allFilesLoaded')}</span>
            </div>
            <div class="end-indicator-line"></div>
          </div>
        {/if}
      {/if}
    </div>
  </div>
    </div>
  {:else if activeTab === 'status'}
    <div class="status-tab-content">
      <UserFileStatus />
    </div>
  {/if}
</div>

<!-- Upload Modal -->
{#if showUploadModal}
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <!--
    Upload modal backdrop.
    Backdrop click is intentionally NOT bound to close - the user has in-progress
    upload state (file, tags, collections, speaker settings, etc.) and a stray
    click outside should not destroy their work. Only the X button and Escape
    key close the modal explicitly.
  -->
  <div
    class="modal-backdrop"
    role="presentation"
    transition:fade={{ duration: 400 }}
    on:wheel|preventDefault|self
    on:touchmove|preventDefault|self
    on:keydown={(e) => e.key === 'Escape' && toggleUploadModal()}
  >
    <!-- The actual modal dialog -->
    <!-- svelte-ignore a11y-click-events-have-key-events -->
    <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
    <!-- svelte-ignore a11y-no-static-element-interactions -->
    <!-- svelte-ignore a11y_interactive_supports_focus -->
    <div
      class="modal-container"
      role="dialog"
      aria-labelledby="upload-modal-title"
      aria-modal="true"
      tabindex="-1"
      transition:scale={{ duration: 350, start: 0.9 }}
      on:click|stopPropagation
      on:keydown|stopPropagation>
      <div class="modal-content">
        <div class="modal-header">
          <h2 id="upload-modal-title">{$t('nav.addMedia')}</h2>
          <button
            class="modal-close"
            on:click={toggleUploadModal}
            aria-label={$t('gallery.closeUploadDialog')}
            title={$t('gallery.closeUploadDialog')}
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        </div>
        <div class="modal-body">
          <FileUploader on:uploadComplete={handleUploadComplete} />
        </div>
      </div>
    </div>
  </div>
{/if}

<!-- Collections Modal -->
{#if showCollectionsModal}
  <div
    class="modal-backdrop"
    role="presentation"
    transition:fade={{ duration: 400 }}
    on:click={() => showCollectionsModal = false}
    on:wheel|preventDefault|self
    on:touchmove|preventDefault|self
    on:keydown={(e) => e.key === 'Escape' && (() => showCollectionsModal = false)()}
  >
    <!-- svelte-ignore a11y-click-events-have-key-events -->
    <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
    <!-- svelte-ignore a11y-no-static-element-interactions -->
    <!-- svelte-ignore a11y_interactive_supports_focus -->
    <div
      class="modal-container"
      role="dialog"
      aria-modal="true"
      transition:scale={{ duration: 350, start: 0.9 }}
      on:click|stopPropagation
      on:keydown|stopPropagation
    >
      <div class="modal-content">
        <div class="modal-header">
          <h2>{$t('gallery.manageCollections')}</h2>
          <button
            class="modal-close"
            on:click={() => showCollectionsModal = false}
            aria-label={$t('gallery.closeCollectionsDialog')}
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        </div>
        <div class="modal-body">
          <CollectionsPanel
            selectedMediaIds={Array.from(selectedFiles)}
            viewMode={selectedFiles.size > 0 ? 'add' : 'manage'}
            onCollectionSelect={() => {
              // Only close modal if we're in 'add' mode and actually adding files
              if (selectedFiles.size > 0) {
                showCollectionsModal = false;
                // Keep selection — user may want to add to multiple collections or perform other actions
              }
              // In manage mode, keep the modal open for multiple collections

              // Refresh collections in filter
              if (filterSidebarRef && filterSidebarRef.refreshCollections) {
                filterSidebarRef.refreshCollections();
              }
              // Refresh files if we're filtering by collection
              if (selectedCollectionId !== null) {
                fetchFiles();
              }
            }}
          />
        </div>
      </div>
    </div>
  </div>
{/if}

<!-- Confirmation Modal -->
<ConfirmationModal
  bind:isOpen={showConfirmModal}
  title={confirmModalTitle}
  message={confirmModalMessage}
  confirmText={$t('gallery.confirmButton')}
  cancelText={$t('gallery.cancelButton')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={handleConfirmModalConfirm}
  on:cancel={handleConfirmModalCancel}
  on:close={handleConfirmModalCancel}
/>

<!-- Bulk Selective Reprocess Modal -->
<SelectiveReprocessModal
  bind:showModal={showBulkReprocessModal}
  bind:reprocessing={bulkReprocessing}
  bulkMode={true}
  bulkFiles={bulkReprocessFiles}
  on:reprocess={handleBulkReprocessComplete}
  on:close={() => { showBulkReprocessModal = false; bulkReprocessFiles = []; }}
/>

<style>
  /* Main Container - Fixed Height Layout */
  .media-library-container {
    height: calc(100vh - var(--content-top, 60px)); /* Full viewport minus navbar + safe-area */
    height: calc(100dvh - var(--content-top, 60px));
    display: flex;
    overflow: hidden;
    padding-top: 0;
  }

  /* Gallery Tab Wrapper */
  .gallery-tab-wrapper {
    display: flex;
    height: 100%;
    width: 100%;
  }

  .status-tab-content {
    padding: 1rem;
    width: 100%;
  }

  /* Left Sidebar - Sticky Filters */
  .filter-sidebar {
    flex-shrink: 0;
    background-color: var(--surface-color);
    border-right: 1px solid var(--border-color);
    height: 100%;
    display: flex;
    flex-direction: column;
    transition: all 0.3s ease;
  }

  /* Expanded state */
  .filter-sidebar.show {
    width: 320px;
  }

  /* Collapsed state */
  .filter-sidebar:not(.show) {
    width: 50px; /* Just enough for the toggle button */
  }

  .filter-toggle-container {
    padding: 0.5rem 0.5rem 0;
    margin-bottom: 0.5rem;
    flex-shrink: 0;
  }

  .filter-sidebar.show .filter-toggle-container {
    padding: 0.5rem 1rem 0;
  }

  .filter-toggle-btn {
    width: 100%;
    background-color: var(--bg-primary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.6rem 1rem;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    height: 40px;
    white-space: nowrap;
  }

  .filter-toggle-btn:hover {
    background-color: var(--hover-color);
    border-color: var(--primary-color);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
  }

  .filter-toggle-btn:active {
    transform: scale(0.98);
  }

  .filter-toggle-btn svg {
    flex-shrink: 0;
    opacity: 0.8;
  }

  .filter-toggle-btn.collapsed {
    justify-content: center;
    padding: 0.6rem;
    width: auto;
  }

  .filter-content {
    flex: 1;
    overflow-y: auto;
    padding: 0 1rem;
  }

  /* Mobile filter overlay backdrop - hidden on desktop */
  .filter-overlay-backdrop {
    display: none;
    position: fixed;
    top: var(--content-top, 60px);
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.4);
    z-index: 999;
  }

  /* Right Content Area - Scrollable */
  .content-area {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0; /* Allow flex shrinking */
  }

  .scrollable-content {
    flex: 1;
    overflow-y: auto;
    padding: 1rem;
    padding-top: 0; /* Gallery header provides top spacing */
  }

  .error-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 400px;
    text-align: center;
    color: var(--text-secondary);
  }

  .error-state {
    color: var(--error-color);
  }

  .retry-button {
    margin-top: 1rem;
    background-color: var(--primary-color);
    color: white;
    border: none;
    padding: 0.5rem 1rem;
    border-radius: 8px;
    cursor: pointer;
    font-size: 0.875rem;
    font-weight: 500;
    transition: all 0.2s ease;
  }

  .retry-button:hover {
    background-color: var(--primary-hover);
    transform: translateY(-1px);
  }

  .retry-button:active {
    transform: translateY(0);
  }

  /* Modal styles */
  .modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1300;
    overflow: hidden;
    overscroll-behavior: none;
  }

  :global(.dark) .modal-backdrop {
    background: rgba(0, 0, 0, 0.7);
  }

  .modal-container {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    max-width: 90%;
    max-height: 90vh;
    width: 720px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
  }

  :global(.dark) .modal-container {
    background: var(--background-color);
    border-color: var(--border-color);
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.3), 0 10px 10px -5px rgba(0, 0, 0, 0.2);
  }

  .modal-content {
    display: flex;
    flex-direction: column;
    height: 100%;
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.5rem;
    border-bottom: 1px solid var(--border-color);
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
  }

  .modal-close {
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.5rem;
    color: var(--text-secondary);
    transition: all 0.2s ease;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .modal-close:hover {
    color: var(--text-color);
    background: var(--button-hover);
  }

  .modal-body {
    flex: 1;
    padding: 1.5rem;
    overflow-y: auto;
  }

  /* Infinite scroll sentinel - invisible trigger element */
  .scroll-sentinel {
    height: 20px;
    margin-top: 2rem;
    pointer-events: none;
  }

  /* Loading more indicator */
  .loading-more {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 2rem;
    gap: 1rem;
  }

  .loading-more p {
    color: var(--text-secondary);
    font-size: 0.9rem;
    margin: 0;
  }

  /* Narrower filter sidebar on tablet to give more room to content */
  @media (max-width: 1200px) {
    .filter-sidebar.show {
      width: 260px;
    }
  }

  /* Responsive design */
  @media (max-width: 768px) {
    .media-library-container {
      flex-direction: column;
    }

    .filter-sidebar {
      position: fixed;
      top: var(--content-top, 60px);
      left: -100%;
      width: 85%;
      max-width: 320px;
      height: calc(100vh - var(--content-top, 60px));
      height: calc(100dvh - var(--content-top, 60px));
      background: var(--surface-color);
      z-index: 1300;
      transition: left 0.3s ease;
      border-right: 1px solid var(--border-color);
      border-top: 1px solid var(--border-color);
      box-shadow: 4px 0 16px rgba(0, 0, 0, 0.1);
    }

    .filter-sidebar.show {
      left: 0;
    }

    .filter-overlay-backdrop {
      display: block;
    }

    .content-area {
      width: 100%;
    }

    .scrollable-content {
      padding: 1rem;
      padding-top: 0;
      background-color: var(--background-color);
    }

    /* Upload & Collections modals: fullscreen on mobile.
       Raise above navbar (z-index 1200) so close button is reachable. */
    .modal-backdrop {
      align-items: stretch;
      z-index: 1300;
    }

    .modal-container {
      width: 100%;
      max-width: 100% !important;
      max-height: 100%;
      max-height: 100dvh;
      border-radius: 0;
      margin: 0;
    }

    .modal-header {
      padding: 1rem;
      padding-top: calc(1rem + env(safe-area-inset-top, 0px));
    }

    .modal-header h2 {
      font-size: 1.1rem;
    }

    .modal-body {
      padding: 1rem;
    }

    .modal-close {
      min-width: 44px;
      min-height: 44px;
    }
  }



  /* Modal button styling to match app design */
  :global(.modal-delete-button) {
    background-color: #ef4444 !important;
    color: white !important;
    border: none !important;
    padding: 0.6rem 1.2rem !important;
    border-radius: 10px !important;
    font-size: 0.95rem !important;
    font-weight: 500 !important;
    cursor: pointer !important;
    transition: all 0.2s ease !important;
    box-shadow: 0 2px 4px rgba(239, 68, 68, 0.2) !important;
  }

  :global(.modal-delete-button:hover) {
    background-color: #dc2626 !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 4px 8px rgba(239, 68, 68, 0.25) !important;
  }

  :global(.modal-cancel-button:hover) {
    background-color: var(--button-hover) !important;
    color: var(--text-color) !important;
    border-color: var(--border-color) !important;
    transform: scale(1.02) !important;
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1) !important;
  }

  /* Gallery Header with Sort and Count Chips */
  .gallery-header {
    position: sticky;
    top: 0;
    z-index: 10;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    margin-bottom: 0.75rem;
    padding: 0.75rem 1rem;
    background-color: var(--surface-color);
    border-bottom: 1px solid var(--border-color);
    margin-left: -1rem;
    margin-right: -1rem;
  }

  .gallery-header-left {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex: 1 1 auto;
    min-width: 0;
  }

  .gallery-header-right {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  /* Mobile-only filter toggle button in gallery header */
  .mobile-filter-toggle {
    display: none; /* Hidden on desktop */
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    padding: 0;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 0.2s ease;
    flex-shrink: 0;
  }

  .mobile-filter-toggle:hover {
    border-color: var(--primary-color);
    color: var(--text-primary);
    background: var(--hover-color);
  }

  .mobile-filter-toggle svg {
    flex-shrink: 0;
  }

  /* Tablet/iPad: wrap gallery header and right-justify controls */
  @media (max-width: 1200px) and (min-width: 769px) {
    .gallery-header {
      flex-wrap: wrap;
      gap: 0.5rem;
    }

    .gallery-header-left {
      flex: 1 1 100%;
    }

    .gallery-header-right {
      flex: 1 1 auto;
      justify-content: flex-end;
      gap: 0.375rem;
    }
  }

  @media (max-width: 768px) {
    .mobile-filter-toggle {
      display: flex; /* Visible on mobile */
    }

    .gallery-header {
      flex-wrap: wrap;
      gap: 0.5rem;
      margin-left: -1rem;
      margin-right: -1rem;
      padding: 0.5rem 1rem;
      /* Prevent content showing through gap between navbar and toolbar */
      background-color: var(--background-color, var(--surface-color));
      box-shadow: 0 -20px 0 0 var(--background-color, var(--surface-color));
    }

    .gallery-header-left {
      flex: 1 1 auto;
      min-width: 0;
    }

    .gallery-header-right {
      flex: 0 0 auto;
      gap: 0.375rem;
    }
  }

  @media (max-width: 480px) {
    .gallery-header {
      gap: 0.375rem;
      padding-top: 0.5rem;
      padding-bottom: 0.5rem;
    }

    .gallery-header-left {
      flex: 1 1 100%;
    }

    .gallery-header-right {
      flex: 1 1 auto;
      justify-content: flex-end;
    }
  }

  /* End of Results Indicator */
  .end-of-results {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 1rem;
    padding: 3rem 1rem;
  }

  .end-indicator-line {
    flex: 1;
    height: 1px;
    background: linear-gradient(to right, transparent, var(--border-color) 50%, transparent);
    max-width: 200px;
  }

  .end-indicator-content {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 1rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 20px;
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--text-secondary);
  }

  .end-indicator-content svg {
    flex-shrink: 0;
    color: var(--success-color, #10b981);
  }

</style>
