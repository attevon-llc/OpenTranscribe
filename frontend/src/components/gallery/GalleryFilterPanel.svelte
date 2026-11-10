<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { fade } from 'svelte/transition';
  import FilterSidebar from '$components/FilterSidebar.svelte';
  import FilterPanelToggle from '$components/ui/FilterPanelToggle.svelte';
  import GalleryHiddenFilter from './GalleryHiddenFilter.svelte';
  import GalleryHeldFilter from './GalleryHeldFilter.svelte';
  import type { DurationRange, DateRange } from '$lib/types/media';

  export let showFilters: boolean;
  export let searchQuery: string;
  export let selectedTags: string[];
  export let selectedSpeakers: string[];
  export let selectedCollectionId: string | null;
  export let dateRange: DateRange;
  export let durationRange: DurationRange;
  export let selectedFileTypes: string[];
  export let selectedStatuses: string[];
  export let ownershipFilter: 'all' | 'mine' | 'shared';
  export let selectedOwners: string[] = []; // Owner UUIDs (issue #966)

  const dispatch = createEventDispatcher();

  let filterSidebarRef: FilterSidebar | undefined;

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
    <FilterPanelToggle expanded={showFilters} on:click={toggleFilters} />
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
        selectedFileTypes={selectedFileTypes}
        selectedStatuses={selectedStatuses}
        ownershipFilter={ownershipFilter}
        selectedOwners={selectedOwners}
        on:filter
        on:reset
      >
        <GalleryHeldFilter slot="tags-leading" />
        <GalleryHiddenFilter />
      </FilterSidebar>
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
    transition: all var(--duration-slow) ease;
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
    padding: var(--space-2) var(--space-2) 0;
    margin-bottom: var(--space-2);
    flex-shrink: 0;
  }

  .filter-sidebar.show .filter-toggle-container {
    padding: var(--space-2) var(--space-4) 0;
  }

  .filter-content {
    flex: 1;
    overflow-y: auto;
    padding: 0 var(--space-4);
  }

  /* Mobile filter overlay backdrop - hidden on desktop */
  .filter-overlay-backdrop {
    display: none;
    position: fixed;
    top: var(--content-top);
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
      top: var(--content-top);
      left: -100%;
      width: 85%;
      max-width: 320px;
      height: calc(100vh - var(--content-top));
      height: calc(100dvh - var(--content-top));
      background: var(--surface-color);
      z-index: var(--z-modal);
      transition: left var(--duration-slow) ease;
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
