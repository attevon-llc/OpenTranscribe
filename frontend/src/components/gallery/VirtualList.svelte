<script lang="ts">
  import './file-list-grid.css';
  import { onMount, onDestroy, createEventDispatcher } from 'svelte';
  import { goto } from '$app/navigation';
  import { galleryStore } from '$stores/gallery';
  import { t } from '$stores/locale';
  import { prefetchFileDetails, cancelPrefetch } from '$lib/prefetch';
  import type { MediaFile } from '$lib/types/media';

  export let items: MediaFile[] = [];
  export let scrollContainer: HTMLElement | null = null;
  export let isSelecting: boolean = false;
  export let selectedFiles: Set<string> = new Set();
  export let pendingNewFiles: Set<string> = new Set();
  export let pendingDeletions: Set<string> = new Set();

  const dispatch = createEventDispatcher<{ errorclick: MediaFile }>();

  // Virtual scrolling config
  const ROW_HEIGHT = 44;
  const OVERSCAN = 5;

  // State
  let containerOffset = 0; // offset of virtual container from scroll container top
  let visibleStart = 0;
  let visibleEnd = 0;
  let rafId: number | null = null;

  $: totalHeight = items.length * ROW_HEIGHT;
  $: {
    // Recalculate when items change. Unlike VirtualGrid's equivalent block, this runs
    // recalculate() synchronously rather than deferring via tick().then(measureOffset, ...).
    // That deferral exists in VirtualGrid because updateColumns()/measureOffset() read
    // post-patch DOM geometry (gridContainerEl.clientWidth, getBoundingClientRect()) that
    // is only accurate once Svelte has patched the DOM for the new item count — items.length
    // feeds totalHeight, which can toggle the scroll container's scrollbar and change the
    // width columnsPerRow is computed from.
    // recalculate() here has no such dependency: ROW_HEIGHT is fixed (no width-driven
    // column math, so no scrollbar-width feedback loop), and it only reads
    // scrollContainer.scrollTop/clientHeight — live properties of the external scroll
    // container, not of this component's own (possibly stale) DOM. containerOffset is
    // reused as last measured; an items change here doesn't invalidate it because nothing
    // above the virtual container (the sticky header) resizes with item count or selection
    // mode. So there's no stale geometry to wait out, and calling it synchronously is safe.
    if (items && scrollContainer) {
      recalculate();
    }
  }

  function recalculate() {
    if (!scrollContainer) return;

    const scrollTop = scrollContainer.scrollTop;
    const viewportHeight = scrollContainer.clientHeight;

    // Account for the offset of the virtual container within the scroll container
    const adjustedScrollTop = Math.max(0, scrollTop - containerOffset);

    const startRow = Math.floor(adjustedScrollTop / ROW_HEIGHT);
    const endRow = Math.ceil((adjustedScrollTop + viewportHeight) / ROW_HEIGHT);

    visibleStart = Math.max(0, startRow - OVERSCAN);
    visibleEnd = Math.min(items.length, endRow + OVERSCAN);
  }

  function onScroll() {
    if (rafId !== null) return;
    rafId = requestAnimationFrame(() => {
      rafId = null;
      recalculate();
    });
  }

  // Measure the offset of the virtual container from scroll container top
  let virtualContainerEl: HTMLElement | null = null;
  let boundScrollContainer: HTMLElement | null = null;

  function measureOffset() {
    if (virtualContainerEl && scrollContainer) {
      const containerRect = scrollContainer.getBoundingClientRect();
      const virtualRect = virtualContainerEl.getBoundingClientRect();
      containerOffset = virtualRect.top - containerRect.top + scrollContainer.scrollTop;
    }
  }

  $: if (scrollContainer !== boundScrollContainer) {
    // Remove listener from previous container
    if (boundScrollContainer) {
      boundScrollContainer.removeEventListener('scroll', onScroll);
    }
    boundScrollContainer = scrollContainer;
    if (scrollContainer) {
      scrollContainer.addEventListener('scroll', onScroll, { passive: true });
      requestAnimationFrame(() => {
        measureOffset();
        recalculate();
      });
    }
  }

  onMount(() => {
    requestAnimationFrame(() => {
      measureOffset();
      recalculate();
    });
  });

  onDestroy(() => {
    if (boundScrollContainer) {
      boundScrollContainer.removeEventListener('scroll', onScroll);
    }
    if (rafId !== null) {
      cancelAnimationFrame(rafId);
    }
  });

  $: visibleItems = items.slice(visibleStart, visibleEnd);
  $: topSpacerHeight = visibleStart * ROW_HEIGHT;
  $: bottomSpacerHeight = Math.max(0, (items.length - visibleEnd) * ROW_HEIGHT);

  // Track which row is currently navigating (double-click guard + visual feedback)
  let navigatingTo: string | null = null;

  function handleRowClick(file: MediaFile, e: MouseEvent) {
    if (isSelecting) {
      e.preventDefault();
      if (e.shiftKey || e.ctrlKey || e.metaKey) {
        galleryStore.handleMultiSelect(file.uuid, e.ctrlKey || e.metaKey, e.shiftKey);
      } else {
        galleryStore.toggleFileSelection(file.uuid);
      }
    } else if (e.ctrlKey || e.metaKey || e.shiftKey) {
      e.preventDefault();
      galleryStore.handleMultiSelect(file.uuid, e.ctrlKey || e.metaKey, e.shiftKey);
    } else {
      e.preventDefault();
      if (navigatingTo) return;
      navigatingTo = file.uuid;
      goto(`/files/${file.uuid}`);
    }
  }

  function handleRowMouseDown(file: MediaFile) {
    if (!isSelecting) prefetchFileDetails(file.uuid, file.status);
  }

  function handleCheckboxChange(fileId: string, e: Event) {
    e.stopPropagation();
    e.preventDefault();
    galleryStore.toggleFileSelection(fileId);
  }

  function handleCheckboxLabelClick(fileId: string, e: MouseEvent) {
    if (e.shiftKey || e.ctrlKey || e.metaKey) {
      e.preventDefault();
      galleryStore.handleMultiSelect(fileId, e.ctrlKey || e.metaKey, e.shiftKey);
    }
  }

  function handleErrorClick(file: MediaFile) {
    dispatch('errorclick', file);
  }
</script>

<div class="file-list" role="grid" aria-rowcount={items.length}>
  <!-- List Header -->
  <div class="file-list-header {isSelecting ? 'selecting-mode' : ''}" role="row">
    {#if isSelecting}
      <div class="list-cell list-cell-checkbox" role="columnheader"></div>
    {/if}
    <div class="list-cell list-cell-type" role="columnheader">{$t('gallery.columnType')}</div>
    <div class="list-cell list-cell-title" role="columnheader">{$t('gallery.columnTitle')}</div>
    <div class="list-cell list-cell-speakers" role="columnheader">{$t('gallery.columnSpeakers')}</div>
    <div class="list-cell list-cell-duration" role="columnheader">{$t('gallery.columnDuration')}</div>
    <div class="list-cell list-cell-date" role="columnheader">{$t('gallery.columnDate')}</div>
    <div class="list-cell list-cell-size" role="columnheader">{$t('gallery.columnSize')}</div>
    <div class="list-cell list-cell-status" role="columnheader">{$t('gallery.columnStatus')}</div>
  </div>

  <!-- Virtual scroll container -->
  <div
    bind:this={virtualContainerEl}
    class="virtual-list-container"
    style="height: {totalHeight}px; position: relative;"
  >
    <!-- Top spacer -->
    <div style="height: {topSpacerHeight}px;" aria-hidden="true"></div>

    <!-- Visible rows -->
    {#each visibleItems as file, i (file.uuid)}
      {@const globalIndex = visibleStart + i}
      <div
        class="file-list-row {selectedFiles.has(file.uuid) ? 'selected' : ''} {pendingNewFiles.has(file.uuid) ? 'new-file' : ''} {pendingDeletions.has(file.uuid) ? 'deleting' : ''} {isSelecting ? 'selecting-mode' : ''} {navigatingTo === file.uuid ? 'navigating' : ''}"
        class:even={globalIndex % 2 === 0}
        style="height: {ROW_HEIGHT}px;"
        role="row"
        aria-rowindex={globalIndex + 1}
        aria-busy={navigatingTo === file.uuid}
      >
        {#if isSelecting}
          <!-- svelte-ignore a11y-click-events-have-key-events -->
          <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
          <label
            class="list-cell list-cell-checkbox"
            on:click|stopPropagation={(e) => handleCheckboxLabelClick(file.uuid, e)}
          >
            <input
              type="checkbox"
              class="file-checkbox"
              checked={selectedFiles.has(file.uuid)}
              on:change={(e) => handleCheckboxChange(file.uuid, e)}
              title={$t('gallery.selectFileTooltip')}
            />
            <span class="checkmark"></span>
          </label>
        {/if}

        <a
          href={isSelecting ? '#' : `/files/${file.uuid}`}
          class="file-list-link"
          on:click={(e) => handleRowClick(file, e)}
          on:mousedown={() => handleRowMouseDown(file)}
          on:mouseenter={() => !isSelecting && prefetchFileDetails(file.uuid, file.status)}
          on:mouseleave={cancelPrefetch}
        >
          <!-- Type Icon -->
          <div class="list-cell list-cell-type">
            {#if file.content_type && file.content_type.startsWith('video/')}
              <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="type-icon video">
                <polygon points="23 7 16 12 23 17 23 7"></polygon>
                <rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect>
              </svg>
            {:else if file.content_type && file.content_type.startsWith('audio/')}
              <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="type-icon audio">
                <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
                <line x1="12" y1="19" x2="12" y2="23"></line>
                <line x1="8" y1="23" x2="16" y2="23"></line>
              </svg>
            {:else}
              <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="type-icon">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                <polyline points="14 2 14 8 20 8"></polyline>
              </svg>
            {/if}
          </div>

          <!-- Title -->
          <div class="list-cell list-cell-title">
            <span class="file-title">{file.title || file.filename}</span>
          </div>

          <!-- Speakers -->
          <div class="list-cell list-cell-speakers">
            {#if file.diarization_disabled}
              <span class="monologue-label" title={$t('gallery.diarizationDisabledTooltip')}>{$t('gallery.monologue')}</span>
            {:else if file.speaker_summary && file.speaker_summary.count > 0}
              <span class="speaker-names" title={file.speaker_summary.primary_speakers.join(', ')}>
                {file.speaker_summary.primary_speakers.join(', ')}
              </span>
              <span class="speaker-count">{file.speaker_summary.count}</span>
            {:else}
              <span class="no-speakers">-</span>
            {/if}
          </div>

          <!-- Duration -->
          <div class="list-cell list-cell-duration">
            {file.formatted_duration || '-'}
          </div>

          <!-- Date -->
          <div class="list-cell list-cell-date">
            {file.formatted_upload_date || '-'}
          </div>

          <!-- Size -->
          <div class="list-cell list-cell-size">
            {file.formatted_file_size || '-'}
          </div>

          <!-- Status -->
          <div class="list-cell list-cell-status">
            <div class="file-status status-{file.status}" class:clickable-error={file.status === 'error' && file.last_error_message}>
              <span class="status-dot"></span>
              {#if file.status === 'error' && file.last_error_message}
                <!-- svelte-ignore a11y-click-events-have-key-events -->
                <!-- svelte-ignore a11y-no-static-element-interactions -->
                <span
                  class="error-details-trigger"
                  on:click|preventDefault|stopPropagation={() => handleErrorClick(file)}
                  title={$t('gallery.errorClickForDetails')}
                >
                  {$t('common.error')}
                </span>
              {:else if file.status === 'completed'}
                {$t('common.completed')}
              {:else if file.status === 'processing'}
                {$t('common.processing')}
              {:else if file.status === 'pending'}
                {$t('common.pending')}
              {:else}
                {file.display_status || file.status}
              {/if}
            </div>
          </div>
        </a>
      </div>
    {/each}

    <!-- Bottom spacer -->
    <div style="height: {bottomSpacerHeight}px;" aria-hidden="true"></div>
  </div>
</div>

<style>
  .file-list {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border-color);
    border-radius: 10px;
    overflow: hidden;
    background: var(--surface-color);
  }

  .virtual-list-container {
    overflow: hidden;
    contain: layout style paint;
  }

  .file-list-header {
    display: grid;
    grid-template-columns: var(--file-list-columns);
    gap: 0.5rem;
    padding: 0.75rem 1rem;
    background: var(--surface-color);
    border-bottom: 1px solid var(--border-color);
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
    position: sticky;
    top: 0;
    z-index: 2;
  }

  .file-list-header.selecting-mode {
    grid-template-columns: var(--file-list-columns-selecting);
  }

  .file-list-row {
    position: relative;
    border-bottom: 1px solid var(--border-color);
    background: var(--surface-color);
    transition: background-color 0.15s ease;
    overflow: hidden;
  }

  /* Press feedback while the detail page is loading.
     Destination page shows a skeleton — no spinner needed on the source row. */
  .file-list-row.navigating {
    pointer-events: none;
  }

  .file-list-row.navigating .file-list-link {
    opacity: 0.72;
    transition: opacity 0.12s ease;
  }

  .file-list-row.navigating {
    background: rgba(59, 130, 246, 0.05);
  }

  :global([data-theme='dark']) .file-list-row.navigating {
    background: rgba(59, 130, 246, 0.1);
  }

  .file-list-row.selecting-mode {
    display: grid;
    grid-template-columns: 40px 1fr;
    user-select: none;
    -webkit-user-select: none;
  }

  .file-list-row.selecting-mode .file-list-link {
    grid-template-columns: var(--file-list-columns);
  }

  .file-list-link {
    display: grid;
    grid-template-columns: var(--file-list-columns);
    gap: 0.5rem;
    padding: 0.5rem 1rem;
    text-decoration: none;
    color: inherit;
    align-items: center;
    height: 100%;
  }

  .file-list-row.selecting-mode .file-list-link {
    padding: 0.5rem 1rem 0.5rem 0;
  }

  .file-list-row.selecting-mode .list-cell-checkbox {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0.5rem 0 0.5rem 1rem;
  }

  .file-list-row:hover {
    background-color: var(--hover-bg, rgba(0, 0, 0, 0.03));
  }

  :global([data-theme='dark']) .file-list-row:hover {
    background-color: rgba(255, 255, 255, 0.06);
  }

  .file-list-row.even {
    background-color: rgba(0, 0, 0, 0.01);
  }

  :global([data-theme='dark']) .file-list-row.even {
    background-color: rgba(255, 255, 255, 0.02);
  }

  .file-list-row.selected {
    background-color: rgba(59, 130, 246, 0.08);
    border-color: rgba(59, 130, 246, 0.2);
  }

  :global([data-theme='dark']) .file-list-row.selected {
    background-color: rgba(59, 130, 246, 0.15);
  }

  .file-list-row.new-file {
    animation: listNewFileGlow 0.6s ease-out;
  }

  @keyframes listNewFileGlow {
    0% { background-color: rgba(59, 130, 246, 0.15); }
    100% { background-color: transparent; }
  }

  .file-list-row.deleting {
    opacity: 0.5;
    pointer-events: none;
  }

  .list-cell {
    display: flex;
    align-items: center;
    min-width: 0;
  }

  .list-cell-checkbox {
    width: 40px;
    justify-content: center;
    position: relative;
  }

  .list-cell-checkbox .checkmark {
    width: 18px;
    height: 18px;
    background-color: var(--background-color);
    border: 2px solid var(--border-color);
    border-radius: 4px;
    position: relative;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .list-cell-checkbox:hover .checkmark {
    border-color: #3b82f6;
  }

  .list-cell-checkbox .file-checkbox:checked ~ .checkmark {
    background-color: var(--primary-color);
    border-color: #3b82f6;
  }

  .list-cell-checkbox .file-checkbox {
    position: absolute;
    opacity: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    cursor: pointer;
  }

  .list-cell-checkbox .checkmark:after {
    content: "";
    position: absolute;
    display: none;
    left: 5px;
    top: 1px;
    width: 5px;
    height: 10px;
    border: solid white;
    border-width: 0 2px 2px 0;
    transform: rotate(45deg);
  }

  .list-cell-checkbox .file-checkbox:checked ~ .checkmark:after {
    display: block;
  }

  .list-cell-type {
    width: 48px;
    justify-content: center;
  }

  .type-icon {
    color: var(--text-secondary);
  }

  .type-icon.video,
  .type-icon.audio {
    color: var(--primary-color, #4f46e5);
  }

  .list-cell-title {
    min-width: 0;
    overflow: hidden;
  }

  .file-title {
    display: block;
    font-weight: 500;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .list-cell-speakers {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    display: flex;
    flex-wrap: nowrap;
    align-items: center;
    gap: 0.375rem;
    overflow: hidden;
    min-width: 0;
  }

  .speaker-names {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .speaker-count {
    flex: 0 0 auto;
    font-size: 0.6875rem;
    font-weight: 500;
    color: var(--primary-on-surface);
    background: var(--primary-color-alpha, rgba(59, 130, 246, 0.1));
    padding: 0.125rem 0.375rem;
    border-radius: 9999px;
    white-space: nowrap;
    margin-left: 0.25rem;
  }

  .no-speakers {
    color: var(--text-tertiary);
  }

  .monologue-label {
    font-style: italic;
    color: var(--text-secondary);
    cursor: help;
  }

  .list-cell-duration,
  .list-cell-date,
  .list-cell-size {
    font-size: 0.875rem;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .list-cell-status {
    justify-content: flex-end;
  }

  .list-cell-status .file-status {
    font-size: 0.7rem;
  }

  .file-status {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 0.65rem;
    font-weight: 500;
    width: fit-content;
    white-space: nowrap;
  }

  .status-pending,
  .status-processing,
  .status-cancelling {
    color: #f59e0b;
  }

  .status-completed {
    color: #10b981;
  }

  .status-error {
    color: #ef4444;
  }

  .status-cancelled {
    color: #6b7280;
  }

  .status-orphaned {
    color: #dc2626;
  }

  /* Abuse / DMCA takedown hold — amber, distinct from the red error states. */
  .status-quarantined {
    color: #d97706;
  }

  .clickable-error {
    cursor: pointer;
    transition: opacity 0.2s ease;
  }

  .clickable-error:hover {
    opacity: 0.8;
  }

  .error-details-trigger {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    text-decoration: underline;
    text-decoration-style: dotted;
  }

  .error-details-trigger:hover {
    text-decoration-style: solid;
  }

  .status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background-color: currentColor;
  }

  @keyframes pulse {
    0% { opacity: 0.6; }
    50% { opacity: 1; }
    100% { opacity: 0.6; }
  }

  .status-processing .status-dot {
    animation: pulse 2s ease-in-out infinite;
  }

  /* Hide speakers and size columns on tablets */
  @media (max-width: 1024px) {
    .list-cell-speakers,
    .list-cell-size {
      display: none;
    }
  }

  @media (max-width: 768px) {
    .list-cell-speakers,
    .list-cell-duration,
    .list-cell-date,
    .list-cell-size {
      display: none;
    }

    .list-cell-type {
      width: 40px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .file-list-row.new-file {
      animation: none;
    }

    .file-list-row {
      transition: none;
    }
  }
</style>
