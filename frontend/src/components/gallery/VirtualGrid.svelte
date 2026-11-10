<script lang="ts">
  import { resolve } from '$app/paths';
  import { onMount, onDestroy, createEventDispatcher, tick } from 'svelte';
  import { cachedThumbnail } from '$lib/thumbnailCache';
  import { locale, t } from '$stores/locale';
  import {
    activateFileRow,
    prefetchOnMouseDown,
    uploadDate,
  } from '$lib/gallery/fileRowActions';
  import { prefetchFileDetails, cancelPrefetch } from '$lib/prefetch';
  import FileSelectCheckbox from './FileSelectCheckbox.svelte';
  import FileRowStatus from './FileRowStatus.svelte';
  import HiddenBadge from './HiddenBadge.svelte';
  import HoldBadge from '$components/ui/HoldBadge.svelte';
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
      void tick().then(() => {
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
  // role="row" groups: visibleStartIndex is always row-aligned, so chunking by
  // columnsPerRow reproduces the visual rows.
  $: visibleRowGroups = Array.from(
    { length: Math.ceil(visibleItems.length / columnsPerRow) },
    (_, r) => ({
      rowIndex: visibleStartRow + r,
      files: visibleItems.slice(r * columnsPerRow, (r + 1) * columnsPerRow),
    })
  );
  $: topSpacerHeight = visibleStartRow * ROW_HEIGHT;
  $: bottomSpacerHeight = Math.max(0, (totalRows - visibleEndRow) * ROW_HEIGHT);

  // Track which file is currently navigating to prevent double-clicks and
  // provide immediate visual feedback while the route change is in flight.
  let navigatingTo: string | null = null;

  function handleCardClick(file: MediaFile, e: MouseEvent) {
    navigatingTo = activateFileRow(file, e, isSelecting, navigatingTo);
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
      {#each visibleRowGroups as rowGroup (rowGroup.rowIndex)}
        <div class="grid-row" role="row" aria-rowindex={rowGroup.rowIndex + 1}>
          {#each rowGroup.files as file (file.uuid)}
            <div
              class="file-card {selectedFiles.has(file.uuid) ? 'selected' : ''} {pendingNewFiles.has(file.uuid) ? 'new-file' : ''} {pendingDeletions.has(file.uuid) ? 'deleting' : ''} {isSelecting ? 'selecting-mode' : ''} {navigatingTo === file.uuid ? 'navigating' : ''}"
              role="gridcell"
            >
              {#if isSelecting}
                <FileSelectCheckbox fileId={file.uuid} checked={selectedFiles.has(file.uuid)} />
              {/if}
              <a
                href={isSelecting ? '#' : resolve('/files/[id]', { id: file.uuid })}
                class="file-card-link"
                on:click={(e) => handleCardClick(file, e)}
                on:mousedown={() => prefetchOnMouseDown(file, isSelecting)}
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
                    <!-- Only this text truncates: the hold badge and status stay whole. -->
                    <span class="meta-text">
                      <span>{uploadDate(file, $locale)}</span>
                      {#if file.formatted_file_size}
                        <span class="meta-dot">&middot;</span>
                        <span dir="ltr">{file.formatted_file_size}</span>
                      {/if}
                      {#if file.speaker_summary && file.speaker_summary.count > 0}
                        <span class="meta-dot">&middot;</span>
                        <span>{$t('gallery.speakerCountShort', { count: file.speaker_summary.count })}</span>
                      {/if}
                    </span>
                    <HoldBadge hold={file.hold} />
                    <span class="status-slot">
                      <FileRowStatus {file} on:errorclick={(e) => handleErrorClick(e.detail)} />
                    </span>
                  </div>
                </div>
              </a>
              {#if file.is_hidden}
                <div class="card-hidden" class:selecting={isSelecting}>
                  <HiddenBadge {file} />
                </div>
              {/if}
            </div>
          {/each}
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
    padding-top: var(--space-2);
    margin-top: calc(-1 * var(--space-2));
  }

  /* Row wrappers exist for ARIA only; the cards still lay out in .file-grid. */
  .grid-row {
    display: contents;
  }

  .file-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: var(--space-3);
    padding: 0 var(--space-0-5) var(--space-2);
  }

  /* --- Card --- */

  .file-card {
    position: relative;
    border: 1px solid var(--border-color);
    background-color: var(--surface-color);
    border-radius: var(--radius-2xl);
    transition: transform var(--duration-base) ease, box-shadow var(--duration-base) ease, border-color var(--duration-base) ease;
    cursor: pointer;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transform: translateZ(0);
  }

  .file-card.new-file {
    animation: new-file-glow 0.6s ease-out;
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

  .file-card-link {
    display: flex;
    flex-direction: column;
    text-decoration: none;
    color: inherit;
    height: 100%;
  }

  .file-card.navigating .file-card-link {
    opacity: 0.72;
    transform: scale(0.985);
    transition: opacity 0.12s ease, transform 0.12s ease;
  }

  @keyframes new-file-glow {
    0% {
      transform: scale(1);
      box-shadow: 0 0 15px rgba(var(--info-color-rgb), 0.4);
      border-color: var(--info-color);
    }
    100% {
      transform: scale(1);
      box-shadow: none;
      border-color: var(--border-color);
    }
  }

  .file-card:hover {
    transform: translateY(-2px);
    box-shadow: var(--dropdown-shadow);
    border-color: var(--border-hover);
  }

  .thumbnail-image {
    width: 100%;
    height: 100%;
    object-fit: cover;
    transition: transform var(--duration-slow) ease;
  }

  .file-card:hover .thumbnail-image {
    transform: scale(1.03);
  }

  .file-card.selected {
    border: 2px solid var(--info-color);
    background-color: rgba(var(--info-color-rgb), 0.05);
    box-shadow: 0 4px 12px rgba(var(--info-color-rgb), 0.15);
  }

  /* A blue tint needs more alpha on the dark surface to read as selected. */
  :global([data-theme='dark']) .file-card.selected {
    background-color: rgba(var(--info-color-rgb), 0.1);
  }

  .file-card.selecting-mode {
    user-select: none;
    -webkit-user-select: none;
  }

  /* --- Selection checkbox (top-right, Apple Photos style) --- */

  /* The "Hidden" pill (issue #1102) sits in the thumbnail's top corner, beside the
     selection checkbox when there is one. Logical insets so it follows RTL. */
  .card-hidden {
    position: absolute;
    top: var(--space-1-5);
    inset-inline-end: var(--space-1-5);
    z-index: var(--z-sticky);
    padding: var(--space-0-5) var(--space-1-5);
    border-radius: var(--radius-2xl);
    background-color: var(--surface-color);
    box-shadow: var(--shadow-sm);
  }

  .card-hidden.selecting {
    inset-inline-end: var(--space-8);
  }

  /* --- Thumbnail container --- */

  .thumbnail-container {
    position: relative;
    width: 100%;
    height: 120px;
    overflow: hidden;
    background-color: var(--surface-hover);
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
    background: var(--player-scrim);
    border-radius: var(--radius-sm);
    color: var(--text-on-solid);
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
    background: var(--player-scrim);
    color: var(--text-on-solid);
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-medium);
    padding: 1px var(--space-1-5);
    border-radius: var(--radius-sm);
    line-height: 1.4;
    font-variant-numeric: tabular-nums;
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
  }

  /* --- Card text area --- */

  .card-text {
    padding: var(--space-2) var(--space-2-5) var(--space-2-5);
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
    flex: 1;
  }

  .file-name {
    font-size: var(--font-size-sm);
    /* Was 600 — the over-bold outlier issue #749 names (the list's `.file-title` is
       already 500; this is the view that needed fixing, not the list). */
    font-weight: var(--font-weight-medium);
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
    font-size: var(--font-size-2xs);
    color: var(--text-secondary);
    line-height: 1.4;
    min-width: 0;
    white-space: nowrap;
  }

  .meta-dot {
    margin: 0 var(--space-1);
    opacity: 0.5;
  }

  /* Positions the shared FileStatusIndicator at the end of the meta line — the
     hover-reveal system that used to live here (`.status-wrap`/`.status-label`,
     `max-width: 0 -> 120px`, `cursor: help` with no `title`) is gone; the
     indicator now carries a real native tooltip everywhere (§5.4). */
  .meta-text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .meta-line :global(.badge) {
    flex-shrink: 0;
    margin-inline-start: var(--space-1);
  }

  .status-slot {
    margin-inline-start: auto;
    flex-shrink: 0;
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
      gap: var(--space-2);
    }

    .thumbnail-container {
      height: 80px;
    }

    .card-text {
      padding: var(--space-1-5) var(--space-2) var(--space-2);
    }

    .file-name {
      font-size: var(--font-size-xs);
    }

    .meta-line {
      font-size: 0.625rem;
    }

    .file-card {
      border-radius: var(--radius-lg);
    }

    .placeholder svg {
      width: 24px;
      height: 24px;
    }

    .duration-badge {
      font-size: 0.6rem;
      padding: 1px var(--space-1);
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
  }

  @media (max-width: 380px) {
    .file-grid {
      grid-template-columns: 1fr;
    }

    .file-name {
      font-size: var(--font-size-sm);
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
