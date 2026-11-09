<script lang="ts">
  /**
   * One dialog size for the gallery's three big modals (upload, tag manager,
   * collections). The upload wizard's six steps have very different natural
   * heights, so a content-sized dialog resized and flickered on every Next/Back
   * (#739); the tag and collections dialogs had the same content-driven jump.
   * Pinning it keeps headers, content and footer controls stationary, and keeps
   * the three reading as one component rather than three.
   *
   * Passed as a prop rather than set from this component's stylesheet: the
   * previous `.host :global(.modal-container)` rule crossed a component
   * boundary and silently stopped applying, with no unused-selector warning.
   */
  const GALLERY_MODAL_HEIGHT = 'min(90vh, 780px)';
  import { onMount, onDestroy } from 'svelte';
  import { get } from 'svelte/store';

  import { toastStore } from '$stores/toast';
  import { isAuthenticated } from '$stores/auth';
  import { galleryStore, galleryState, hasMoreFiles, isLoadingMore } from '$stores/gallery';
  import { t } from '$stores/locale';
  import { getErrorCode } from '$lib/utils/apiError';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import { trashConfirmMessage, trashSelection } from '$lib/trash/galleryTrash';
  import SelectiveReprocessModal from '$components/SelectiveReprocessModal.svelte';
  import GalleryFilterPanel from '$components/gallery/GalleryFilterPanel.svelte';
  import GalleryHeader from '$components/gallery/GalleryHeader.svelte';
  import GalleryGrid from '$components/gallery/GalleryGrid.svelte';
  import BulkTagModal from '$components/gallery/BulkTagModal.svelte';
  import QuarantineModal from '$components/gallery/QuarantineModal.svelte';
  import GalleryLegalHold from '$components/gallery/GalleryLegalHold.svelte';
  import { buildGalleryQuery, hasActiveFilters, type FilterPanelDetail } from '$lib/gallery/galleryQuery';
  import { showFileErrorToast } from '$lib/gallery/fileErrorToast';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import type { MediaFile } from '$lib/types/media';
  import type { BulkTagAction } from '$lib/types/tag';

  // Modal state
  let showUploadModal = false;

  // Confirmation modal state
  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
    confirmText: confirmButtonText,
  } = confirmDialog;

  let showBulkTagModal = false;
  let bulkTagAction: BulkTagAction = 'add_tag';

  let showQuarantineModal = false;
  let quarantineTargetFiles: MediaFile[] = [];

  // Bulk reprocess modal state
  let showBulkReprocessModal = false;
  let bulkReprocessFiles: MediaFile[] = [];
  let bulkReprocessing = false;

  import { BULK_ACTIONS, BulkExporter, eligibleFiles, runBulkAction } from '$lib/gallery/bulkActions';
  import { fetchFilesPage, pageMeta } from '$lib/gallery/filesPage';
  import {
    applyNotificationBatch,
    createNotificationBatcher,
    createThrottledRefresher,
    markHeldRows,
    markQuarantinedRows,
    totalsAfterAdd,
    totalsAfterRemove,
    type GalleryNotification,
  } from '$lib/gallery/fileMapUpdates';
  import { listenForUploadRequests } from '$lib/gallery/uploadModalEvents';
  import { createInfiniteScrollObserver, createScrollSaver, restoreScrollTop } from '$lib/gallery/galleryScroll';
  import { bindGalleryTriggers, listenForTrashEvents } from '$lib/gallery/galleryTriggers';

  // Import components
  import FileUploader from '$components/FileUploader.svelte';
  import { supportSessionGate } from '$stores/supportSession';
  import CollectionsPanel from '$components/CollectionsPanel.svelte';
  import TagManagerModal from '$components/tags/TagManagerModal.svelte';

  // Media files state
  let files: MediaFile[] = [];
  let loading: boolean = true;
  let error: string | null = null;

  // Animation and smooth update state
  const fileMap = new Map<string, MediaFile>();
  let pendingNewFiles = new Set<string>();
  let pendingDeletions = new Set<string>();
  const refresher = createThrottledRefresher(() => fetchFiles());
  const throttledRefresh = refresher.refresh;

  // Scroll container ref for virtual scrolling
  let scrollableContentEl: HTMLElement | null = null;

  // Continuously save scroll position for back-nav restoration
  const scrollSaver = createScrollSaver(() => scrollableContentEl, (top) => galleryStore.setScrollTop(top));
  const onGalleryScroll = scrollSaver.onScroll;

  // Infinite scroll state
  let infiniteScrollSentinel: HTMLElement | null = null;
  let intersectionObserver: IntersectionObserver | null = null;

  // View state — restore from gallery store for navigation persistence
  let selectedCollectionId: string | null = $galleryState.filterSelectedCollectionId;
  let showCollectionsModal = false;
  let showTagManagerModal = false;

  // Use gallery store for state management
  $: isSelecting = $galleryState.isSelecting;
  $: selectedFiles = $galleryState.selectedFiles;

  // Component refs
  let filterPanelRef: any;

  // WebSocket subscription
  let stopRealtime: (() => void) | undefined;
  const bulkExporter = new BulkExporter();

  // Filter state — restore from gallery store for navigation persistence
  let searchQuery: string = $galleryState.filterSearchQuery;
  let selectedTags: string[] = [...$galleryState.filterSelectedTags];
  let selectedSpeakers: string[] = [...$galleryState.filterSelectedSpeakers];
  let dateRange = { ...$galleryState.filterDateRange };
  let durationRange = { ...$galleryState.filterDurationRange };
  let selectedFileTypes: string[] = [...$galleryState.filterSelectedFileTypes];
  let selectedStatuses: string[] = [...$galleryState.filterSelectedStatuses];
  let ownershipFilter: 'all' | 'mine' | 'shared' = $galleryState.filterOwnershipFilter;
  let selectedOwners: string[] = [...$galleryState.filterSelectedOwners];
  $: showFilters = $galleryState.showFilters;

  $: filtersActive = hasActiveFilters({
    searchQuery, selectedTags, selectedSpeakers, dateRange, durationRange, selectedFileTypes,
    selectedStatuses, ownershipFilter, hiddenMode: $galleryState.filterHiddenMode, held: $galleryState.filterHeld,
  });

  // Sort state — restore from gallery store
  let sortBy: string = $galleryState.filterSortBy;
  let sortOrder: 'asc' | 'desc' = $galleryState.filterSortOrder;

  // Observe the sentinel element when it becomes available (after files load)
  $: if (infiniteScrollSentinel && intersectionObserver) {
    intersectionObserver.observe(infiniteScrollSentinel);
  }

  // Build query parameters for API calls ($lib/gallery/galleryQuery owns the mapping)
  function buildQueryParams(page: number = 1): URLSearchParams {
    return buildGalleryQuery({
      page, pageSize: $galleryState.pageSize, sortBy, sortOrder, searchQuery, selectedTags,
      selectedSpeakers, dateRange, durationRange, selectedFileTypes, selectedStatuses,
      ownershipFilter, selectedOwners, hiddenMode: $galleryState.filterHiddenMode,
      held: $galleryState.filterHeld,
    });
  }

  // Fetch media files with smooth update support and pagination
  async function fetchFiles() {
    // Reset pagination on new fetch
    galleryStore.resetPagination();

    // Always show loading state so the count chip displays "loading..." instead
    // of prematurely showing "no matches" while the query is in progress
    loading = true;
    error = null;

    try {
      const page = await fetchFilesPage(selectedCollectionId, buildQueryParams(1));
      files = page.items;
      galleryStore.setFiles(page.items);
      const len = page.items.length;
      const meta = pageMeta(page, { page: 1, pageSize: len, total: len, totalPages: 1, hasMore: false });
      if (meta) galleryStore.appendFiles(page.items, meta);

      updateFileMap();

      // Note: scroll position restoration for back-nav is handled in onMount
      // before fetchFiles is called, using the cached files from the store.

    } catch (err: unknown) {
      console.error('Error fetching files:', err);
      if (getErrorCode(err) === 'ECONNABORTED') {
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
      const page = await fetchFilesPage(selectedCollectionId, buildQueryParams(nextPage));
      // Deduplicate: only add files not already loaded (prevents duplicate key errors)
      files = [...files, ...page.items.filter((f) => !fileMap.has(f.uuid))];
      galleryStore.setFiles(files);
      const meta = pageMeta(
        page,
        { page: nextPage, pageSize: page.items.length, total: files.length, totalPages: nextPage, hasMore: false },
        false
      );
      if (meta) galleryStore.appendFiles(page.items, meta);

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

      galleryStore.appendFiles([], totalsAfterAdd($galleryState));

      // Clear new file marker after animation
      setTimeout(() => {
        pendingNewFiles.delete(newFile.uuid);
        pendingNewFiles = new Set(pendingNewFiles);
      }, 600);
    } else {
      toastStore.info($t('gallery.newFileAvailable'));
      galleryStore.appendFiles([], totalsAfterAdd($galleryState));
    }
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

      if (actualDeletions.length > 0) {
        galleryStore.appendFiles([], totalsAfterRemove($galleryState, actualDeletions.length));
      }
    }, 250); // Match fade out animation duration
  }

  // Remove single file smoothly
  function removeFileSmooth(fileId: string) {
    removeFilesSmooth([fileId]);
  }

  // Handle filter changes
  function applyFilters(event: { detail: FilterPanelDetail }) {
    const { search, tags, speaker, collectionId, dates, durationRange: duration, fileTypes, statuses, ownership, owners } = event.detail;

    searchQuery = search;
    selectedTags = tags;
    selectedSpeakers = speaker;
    selectedCollectionId = collectionId;
    if (collectionId !== null && $galleryState.filterHeld) galleryStore.setHeldFilter(false, false);
    dateRange = dates;

    // Handle advanced filters if provided
    if (duration !== undefined) durationRange = duration;
    if (fileTypes !== undefined) selectedFileTypes = fileTypes;
    if (statuses !== undefined) selectedStatuses = statuses;
    if (ownership !== undefined) ownershipFilter = ownership;
    if (owners !== undefined) selectedOwners = owners;

    fetchFiles();
  }

  // Handle the toolbar's filename/title search (issue #747 — moved out of the
  // filter sidebar into GalleryHeader). Debouncing is owned by the shared
  // `SearchBar` primitive GalleryHeader renders, not duplicated here.
  function handleToolbarSearch(event: CustomEvent<{ value: string }>) {
    searchQuery = event.detail.value;
    fetchFiles();
  }

  // Handle sort change
  function handleSortChange(event: CustomEvent<{ sortBy: string; sortOrder: 'asc' | 'desc' }>) {
    sortBy = event.detail.sortBy;
    sortOrder = event.detail.sortOrder;
    fetchFiles();
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
  // Upload, URL import and recording are refused under a support grant. Every way of opening
  // the modal (button, drop, recorded-file event) funnels through this one flag.
  $: if ($supportSessionGate.active && showUploadModal) showUploadModal = false;

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
    selectedFileTypes = [];
    selectedStatuses = [];
    ownershipFilter = 'all';
    selectedOwners = [];
    sortBy = 'upload_time';
    sortOrder = 'desc';

    // Clear persisted filter state in the store
    galleryStore.resetFilters();

    // Skip animation when resetting filters to avoid highlighting previously filtered items
    fetchFiles();
  }

  // Delete moves the selection to the trash (#1101); the confirmation says for how long.
  async function deleteSelectedFiles() {
    if (selectedFiles.size === 0) return;
    confirmDialog.request(
      $t('gallery.trashConfirmTitle'),
      await trashConfirmMessage($t),
      () =>
        trashSelection(selectedFiles, {
          t: $t,
          removeFiles: removeFilesSmooth,
          refetch: fetchFiles,
          clearSelection,
        }),
      $t('gallery.trashConfirmButton')
    );
  }

  // Bulk reprocess selected files — opens SelectiveReprocessModal in bulk mode
  function bulkReprocess() {
    const selected = $galleryState.selectedFiles;
    if (selected.size === 0) return;

    const reprocessable = eligibleFiles(files, selected, ['completed', 'error', 'cancelled']);
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

  // Bulk tagging — the Organize menu's add/remove entries. The uuids come
  // straight from the shared selection. No `presentTagNames` is passed: `GET /files`
  // carries no per-file tags (#326), so BulkTagModal looks up the selection's tags
  // itself.
  function openBulkTagModal(action: BulkTagAction) {
    if ($galleryState.selectedFiles.size === 0) return;
    bulkTagAction = action;
    showBulkTagModal = true;
  }

  /** Opens the takedown modal over every currently-selected file, whatever its
   * status (issue #576 §B.5 — quarantining a still-downloading file is exactly
   * the case that matters, so this is NOT gated on completed status). */
  function openQuarantineModal() {
    const selected = $galleryState.selectedFiles;
    quarantineTargetFiles = files.filter((f) => selected.has(f.uuid));
    if (quarantineTargetFiles.length === 0) return;
    showQuarantineModal = true;
  }

  /**
   * Update rows locally; do NOT remove them (§B.6 E5) — a quarantined row
   * legitimately stays in an admin's gallery. Optimistically removing it looks
   * like it works until the next refetch puts it back.
   */
  function handleQuarantined(event: CustomEvent<{ uuids: string[] }>) {
    files = markQuarantinedRows(files, event.detail.uuids);
  }

  // A hold placed from the selection: badge the rows now (legal-hold plan, Lane D).
  function handleHoldPlaced(event: CustomEvent<{ uuids: string[]; authority: 'organization' | 'platform' }>) {
    files = markHeldRows(files, event.detail.uuids, event.detail.authority);
  }

  // Rows carry no tags, so only an active tag filter can change; a refetch resets pagination.
  function handleBulkTagApplied(event: CustomEvent<{ changed: number }>) {
    if (event.detail.changed > 0 && selectedTags.length > 0) {
      throttledRefresh('bulk-tag', 300);
    }
  }

  const selectedUuids = () => $galleryState.selectedFiles;
  const bulkSummarize = () => runBulkAction(BULK_ACTIONS.summarize, files, selectedUuids(), $t);
  const bulkRedact = () => runBulkAction(BULK_ACTIONS.redact, files, selectedUuids(), $t);
  const bulkRetryFailed = () => runBulkAction(BULK_ACTIONS.retry, files, selectedUuids(), $t);
  const bulkSpeakerId = () => runBulkAction(BULK_ACTIONS.speakerId, files, selectedUuids(), $t);
  const bulkCancelProcessing = () => runBulkAction(BULK_ACTIONS.cancel, files, selectedUuids(), $t);
  const bulkExport = (format: string) => bulkExporter.run(format, files, selectedUuids(), $t);

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

  function processNotificationBatch(batch: GalleryNotification[]) {
    const filesUpdated = applyNotificationBatch(batch, fileMap, $t, {
      removeFile: removeFileSmooth,
      addNewFile: addNewFileSmooth,
      refresh: throttledRefresh,
    });
    // Single batched update: rebuild the files array from fileMap once.
    if (filesUpdated) {
      files = files.map((f) => fileMap.get(f.uuid) || f);
      galleryStore.setFiles(files);
    }
  }

  onMount(() => {
    // DEFENSE-IN-DEPTH: refuse to render/fetch if unauthenticated.
    // The layout is the primary guard and should prevent this component
    // from mounting at all, but if it somehow does, block all API calls
    // and WebSocket subscriptions to avoid data leaks.
    if (!get(isAuthenticated)) {
      return;
    }

    document.title = $t('gallery.pageTitle');

    // Collapse filter sidebar on mobile to avoid covering the content
    if (window.innerWidth <= 768 && $galleryState.showFilters) {
      galleryStore.toggleFilters();
    }

    stopRealtime = createNotificationBatcher(processNotificationBatch, () =>
      throttledRefresh('uploads-complete', 800)
    );

    const stopUploadRequests = listenForUploadRequests(() => (showUploadModal = true));
    const stopTrashEvents = listenForTrashEvents(
      (id) => fileMap.has(id) && removeFileSmooth(id),
      fetchFiles
    );

    // On back navigation, restore files from store instead of re-fetching
    const savedScrollTop = $galleryState.scrollTop;
    const savedFiles = $galleryState.files;
    if (savedFiles.length > 0 && savedScrollTop > 0) {
      // Restore cached files so VirtualGrid has data to render
      files = [...savedFiles];
      updateFileMap();
      loading = false;

      restoreScrollTop(() => scrollableContentEl, savedScrollTop);
    } else {
      fetchFiles();
    }

    intersectionObserver = createInfiniteScrollObserver(() => {
      if ($hasMoreFiles && !$isLoadingMore) loadMoreFiles();
    });

    return () => {
      stopUploadRequests();
      stopTrashEvents();
      intersectionObserver?.disconnect();
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
      selectedFileTypes,
      selectedStatuses,
      ownershipFilter,
      selectedOwners,
      sortBy,
      sortOrder,
    });

    stopRealtime?.();

    scrollSaver.cancel();
    refresher.cancelAll();

    bulkExporter.close();
  });

  onMount(() =>
    bindGalleryTriggers({
      upload: toggleUploadModal,
      collections: () => (showCollectionsModal = true),
      addToCollection: () => (showCollectionsModal = true),
      addTags: () => openBulkTagModal('add_tag'),
      // One Tags button, two modes (the contract CollectionsPanel uses): a selection means
      // "apply to these", none means "manage the library".
      tags: () => {
        if ($galleryState.selectedFiles.size > 0) openBulkTagModal('add_tag');
        else showTagManagerModal = true;
      },
      removeTags: () => openBulkTagModal('remove_tag'),
      quarantine: openQuarantineModal,
      hiddenChanged: fetchFiles,
      // The computed "Legal hold" collection replaces a selected real one (the store cleared its copy).
      heldChanged: (held) => {
        if (held) selectedCollectionId = null;
        fetchFiles();
      },
      deleteSelected: deleteSelectedFiles,
      reprocess: bulkReprocess,
      summarize: bulkSummarize,
      redact: bulkRedact,
      retryFailed: bulkRetryFailed,
      exportSelected: bulkExport,
      speakerId: bulkSpeakerId,
      cancelProcessing: bulkCancelProcessing,
    })
  );
</script>

<!-- Main container with fixed height -->
<div class="media-library-container">
  <div class="gallery-tab-wrapper">
    <!-- Left Sidebar: Filters (Sticky) -->
    <GalleryFilterPanel
      bind:this={filterPanelRef}
      {showFilters}
      {searchQuery}
      {selectedTags}
      {selectedSpeakers}
      {selectedCollectionId}
      {dateRange}
      {durationRange}
      {selectedFileTypes}
      {selectedStatuses}
      {ownershipFilter}
      {selectedOwners}
      on:toggle={toggleFilters}
      on:filter={applyFilters}
      on:reset={resetFilters}
    />

    <!-- Right Content: Scrollable Media Grid -->
    <div class="content-area">
      <div class="scrollable-content" bind:this={scrollableContentEl} on:scroll={onGalleryScroll}>
        <!-- Gallery Header (sticky) - always visible for action buttons -->
        <GalleryHeader
          {files}
          {sortBy}
          {sortOrder}
          {loading}
          {showFilters}
          {searchQuery}
          on:togglefilters={toggleFilters}
          on:change={handleSortChange}
          on:search={handleToolbarSearch}
        />

        <GalleryGrid
          {files}
          {loading}
          {error}
          {selectedCollectionId}
          {isSelecting}
          {selectedFiles}
          {pendingNewFiles}
          {pendingDeletions}
          {filtersActive}
          scrollContainer={scrollableContentEl}
          on:sentinel={(e) => (infiniteScrollSentinel = e.detail)}
          on:retry={() => fetchFiles()}
          on:errorclick={(e) => showFileErrorToast(e.detail, $t)}
        />
      </div>
    </div>
  </div>
</div>

<!-- Upload Modal -->
{#if showUploadModal}
  <!--
    `closeOnBackdropClick={false}` is deliberate: the user has in-progress upload
    state (file, tags, collections, speaker settings) and a stray click outside
    must not destroy their work. The X button and Escape still close.

    This was hand-rolled modal chrome until #739. BaseModal brings the scroll
    lock, focus trap and `overscroll-behavior: contain` the copy never had — the
    last of which is why scrolling the wizard used to scroll the gallery behind it.
  -->
  <BaseModal
      isOpen={showUploadModal}
      title={$t('nav.addMedia')}
      size="lg"
      height={GALLERY_MODAL_HEIGHT}
      closeOnBackdropClick={false}
      onClose={toggleUploadModal}
    >
      <FileUploader on:uploadComplete={handleUploadComplete} />
    </BaseModal>
{/if}

<!-- Tag Manager Modal -->
{#if showTagManagerModal}
  <!--
    Sibling of the collections dialog below, deliberately: both attach metadata
    to files and were reading as different components. Both now use BaseModal,
    so they cannot drift on backdrop, radius, header or close affordance — and
    both share the upload wizard's fixed dialog size (see
    `GALLERY_MODAL_HEIGHT` at the top of this file).
  -->
  <BaseModal
      isOpen={showTagManagerModal}
      title={$t('tags.manager.title')}
      size="lg"
      height={GALLERY_MODAL_HEIGHT}
      onClose={() => (showTagManagerModal = false)}
    >
      <TagManagerModal />
    </BaseModal>
{/if}

<!-- Collections Modal -->
{#if showCollectionsModal}
  <BaseModal
      isOpen={showCollectionsModal}
      title={$t('gallery.manageCollections')}
      size="lg"
      height={GALLERY_MODAL_HEIGHT}
      onClose={() => (showCollectionsModal = false)}
    >
      <CollectionsPanel
          selectedMediaIds={Array.from(selectedFiles)}
          viewMode={selectedFiles.size > 0 ? 'add' : 'manage'}
          onHeldSelect={() => { showCollectionsModal = false; galleryStore.setHeldFilter(true); }}
          onCollectionSelect={() => {
            // Only close modal if we're in 'add' mode and actually adding files
            if (selectedFiles.size > 0) {
              showCollectionsModal = false;
              // Keep selection — user may want to add to multiple collections or perform other actions
            }
            // In manage mode, keep the modal open for multiple collections

            // Refresh collections in filter
            if (filterPanelRef && filterPanelRef.refreshCollections) {
              filterPanelRef.refreshCollections();
            }
            // Refresh files if we're filtering by collection
            if (selectedCollectionId !== null) {
              fetchFiles();
            }
          }}
      />
    </BaseModal>
{/if}

<!-- Confirmation Modal -->
<ConfirmationModal
  bind:isOpen={$confirmOpen}
  title={$confirmTitle}
  message={$confirmMessage}
  confirmText={$confirmButtonText || $t('gallery.confirmButton')}
  cancelText={$t('gallery.cancelButton')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<!-- Add or remove one tag across the selection; a Set iterates in the order files were picked -->
<BulkTagModal
  isOpen={showBulkTagModal}
  action={bulkTagAction}
  fileUuids={Array.from($galleryState.selectedFiles)}
  on:applied={handleBulkTagApplied}
  on:close={() => (showBulkTagModal = false)}
/>


<!-- Place legal hold on the selection — admins only (legal-hold plan, Lane D) -->
<GalleryLegalHold {files} on:placed={handleHoldPlaced} />

<!-- Quarantine (takedown) Modal — admin-only (issue #576) -->
<QuarantineModal
  isOpen={showQuarantineModal}
  files={quarantineTargetFiles}
  on:quarantined={handleQuarantined}
  on:close={() => (showQuarantineModal = false)}
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

  /* Gallery wrapper (formerly one of two navbar tabs; File Status now lives in Settings) */
  .gallery-tab-wrapper {
    display: flex;
    height: 100%;
    width: 100%;
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

  /* Responsive design */
  @media (max-width: 768px) {
    .media-library-container {
      flex-direction: column;
    }

    .content-area {
      width: 100%;
    }

    .scrollable-content {
      padding: 1rem;
      padding-top: 0;
      background-color: var(--background-color);
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
</style>
