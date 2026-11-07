<!--
  GalleryHeldFilter.svelte - the computed "Legal hold" tag chip (legal-hold plan §5, Lane D).

  Not a real tag: the server computes which files are held (`held=true`), so there is nothing
  to rename, delete or remove from a file, and no menu offers it. Shown only while the caller
  can see at least one held file (`held_count`), or while the filter is on so it can be turned
  off. It names no reason and no hold: the badge on each card says who holds it.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import FilterChipButton from '$components/ui/FilterChipButton.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { heldCount, refreshHeldCount } from '$lib/legalHold/heldCount';
  import { galleryState, galleryStore } from '$stores/gallery';
  import { t } from '$stores/locale';

  // A hold placed or released elsewhere arrives as a files/metadata cache push.
  function onCacheInvalidated(event: Event) {
    const scope = (event as CustomEvent<{ scope?: string }>).detail?.scope;
    if (scope === 'metadata' || scope === 'files' || scope === 'all') refreshHeldCount();
  }

  onMount(() => {
    refreshHeldCount();
    window.addEventListener('cache-invalidated', onCacheInvalidated);
  });
  onDestroy(() => {
    if (typeof window !== 'undefined') {
      window.removeEventListener('cache-invalidated', onCacheInvalidated);
    }
  });
</script>

{#if $heldCount > 0 || $galleryState.filterHeld}
  <div class="held-filter" data-testid="held-tag-filter">
    <FilterChipButton
      class="held-tag"
      selected={$galleryState.filterHeld}
      count={$heldCount}
      showCheck
      title={$t('holds.virtual.tagTooltip')}
      on:click={() => galleryStore.setHeldFilter(!$galleryState.filterHeld)}
    >
      <span slot="icon" class="locked-icon"><MenuIcon name="shield" size={12} /></span>
      {$t('holds.virtual.tag')}
    </FilterChipButton>
  </div>
{/if}

<style>
  .held-filter {
    display: flex;
    margin-block-end: var(--space-2);
  }

  .locked-icon {
    display: inline-flex;
    color: var(--color-warning-text);
  }
</style>
