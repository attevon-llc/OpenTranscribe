<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import GalleryCountChip from '$components/gallery/GalleryCountChip.svelte';
  import SortDropdown, { type SortOption } from '$components/ui/SortDropdown.svelte';
  import GalleryViewToggle from '$components/gallery/GalleryViewToggle.svelte';
  import GalleryActionButtons from '$components/gallery/GalleryActionButtons.svelte';
  import type { MediaFile } from '$lib/types/media';

  const sortOptions: SortOption[] = [
    { value: 'upload_time', label: 'gallery.sort.uploadDate' },
    { value: 'completed_at', label: 'gallery.sort.completedDate' },
    { value: 'filename', label: 'gallery.sort.filename' },
    { value: 'duration', label: 'gallery.sort.duration' },
    { value: 'file_size', label: 'gallery.sort.fileSize' },
  ];

  export let files: MediaFile[] = [];
  export let sortBy: string;
  export let sortOrder: 'asc' | 'desc';
  export let loading: boolean;
  export let showFilters: boolean;

  const dispatch = createEventDispatcher();

  function toggleFilters() {
    dispatch('togglefilters');
  }
</script>

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
      <SortDropdown
        {sortOptions}
        {sortBy}
        {sortOrder}
        ariaLabelKey="gallery.sort.label"
        on:change
      />
      <GalleryViewToggle />
      <GalleryCountChip loading={loading} filesLoaded={files.length} />
    </div>
  {/if}
</div>

<style>
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
</style>
