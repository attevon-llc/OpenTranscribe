<script lang="ts">
  import { onMount, onDestroy, createEventDispatcher, tick } from 'svelte';
  import { goto } from '$app/navigation';
  import { cachedThumbnail } from '$lib/thumbnailCache';
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

  // Virtual scrolling config — compact Apple-like cards
  const isMobile = typeof window !== 'undefined' && window.innerWidth < 768;
  const ROW_HEIGHT = isMobile ? 155 : 195;
  const CARD_MIN_WIDTH = isMobile ? 140 : 220;
  const GAP = isMobile ? 8 : 12;
  const OVERSCAN = isMobile ? 3 : 2;

  // State
  let columnsPerRow = 1;
  let containerOffset = 0;
  let visibleStartRow = 0;
  let visibleEndRow = 0;
  let totalRows = 0;
  let rafId: number | null = null;
  let resizeObserver: ResizeObserver | null = null;
  let gridContainerEl: HTMLElement | null = null;
  let virtualContainerEl: HTMLElement | null = null;
  let boundScrollContainer: HTMLElement | null = null;

  $: totalHeight = totalRows * ROW_HEIGHT;
  // When all rows are visible, let the container auto-size to avoid dead space
  // from ROW_HEIGHT overestimating actual card height
  $: allRowsVisible = visibleStartRow === 0 && visibleEndRow >= totalRows;

  $: {
    if (items && scrollContainer) {
      // Defer to after DOM update — reading getBoundingClientRect() synchronously
      // returns stale geometry because Svelte hasn't patched the DOM yet
      tick().then(() => {
        updateColumns();
        measureOffset();
        recalculate();
      });
    }
  }

  function updateColumns() {
    if (!gridContainerEl) return;
    const width = gridContainerEl.clientWidth;
    const newCols = Math.max(1, Math.floor((width + GAP) / (CARD_MIN_WIDTH + GAP)));
    if (newCols !== columnsPerRow) {
      columnsPerRow = newCols;
    }
    // Always recalculate on resize (viewport height may have changed)
    measureOffset();
    recalculate();
  }

  function measureOffset() {
    if (virtualContainerEl && scrollContainer) {
      const containerRect = scrollContainer.getBoundingClientRect();
      const virtualRect = virtualContainerEl.getBoundingClientRect();
      containerOffset = virtualRect.top - containerRect.top + scrollContainer.scrollTop;
    }
  }

  function recalculate() {
    if (!scrollContainer) return;

    // Compute totalRows inline to avoid stale reactive values
    totalRows = Math.ceil(items.length / columnsPerRow);

    const scrollTop = scrollContainer.scrollTop;
    const viewportHeight = scrollContainer.clientHeight;
    const adjustedScrollTop = Math.max(0, scrollTop - containerOffset);

    const startRow = Math.floor(adjustedScrollTop / ROW_HEIGHT);
    const endRow = Math.ceil((adjustedScrollTop + viewportHeight) / ROW_HEIGHT);

    visibleStartRow = Math.max(0, startRow - OVERSCAN);
    visibleEndRow = Math.min(totalRows, endRow + OVERSCAN);
  }

  function onScroll() {
    if (rafId !== null) return;
    rafId = requestAnimationFrame(() => {
      rafId = null;
      recalculate();
    });
  }

  $: if (scrollContainer !== boundScrollContainer) {
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
      updateColumns();
      measureOffset();
      recalculate();
    });

    if (gridContainerEl) {
      resizeObserver = new ResizeObserver(() => {
        updateColumns();
      });
      resizeObserver.observe(gridContainerEl);
    }
  });

  onDestroy(() => {
    if (boundScrollContainer) {
      boundScrollContainer.removeEventListener('scroll', onScroll);
    }
    if (rafId !== null) {
      cancelAnimationFrame(rafId);
    }
    if (resizeObserver) {
      resizeObserver.disconnect();
    }
  });

  // Compute visible items from visible rows
  $: visibleStartIndex = visibleStartRow * columnsPerRow;
  $: visibleEndIndex = Math.min(items.length, visibleEndRow * columnsPerRow);
  $: visibleItems = items.slice(visibleStartIndex, visibleEndIndex);
  $: topSpacerHeight = visibleStartRow * ROW_HEIGHT;
  $: bottomSpacerHeight = Math.max(0, (totalRows - visibleEndRow) * ROW_HEIGHT);

  // Track which file is currently navigating to prevent double-clicks and
  // provide immediate visual feedback while the route change is in flight.
  let navigatingTo: string | null = null;

  function handleCardClick(file: MediaFile, e: MouseEvent) {
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
      // Guard against double-clicks — ignore if already navigating
      if (navigatingTo) return;
      navigatingTo = file.uuid;
      goto(`/files/${file.uuid}`);
    }
  }

  // Kick off prefetch on mousedown (slightly earlier than click) to get a
  // head start on loading detail data.
  function handleCardMouseDown(file: MediaFile) {
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

<div
  bind:this={gridContainerEl}
  class="virtual-grid-wrapper"
  role="grid"
  aria-rowcount={totalRows}
>
  <div
    bind:this={virtualContainerEl}
    class="virtual-grid-container"
    style="{allRowsVisible ? '' : `height: ${totalHeight}px;`} position: relative;"
  >
    <!-- Top spacer -->
    <div style="height: {topSpacerHeight}px;" aria-hidden="true"></div>

    <!-- Visible items in CSS grid -->
    <div class="file-grid">
      {#each visibleItems as file, i (file.uuid)}
        {@const globalIndex = visibleStartIndex + i}
        <div
          class="file-card {selectedFiles.has(file.uuid) ? 'selected' : ''} {pendingNewFiles.has(file.uuid) ? 'new-file' : ''} {pendingDeletions.has(file.uuid) ? 'deleting' : ''} {isSelecting ? 'selecting-mode' : ''} {navigatingTo === file.uuid ? 'navigating' : ''}"
          role="gridcell"
          aria-rowindex={Math.floor(globalIndex / columnsPerRow) + 1}
        >
          {#if isSelecting}
            <!-- svelte-ignore a11y-click-events-have-key-events -->
            <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
            <label
              class="file-selector"
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
            class="file-card-link"
            on:click={(e) => handleCardClick(file, e)}
            on:mousedown={() => handleCardMouseDown(file)}
            on:mouseenter={() => !isSelecting && prefetchFileDetails(file.uuid, file.status)}
            on:mouseleave={cancelPrefetch}
            aria-busy={navigatingTo === file.uuid}
          >
            <!-- Thumbnail area — edge-to-edge -->
            <div class="thumbnail-container">
              {#if file.thumbnail_url && file.content_type && file.content_type.startsWith('video/')}
                <img
                  use:cachedThumbnail={{ uuid: file.uuid, url: file.thumbnail_url }}
                  alt={$t('gallery.thumbnailAlt', { title: file.title || file.filename })}
                  loading="lazy"
                  decoding="async"
                  class="thumbnail-image"
                />
              {:else if file.content_type && file.content_type.startsWith('video/')}
                <div class="placeholder video-placeholder">
                  <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                    <polygon points="23 7 16 12 23 17 23 7"></polygon>
                    <rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect>
                  </svg>
                </div>
              {:else if file.content_type && file.content_type.startsWith('audio/')}
                <div class="placeholder audio-placeholder">
                  <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
                    <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
                    <line x1="12" y1="19" x2="12" y2="23"></line>
                    <line x1="8" y1="23" x2="16" y2="23"></line>
                  </svg>
                </div>
              {:else}
                <div class="placeholder file-placeholder">
                  <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                    <polyline points="14 2 14 8 20 8"></polyline>
                  </svg>
                </div>
              {/if}

              <!-- Type badge (top-left) -->
              {#if file.content_type}
                <div class="type-badge">
                  {#if file.content_type.startsWith('video/')}
                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <polygon points="23 7 16 12 23 17 23 7"></polygon>
                      <rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect>
                    </svg>
                  {:else}
                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
                      <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
                    </svg>
                  {/if}
                </div>
              {/if}

              <!-- Duration badge (bottom-right) -->
              {#if file.formatted_duration}
                <div class="duration-badge">{file.formatted_duration}</div>
              {/if}
            </div>

            <!-- Text area -->
            <div class="card-text">
              <h2 class="file-name">{file.title || file.filename}</h2>

              <div class="meta-line">
                <span>{file.formatted_upload_date}</span>
                {#if file.formatted_file_size}
                  <span class="meta-dot">&middot;</span>
                  <span>{file.formatted_file_size}</span>
                {/if}
                {#if file.speaker_summary && file.speaker_summary.count > 0}
                  <span class="meta-dot">&middot;</span>
                  <span>{file.speaker_summary.count} spk</span>
                {/if}
                <!-- Status dot with inline label on hover -->
                <!-- svelte-ignore a11y-click-events-have-key-events -->
                <!-- svelte-ignore a11y-no-static-element-interactions -->
                <span
                  class="status-wrap status-{file.status}"
                  class:clickable-error={file.status === 'error' && file.last_error_message}
                  on:click|preventDefault|stopPropagation={() => file.status === 'error' && file.last_error_message && handleErrorClick(file)}
                >
                  <span class="status-label">{file.status === 'error' && file.last_error_message ? $t('gallery.errorClickForDetails') : (file.display_status || file.status)}</span>
                  <span class="status-dot"></span>
                </span>
              </div>
            </div>
          </a>
        </div>
      {/each}
    </div>

    <!-- Bottom spacer -->
    <div style="height: {bottomSpacerHeight}px;" aria-hidden="true"></div>
  </div>
</div>

<style>
  .virtual-grid-wrapper {
    width: 100%;
  }

  .virtual-grid-container {
    overflow: visible;
    contain: layout style;
    padding-top: 0.5rem;
    margin-top: -0.5rem;
  }

  .file-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 0.75rem;
    padding: 0 2px 0.5rem;
  }

  /* --- Card --- */

  .file-card {
    position: relative;
    border: 1px solid var(--border-color);
    background-color: var(--surface-color);
    border-radius: 12px;
    transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
    cursor: pointer;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transform: translateZ(0);
  }

  .file-card.new-file {
    animation: newFileGlow 0.6s ease-out;
  }

  .file-card.deleting {
    opacity: 0.5;
    transform: scale(0.95);
    transition: all 0.25s ease-out;
    pointer-events: none;
  }

  /* Press feedback while the detail page is loading.
     Apple HIG: use a brief active-state, not a spinner on the source item.
     The destination page shows a skeleton that actually represents progress. */
  .file-card.navigating {
    pointer-events: none;
  }

  .file-card.navigating .file-card-link {
    opacity: 0.72;
    transform: scale(0.985);
    transition: opacity 0.12s ease, transform 0.12s ease;
  }

  @keyframes newFileGlow {
    0% {
      transform: scale(1);
      box-shadow: 0 0 15px rgba(59, 130, 246, 0.4);
      border-color: #60a5fa;
    }
    100% {
      transform: scale(1);
      box-shadow: none;
      border-color: var(--border-color);
    }
  }

  :global([data-theme='dark']) .file-card.new-file {
    animation: newFileGlowDark 0.6s ease-out;
  }

  @keyframes newFileGlowDark {
    0% {
      box-shadow: 0 0 15px rgba(96, 165, 250, 0.3);
      border-color: #60a5fa;
    }
    100% {
      box-shadow: none;
      border-color: var(--border-color);
    }
  }

  .file-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 16px -4px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
    border-color: var(--border-hover);
  }

  .file-card:hover .thumbnail-image {
    transform: scale(1.03);
  }

  .file-card.selected {
    border: 2px solid #3b82f6;
    background-color: rgba(59, 130, 246, 0.05);
    box-shadow: 0 4px 12px rgba(59, 130, 246, 0.15);
  }

  :global([data-theme='dark']) .file-card.selected {
    background-color: rgba(59, 130, 246, 0.1);
    border-color: #60a5fa;
    box-shadow: 0 4px 12px rgba(96, 165, 250, 0.2);
  }

  .file-card.selecting-mode {
    user-select: none;
    -webkit-user-select: none;
  }

  /* --- Selection checkbox (top-right, Apple Photos style) --- */

  .file-selector {
    position: absolute;
    top: 6px;
    right: 6px;
    z-index: 10;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .checkmark {
    width: 18px;
    height: 18px;
    background-color: var(--background-color);
    border: 2px solid var(--border-color);
    border-radius: 4px;
    position: relative;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .file-selector:hover .checkmark {
    border-color: #3b82f6;
  }

  .file-checkbox:checked ~ .checkmark {
    background-color: #3b82f6;
    border-color: #3b82f6;
  }

  .file-checkbox {
    position: absolute;
    opacity: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    cursor: pointer;
  }

  .checkmark:after {
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

  .file-checkbox:checked ~ .checkmark:after {
    display: block;
  }

  /* --- Thumbnail container --- */

  .thumbnail-container {
    position: relative;
    width: 100%;
    height: 120px;
    overflow: hidden;
    background-color: rgba(0, 0, 0, 0.03);
  }

  :global([data-theme='dark']) .thumbnail-container {
    background-color: rgba(255, 255, 255, 0.05);
  }

  .thumbnail-image {
    width: 100%;
    height: 100%;
    object-fit: cover;
    transition: transform 0.3s ease;
  }

  .placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 100%;
    color: var(--text-secondary);
    opacity: 0.5;
  }

  .type-badge {
    position: absolute;
    top: 6px;
    left: 6px;
    width: 22px;
    height: 22px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(0, 0, 0, 0.5);
    border-radius: 4px;
    color: #fff;
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
  }

  .type-badge svg {
    width: 12px;
    height: 12px;
  }

  .duration-badge {
    position: absolute;
    bottom: 6px;
    right: 6px;
    background: rgba(0, 0, 0, 0.7);
    color: #fff;
    font-size: 0.6875rem;
    font-weight: 500;
    padding: 1px 6px;
    border-radius: 4px;
    line-height: 1.4;
    font-variant-numeric: tabular-nums;
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
  }

  /* --- Card text area --- */

  .file-card-link {
    display: flex;
    flex-direction: column;
    text-decoration: none;
    color: inherit;
    height: 100%;
  }

  .card-text {
    padding: 8px 10px 10px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    flex: 1;
  }

  .file-name {
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    line-height: 1.4;
  }

  .meta-line {
    display: flex;
    align-items: center;
    font-size: 0.6875rem;
    color: var(--text-secondary);
    line-height: 1.4;
    min-width: 0;
    white-space: nowrap;
  }

  .meta-dot {
    margin: 0 4px;
    opacity: 0.5;
  }

  /* --- Status indicator (dot + label on hover) --- */

  .status-wrap {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-left: auto;
    flex-shrink: 0;
    cursor: help;
  }

  .status-label {
    font-size: 0.5625rem;
    font-weight: 500;
    color: currentColor;
    max-width: 0;
    overflow: hidden;
    opacity: 0;
    transition: max-width 0.15s ease, opacity 0.1s ease;
    white-space: nowrap;
  }

  .status-wrap:hover .status-label {
    max-width: 120px;
    opacity: 1;
  }

  .status-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
    background-color: currentColor;
    transition: transform 0.12s ease, box-shadow 0.12s ease;
  }

  .status-wrap:hover .status-dot {
    transform: scale(1.5);
  }

  /* Status colors */
  .status-wrap.status-completed {
    color: #10b981;
  }

  .status-wrap.status-error {
    color: #ef4444;
  }

  .status-wrap.status-processing {
    color: #f59e0b;
  }

  .status-wrap.status-pending,
  .status-wrap.status-cancelling {
    color: #f59e0b;
  }

  .status-wrap.status-pending .status-dot,
  .status-wrap.status-cancelling .status-dot {
    opacity: 0.6;
  }

  .status-wrap.status-pending:hover .status-dot,
  .status-wrap.status-cancelling:hover .status-dot {
    opacity: 1;
  }

  .status-wrap.status-cancelled {
    color: #6b7280;
  }

  .status-wrap.status-orphaned {
    color: #dc2626;
  }

  /* Abuse / DMCA takedown hold — amber, distinct from the red error states. */
  .status-wrap.status-quarantined {
    color: #d97706;
  }

  .status-wrap.clickable-error {
    cursor: pointer;
  }

  @keyframes pulse {
    0% { opacity: 0.5; }
    50% { opacity: 1; }
    100% { opacity: 0.5; }
  }

  .status-wrap.status-processing .status-dot {
    animation: pulse 2s ease-in-out infinite;
  }

  .status-wrap.status-processing:hover .status-dot {
    animation: none;
    opacity: 1;
  }

  /* --- Dark mode --- */

  :global([data-theme='dark']) .file-card {
    background: var(--surface-color);
    border-color: var(--border-color);
  }

  :global([data-theme='dark']) .file-card:hover {
    border-color: var(--border-hover);
  }

  :global([data-theme='dark']) .file-name {
    color: var(--text-primary);
  }

  :global([data-theme='dark']) .meta-line {
    color: var(--text-secondary);
  }

  /* --- Responsive: Mobile --- */

  @media (max-width: 768px) {
    .file-grid {
      grid-template-columns: repeat(2, 1fr);
      gap: 0.5rem;
    }

    .thumbnail-container {
      height: 80px;
    }

    .card-text {
      padding: 6px 8px 8px;
    }

    .file-name {
      font-size: 0.75rem;
    }

    .meta-line {
      font-size: 0.625rem;
    }

    .status-line {
      font-size: 0.5625rem;
    }

    .file-card {
      border-radius: 8px;
    }

    .placeholder svg {
      width: 24px;
      height: 24px;
    }

    .duration-badge {
      font-size: 0.6rem;
      padding: 1px 4px;
    }

    .type-badge {
      width: 18px;
      height: 18px;
      top: 4px;
      left: 4px;
    }

    .type-badge svg {
      width: 10px;
      height: 10px;
    }

    .file-selector {
      top: 4px;
      right: 4px;
    }

    .status-dot {
      width: 6px;
      height: 6px;
    }

    .status-label {
      font-size: 0.5rem;
    }
  }

  @media (max-width: 380px) {
    .file-grid {
      grid-template-columns: 1fr;
    }

    .file-name {
      font-size: 0.8125rem;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .file-card,
    .file-card.new-file,
    .thumbnail-image {
      transition: none;
      animation: none;
    }

    .file-card:hover {
      transform: none;
    }

    .file-card:hover .thumbnail-image {
      transform: none;
    }
  }
</style>
