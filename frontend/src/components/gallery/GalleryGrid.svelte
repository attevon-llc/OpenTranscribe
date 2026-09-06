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
      class="retry-button"
      on:click={() => dispatch('retry')}
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
