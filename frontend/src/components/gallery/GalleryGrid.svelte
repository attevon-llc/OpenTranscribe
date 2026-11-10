<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { fade } from 'svelte/transition';
  import { t } from '$stores/locale';
  import { hasMoreFiles, isLoadingMore, galleryViewMode } from '$stores/gallery';
  import CardGridSkeleton from '$components/ui/CardGridSkeleton.svelte';
  import FileListSkeleton from './FileListSkeleton.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import VirtualList from '$components/gallery/VirtualList.svelte';
  import VirtualGrid from '$components/gallery/VirtualGrid.svelte';
  import type { MediaFile } from '$lib/types/media';

  export let files: MediaFile[] = [];
  export let loading: boolean;
  export let error: string | null;
  export let selectedCollectionId: string | null;
  export let isSelecting: boolean;
  export let selectedFiles: Set<string>;
  export let pendingNewFiles: Set<string>;
  export let pendingDeletions: Set<string>;
  export let scrollContainer: HTMLElement | null = null;
  /**
   * Whether any filter (search, tags, dates, …) is currently narrowing the
   * list. An empty `files` array with a filter active means "no matches", not
   * "your library is empty" — issue #747 §4.1 defect 3: this used to always
   * say the library was empty, even with 500 files in it and one active filter.
   */
  export let filtersActive = false;

  const dispatch = createEventDispatcher();

  // Infinite scroll sentinel — bound here, surfaced to the parent so its
  // IntersectionObserver can observe it for "load more" triggering.
  let infiniteScrollSentinel: HTMLElement | null = null;
  $: dispatch('sentinel', infiniteScrollSentinel);
</script>

{#if loading}
  <!-- The placeholder must match the view the data will land in. This rendered
       card placeholders unconditionally, so the LIST view showed a grid of
       cards and then visibly jumped to a table once the files arrived. The
       real content branch below already keys off `$galleryViewMode`; this one
       simply never did. -->
  {#if $galleryViewMode === 'list'}
    <FileListSkeleton count={8} {isSelecting} />
  {:else}
    <CardGridSkeleton variant="media" count={12} />
  {/if}
{:else if error}
  <div class="error-state">
    <p>{$t('gallery.connectionError')}</p>
    <button
      class="btn btn-primary retry-button"
      on:click={() => dispatch('retry')}
      title={$t('gallery.retryTooltip')}
    >{$t('gallery.retry')}</button>
  </div>
{:else if files.length === 0}
  <EmptyState
    title={selectedCollectionId
      ? $t('gallery.noFilesInCollection')
      : filtersActive
        ? $t('gallery.noFilesMatch')
        : $t('gallery.libraryEmpty')}
    description={!selectedCollectionId && filtersActive
      ? $t('gallery.noFilesMatchDescription')
      : $t('gallery.uploadFirstFile')}
  />
{:else}
  <!-- E2E readiness oracle (issue #747 §4.5): real content has rendered — not
       skeleton, not empty, not error. Replaces the old `.gallery-header-right`
       wait, which was gated on `files.length > 0` and disappeared entirely once
       that gate was removed from GalleryHeader. See gallery/CLAUDE.md and
       backend/tests/e2e/conftest.py's `gallery_page` fixture. -->
  <div data-testid="gallery-files-loaded">
    {#if $galleryViewMode === 'list'}
      <!-- List View (Virtual Scrolling) -->
      <VirtualList
        items={files}
        {scrollContainer}
        {isSelecting}
        {selectedFiles}
        {pendingNewFiles}
        {pendingDeletions}
        on:errorclick
      />
    {:else}
      <!-- Grid View (Virtual Scrolling) -->
      <VirtualGrid
        items={files}
        {scrollContainer}
        {isSelecting}
        {selectedFiles}
        {pendingNewFiles}
        {pendingDeletions}
        on:errorclick
      />
    {/if}
  </div>

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

<style>
  .error-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 400px;
    text-align: center;
    color: var(--error-color);
  }

  /* Infinite scroll sentinel - invisible trigger element */
  .scroll-sentinel {
    height: 20px;
    margin-top: var(--space-8);
    pointer-events: none;
  }

  /* Loading more indicator */
  .loading-more {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--space-8);
    gap: var(--space-4);
  }

  .loading-more p {
    color: var(--text-secondary);
    font-size: 0.9rem;
    margin: 0;
  }

  /* End of Results Indicator */
  .end-of-results {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-4);
    padding: var(--space-12) var(--space-4);
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
    gap: var(--space-2);
    padding: var(--space-2) var(--space-4);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 20px;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  .end-indicator-content svg {
    flex-shrink: 0;
    color: var(--success-color);
  }
</style>
