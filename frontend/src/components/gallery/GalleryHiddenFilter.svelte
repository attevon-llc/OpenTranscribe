<!--
  GalleryHiddenFilter.svelte - the gallery's "Hidden files" chips (issue #1102).

  Leave out (the default) / Include / Only hidden. Hiding is the caller's own view preference,
  so this section lives only on the gallery, the one list that can show hidden files on
  request; search always leaves them out. The mode lives in the gallery store, which reloads
  the list when it changes and puts it back to "Leave out" on reset.
-->
<script lang="ts">
  import FilterChipButton from '$components/ui/FilterChipButton.svelte';
  import { galleryState, galleryStore } from '$stores/gallery';
  import { t } from '$stores/locale';
  import type { HiddenMode } from '$lib/api/hiddenMedia';

  const MODES: { mode: HiddenMode; key: string }[] = [
    { mode: 'exclude', key: 'filter.hiddenExclude' },
    { mode: 'include', key: 'filter.hiddenInclude' },
    { mode: 'only', key: 'filter.hiddenOnly' },
  ];

  function choose(mode: HiddenMode) {
    if (mode !== $galleryState.filterHiddenMode) galleryStore.setHiddenMode(mode);
  }
</script>

<div class="hidden-filter" data-testid="hidden-filter">
  <h3>{$t('filter.hiddenFiles')}</h3>
  <div class="hidden-mode-list">
    {#each MODES as { mode, key } (mode)}
      <FilterChipButton
        selected={$galleryState.filterHiddenMode === mode}
        on:click={() => choose(mode)}
      >
        {$t(key)}
      </FilterChipButton>
    {/each}
  </div>
  <span class="hidden-help">{$t('filter.hiddenHelp')}</span>
</div>

<style>
  /* Matches FilterSidebar's .filter-section: a divider above, a small secondary heading. */
  .hidden-filter {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-color);
  }

  h3 {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    margin: 0;
  }

  .hidden-mode-list {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .hidden-help {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    font-style: italic;
  }
</style>
