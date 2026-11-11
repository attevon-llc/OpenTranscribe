<script lang="ts">
  import { galleryState } from '$stores/gallery';
  import { t } from '$stores/locale';
  import { fade, scale } from 'svelte/transition';

  export let loading: boolean = false;
  export let filesLoaded: number = 0;

  $: displayText = getDisplayText($galleryState.totalFiles, filesLoaded, loading);

  function getDisplayText(total: number, loaded: number, isLoading: boolean): string {
    if (isLoading && loaded === 0) {
      return $t('gallery.loadingCount');
    }
    if (total === 0) {
      return $t('gallery.noFilesMatch');
    }
    if (loaded === total) {
      return `${total}`;
    }
    return `${loaded} / ${total}`;
  }
</script>

{#if !loading || filesLoaded > 0}
  <div class="pill-chip count-chip" in:scale={{ duration: 200 }} out:fade={{ duration: 150 }}>
    <span>{displayText}</span>
  </div>
{/if}

<style>
  .count-chip {
    background: color-mix(in srgb, var(--surface-color) 90%, transparent);
    border-color: var(--border-color);
    color: var(--text-secondary);
    box-shadow: var(--shadow-md);
    backdrop-filter: blur(8px);
    transition: all var(--duration-base) ease;
    user-select: none;
  }

  @media (max-width: 768px) {
    .count-chip {
      font-size: var(--font-size-xs);
      padding: var(--space-1) var(--space-2-5);
      gap: var(--space-1);
    }

    .count-chip span {
      max-width: 120px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  }

  @media (max-width: 480px) {
    .count-chip {
      font-size: var(--font-size-2xs);
      padding: var(--space-1) var(--space-2);
    }
  }
</style>
