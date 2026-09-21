<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { fade } from 'svelte/transition';
  import { t } from '$stores/locale';
  import FilterSidebar from '$components/FilterSidebar.svelte';
  import type { DurationRange, DateRange } from '$lib/types/media';

  export let showFilters: boolean;
  export let searchQuery: string;
  export let selectedTags: string[];
  export let selectedSpeakers: string[];
  export let selectedCollectionId: string | null;
  export let dateRange: DateRange;
  export let durationRange: DurationRange;
  export let fileSizeRange: { min: number | null; max: number | null };
  export let selectedFileTypes: string[];
  export let selectedStatuses: string[];
  export let ownershipFilter: 'all' | 'mine' | 'shared';
  export let selectedOwners: string[] = []; // Owner UUIDs (issue #966)

  const dispatch = createEventDispatcher();

  let filterSidebarRef: any;

  // Expose collections refresh to the parent (used by the Collections modal)
  export function refreshCollections() {
    if (filterSidebarRef && filterSidebarRef.refreshCollections) {
      filterSidebarRef.refreshCollections();
    }
  }

  function toggleFilters() {
    dispatch('toggle');
  }
</script>

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
  <!-- Filters Toggle Button (always visible). A compact rotating chevron —
       see issue #750 item 1: the old control was a 40px full-width button
       carrying a sliders icon and no text, a leftover from a label that no
       longer exists. The mobile sliders-icon toggle lives separately in
       `GalleryHeader.svelte` and is unaffected. One glyph, one `transform:
       rotate()`, which is RTL-correct for free (a rotation has no
       handedness). -->
  <div class="filter-toggle-container">
    <button
      class="filter-toggle-btn {showFilters ? 'expanded' : 'collapsed'}"
      on:click={toggleFilters}
      aria-expanded={showFilters}
      title={showFilters ? $t('gallery.hideFiltersPanel') : $t('gallery.showFiltersPanel')}
      aria-label={showFilters ? $t('gallery.hideFiltersPanel') : $t('gallery.showFiltersPanel')}
    >
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
        class="toggle-chevron"
        class:rotated={showFilters}
      >
        <polyline points="9 18 15 12 9 6"></polyline>
      </svg>
    </button>
  </div>

  <!-- Filter Content (hidden when collapsed) -->
  {#if showFilters}
    <div class="filter-content">
      <FilterSidebar
        bind:this={filterSidebarRef}
        showSearchField={false}
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
        selectedOwners={selectedOwners}
        on:filter
        on:reset
      />
    </div>
  {/if}
</div>

<style>
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

  /* Compact square control, not the old 40px full-width button (#750 item 1). */
  .filter-toggle-btn {
    width: 32px;
    height: 32px;
    background-color: var(--bg-primary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
  }

  .filter-toggle-btn:hover {
    background-color: var(--hover-color);
    border-color: var(--primary-color);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
  }

  .filter-toggle-btn:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .filter-toggle-btn:active {
    transform: scale(0.94);
  }

  .toggle-chevron {
    flex-shrink: 0;
    opacity: 0.8;
    transition: transform 0.2s ease;
  }

  .toggle-chevron.rotated {
    transform: rotate(180deg);
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

  /* Narrower filter sidebar on tablet to give more room to content */
  @media (max-width: 1200px) {
    .filter-sidebar.show {
      width: 260px;
    }
  }

  /* Responsive design */
  @media (max-width: 768px) {
    .filter-sidebar {
      position: fixed;
      top: var(--content-top, 60px);
      left: -100%;
      width: 85%;
      max-width: 320px;
      height: calc(100vh - var(--content-top, 60px));
      height: calc(100dvh - var(--content-top, 60px));
      background: var(--surface-color);
      z-index: var(--z-modal);
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
  }
</style>
