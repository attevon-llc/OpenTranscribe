<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { galleryStore, galleryState, selectedCount, allFilesSelected } from '../../stores/gallery';
  import { t } from '../../stores/locale';
  import type { MediaFile } from '$lib/types/media';

  export let files: MediaFile[] = [];

  // Derive disabled states reactively from selected file objects
  $: selectedFileObjects = files.filter(f => $galleryState.selectedFiles.has(f.uuid));
  $: hasCompletedSelected = selectedFileObjects.some(f => f.status === 'completed');
  $: hasFailedSelected = selectedFileObjects.some(f => f.status === 'error');
  $: hasProcessingSelected = selectedFileObjects.some(f => f.status === 'processing');
  $: hasReprocessable = selectedFileObjects.some(f => f.status === 'completed' || f.status === 'error' || f.status === 'cancelled');

  // Dropdown states
  let showProcessMenu = false;
  let showOrganizeMenu = false;

  function closeAllMenus() {
    showProcessMenu = false;
    showOrganizeMenu = false;
  }

  function toggleProcessMenu(event: MouseEvent) {
    event.stopPropagation();
    showOrganizeMenu = false;
    showProcessMenu = !showProcessMenu;
  }

  function toggleOrganizeMenu(event: MouseEvent) {
    event.stopPropagation();
    showProcessMenu = false;
    showOrganizeMenu = !showOrganizeMenu;
  }

  function handleClickOutside() {
    if (showProcessMenu || showOrganizeMenu) {
      closeAllMenus();
    }
  }

  onMount(() => {
    document.addEventListener('click', handleClickOutside);
  });

  onDestroy(() => {
    document.removeEventListener('click', handleClickOutside);
  });

  // Event handlers
  function handleUploadClick() { galleryStore.triggerUpload(); }
  function handleCollectionsClick() { galleryStore.triggerCollections(); }
  function handleSelectFilesClick() { galleryStore.setSelecting(true); }
  function handleSelectAllFiles() { galleryStore.selectAllFiles(); }
  function handleDeleteSelected() { galleryStore.triggerDeleteSelected(); }
  function handleCancelSelection() { galleryStore.clearSelection(); }
  function handleAddToCollection() { galleryStore.triggerAddToCollection(); closeAllMenus(); }
  function handleAddTags() { galleryStore.triggerAddTags(); closeAllMenus(); }
  function handleRemoveTags() { galleryStore.triggerRemoveTags(); closeAllMenus(); }
  function handleReprocess() { galleryStore.triggerReprocess(); closeAllMenus(); }
  function handleSummarize() { galleryStore.triggerSummarize(); closeAllMenus(); }
  function handleRedact() { galleryStore.triggerRedact(); closeAllMenus(); }
  function handleRetryFailed() { galleryStore.triggerRetryFailed(); closeAllMenus(); }
  function handleExport(format: string) { galleryStore.triggerExport(format); closeAllMenus(); }
  function handleSpeakerId() { galleryStore.triggerSpeakerId(); closeAllMenus(); }
  function handleCancelProcessing() { galleryStore.triggerCancelProcessing(); closeAllMenus(); }
</script>

<div class="gallery-action-buttons">
  {#if $galleryState.isSelecting}
    <!-- Selection mode: consolidated into dropdown groups -->
    <div class="selection-actions">
      <button
        class="action-btn select-all-btn"
        on:click={handleSelectAllFiles}
        title={$t('gallery.bulk.selectAllTooltip')}
      >
        {$allFilesSelected ? $t('nav.deselectAll') : $t('nav.selectAll')}
      </button>

      <div class="action-separator"></div>

      <!-- Process dropdown -->
      <div class="dropdown-container">
        <button
          class="action-btn process-btn"
          on:click={toggleProcessMenu}
          title={$t('gallery.bulk.processTooltip')}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="23 4 23 10 17 10"></polyline>
            <polyline points="1 20 1 14 7 14"></polyline>
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
          </svg>
          <span>Process ({$selectedCount})</span>
          <svg class="chevron" class:open={showProcessMenu} xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="6 9 12 15 18 9"></polyline>
          </svg>
        </button>
        {#if showProcessMenu}
          <!-- svelte-ignore a11y-click-events-have-key-events -->
          <!-- svelte-ignore a11y-no-static-element-interactions -->
          <div class="dropdown-menu" on:click|stopPropagation>
            <button
              class="dropdown-item"
              on:click={handleReprocess}
              disabled={!hasReprocessable}
              title={$t('gallery.bulk.reprocessTooltip')}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="23 4 23 10 17 10"></polyline>
                <polyline points="1 20 1 14 7 14"></polyline>
                <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
              </svg>
              {$t('gallery.bulk.reprocess')}
            </button>
            <button
              class="dropdown-item"
              on:click={handleSummarize}
              disabled={!hasCompletedSelected}
              title={$t('gallery.bulk.summarizeTooltip')}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                <polyline points="14 2 14 8 20 8"></polyline>
                <line x1="16" y1="13" x2="8" y2="13"></line>
                <line x1="16" y1="17" x2="8" y2="17"></line>
              </svg>
              {$t('gallery.bulk.summarize')}
            </button>
            <button
              class="dropdown-item"
              on:click={handleRedact}
              disabled={!hasCompletedSelected}
              title={$t('gallery.bulk.redactTooltip')}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"></path>
                <path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"></path>
                <path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"></path>
                <line x1="2" y1="2" x2="22" y2="22"></line>
              </svg>
              {$t('gallery.bulk.redact')}
            </button>
            <button
              class="dropdown-item"
              on:click={handleRetryFailed}
              disabled={!hasFailedSelected}
              title={$t('gallery.bulk.retryFailedTooltip')}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="1 4 1 10 7 10"></polyline>
                <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"></path>
              </svg>
              {$t('gallery.bulk.retryFailed')}
            </button>
            <div class="dropdown-divider"></div>
            <button
              class="dropdown-item"
              on:click={handleSpeakerId}
              disabled={!hasCompletedSelected}
              title={$t('gallery.bulk.speakerIdTooltip')}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
                <circle cx="12" cy="7" r="4"></circle>
              </svg>
              {$t('gallery.bulk.speakerId')}
            </button>
            <button
              class="dropdown-item"
              on:click={handleCancelProcessing}
              disabled={!hasProcessingSelected}
              title={$t('gallery.bulk.cancelProcessingTooltip')}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="15" y1="9" x2="9" y2="15"></line>
                <line x1="9" y1="9" x2="15" y2="15"></line>
              </svg>
              {$t('gallery.bulk.cancelProcessing')}
            </button>
          </div>
        {/if}
      </div>

      <!-- Organize dropdown -->
      <div class="dropdown-container">
        <button
          class="action-btn organize-btn"
          on:click={toggleOrganizeMenu}
          title={$t('gallery.bulk.organizeTooltip')}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
          </svg>
          <span>Organize ({$selectedCount})</span>
          <svg class="chevron" class:open={showOrganizeMenu} xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="6 9 12 15 18 9"></polyline>
          </svg>
        </button>
        {#if showOrganizeMenu}
          <!-- svelte-ignore a11y-click-events-have-key-events -->
          <!-- svelte-ignore a11y-no-static-element-interactions -->
          <div class="dropdown-menu" on:click|stopPropagation>
            <button
              class="dropdown-item"
              on:click={handleAddToCollection}
              title={$t('gallery.bulk.addToCollectionTooltip')}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
                <line x1="12" y1="11" x2="12" y2="17"></line>
                <line x1="9" y1="14" x2="15" y2="14"></line>
              </svg>
              {$t('gallery.bulk.addToCollection')}
            </button>
            <button
              class="dropdown-item"
              on:click={handleAddTags}
              disabled={$selectedCount === 0}
              title={$t('gallery.bulk.addTagTooltip')}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"></path>
                <line x1="7" y1="7" x2="7.01" y2="7"></line>
              </svg>
              {$t('gallery.bulk.addTag')}
            </button>
            <button
              class="dropdown-item"
              on:click={handleRemoveTags}
              disabled={$selectedCount === 0}
              title={$t('gallery.bulk.removeTagTooltip')}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"></path>
                <line x1="7" y1="7" x2="7.01" y2="7"></line>
                <line x1="9" y1="15" x2="15" y2="9"></line>
              </svg>
              {$t('gallery.bulk.removeTag')}
            </button>
            <div class="dropdown-divider"></div>
            <button
              class="dropdown-item"
              on:click={() => handleExport('srt')}
              title={$t('gallery.bulk.exportSrtTooltip')}
            >
              {$t('gallery.bulk.exportSrt')}
            </button>
            <button
              class="dropdown-item"
              on:click={() => handleExport('webvtt')}
              title={$t('gallery.bulk.exportWebvttTooltip')}
            >
              {$t('gallery.bulk.exportWebvtt')}
            </button>
            <button
              class="dropdown-item"
              on:click={() => handleExport('txt')}
              title={$t('gallery.bulk.exportTxtTooltip')}
            >
              {$t('gallery.bulk.exportTxt')}
            </button>
          </div>
        {/if}
      </div>

      <div class="action-separator"></div>

      <button
        class="action-btn delete-btn"
        on:click={handleDeleteSelected}
        title={$t('gallery.bulk.deleteTooltip')}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="3 6 5 6 21 6"></polyline>
          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
          <line x1="10" y1="11" x2="10" y2="17"></line>
          <line x1="14" y1="11" x2="14" y2="17"></line>
        </svg>
        <span>{$t('nav.deleteSelected', { count: $selectedCount })}</span>
      </button>

      <button
        class="action-btn cancel-btn"
        on:click={handleCancelSelection}
        title={$t('gallery.bulk.cancelSelectionTooltip')}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>
    </div>
  {:else}
    <!-- Normal mode -->
    <div class="normal-actions">
      <button
        class="action-btn upload-btn"
        on:click={handleUploadClick}
        title={$t('gallery.bulk.addMediaTooltip')}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
          <polyline points="17 8 12 3 7 8"></polyline>
          <line x1="12" y1="3" x2="12" y2="15"></line>
        </svg>
        <span>{$t('nav.addMedia')}</span>
      </button>
      <button
        class="action-btn collections-btn"
        on:click={handleCollectionsClick}
        title={$t('gallery.bulk.collectionsTooltip')}
      >
        <span>{$t('nav.collections')}</span>
      </button>
      <button
        class="action-btn select-btn"
        on:click={handleSelectFilesClick}
        title={$t('gallery.bulk.selectFilesTooltip')}
      >
        <span>{$t('nav.selectFiles')}</span>
      </button>
    </div>
  {/if}
</div>

<style>
  .gallery-action-buttons {
    display: flex;
    align-items: center;
  }

  .normal-actions,
  .selection-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .action-separator {
    width: 1px;
    height: 24px;
    background-color: var(--border-color);
    margin: 0 0.125rem;
    flex-shrink: 0;
  }

  /* Base button style */
  .action-btn {
    color: white;
    border: none;
    padding: 0.375rem 0.75rem;
    border-radius: 10px;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 0.35rem;
    white-space: nowrap;
    flex-shrink: 0;
    font-family: inherit;
  }

  .action-btn:hover:not(:disabled) {
    transform: scale(1.02);
  }

  .action-btn:active:not(:disabled) {
    transform: scale(1);
  }

  .action-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .action-btn svg {
    width: 14px;
    height: 14px;
    flex-shrink: 0;
  }

  .chevron {
    transition: transform 0.2s ease;
  }

  .chevron.open {
    transform: rotate(180deg);
  }

  /*
   * Button color system — 2-color toolbar (Apple HIG / Material Design):
   *   PRIMARY (blue)    — one main action per view (Upload, Select All, Process)
   *   SECONDARY (surface/border) — all other toolbar actions
   *   DANGER (red)      — destructive actions only (Delete)
   *   CANCEL (gray)     — dismiss / exit mode
   *
   * Purple, green, and amber were previously used but created a rainbow toolbar
   * that looked arbitrary. Semantic colors should only appear when the meaning
   * is clear (red = destructive, amber = warning, green = success confirmation).
   */

  /* Primary actions — solid blue */
  .upload-btn,
  .select-all-btn,
  .process-btn {
    background-color: var(--primary-color, #3b82f6);
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .upload-btn:hover:not(:disabled),
  .select-all-btn:hover:not(:disabled),
  .process-btn:hover:not(:disabled) {
    background-color: var(--primary-hover, #2563eb);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  /* Secondary actions — surface with border (not colored) */
  .collections-btn,
  .select-btn,
  .organize-btn {
    background-color: var(--surface-color);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
  }

  .collections-btn:hover:not(:disabled),
  .select-btn:hover:not(:disabled),
  .organize-btn:hover:not(:disabled) {
    background-color: var(--button-hover, #f1f5f9);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.08);
  }

  :global(.dark) .collections-btn:hover:not(:disabled),
  :global(.dark) .select-btn:hover:not(:disabled),
  :global(.dark) .organize-btn:hover:not(:disabled) {
    background-color: rgba(255, 255, 255, 0.08);
  }

  /* Danger — red, destructive only */
  .delete-btn {
    background-color: #dc2626;
    box-shadow: 0 2px 4px rgba(220, 38, 38, 0.2);
  }

  .delete-btn:hover:not(:disabled) {
    background-color: #b91c1c;
    box-shadow: 0 4px 8px rgba(220, 38, 38, 0.25);
  }

  /* Cancel — neutral gray */
  .cancel-btn {
    background-color: #6b7280;
    box-shadow: 0 2px 4px rgba(107, 114, 128, 0.2);
    padding: 0.375rem 0.5rem;
    align-self: stretch;
  }

  .cancel-btn:hover:not(:disabled) {
    background-color: #4b5563;
    box-shadow: 0 4px 8px rgba(107, 114, 128, 0.25);
  }

  /* Dropdown */
  .dropdown-container {
    position: relative;
  }

  .dropdown-menu {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    min-width: 200px;
    z-index: 100;
    padding: 0.25rem 0;
  }

  .dropdown-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: none;
    background: none;
    color: var(--text-color);
    font-size: 0.8125rem;
    cursor: pointer;
    text-align: left;
    transition: background-color 0.15s ease;
    font-family: inherit;
  }

  .dropdown-item:hover:not(:disabled) {
    background-color: var(--hover-color, rgba(0, 0, 0, 0.05));
  }

  .dropdown-item:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .dropdown-item svg {
    width: 14px;
    height: 14px;
    flex-shrink: 0;
  }

  .dropdown-divider {
    height: 1px;
    background-color: var(--border-color);
    margin: 0.25rem 0;
  }


  /* Tablet: allow action buttons to wrap */
  @media (max-width: 1200px) {
    .normal-actions,
    .selection-actions {
      flex-wrap: wrap;
      gap: 0.375rem;
    }

    .action-btn {
      padding: 0.35rem 0.65rem;
      font-size: 0.8rem;
    }
  }

  /* Mobile responsive */
  @media (max-width: 768px) {
    .gallery-action-buttons {
      min-width: 0;
      overflow: visible;
    }

    .action-btn svg {
      width: 16px;
      height: 16px;
    }

    .cancel-btn {
      padding: 0.35rem 0.5rem;
    }

    .action-separator {
      height: 20px;
    }
  }

  @media (max-width: 480px) {
    .action-btn {
      padding: 0.35rem 0.6rem;
      font-size: 0.75rem;
    }

    .action-separator {
      display: none;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .action-btn,
    .chevron {
      transition: none;
    }

    .action-btn:hover:not(:disabled) {
      transform: none;
    }
  }
</style>
