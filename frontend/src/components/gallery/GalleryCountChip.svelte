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
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
    backdrop-filter: blur(8px);
    transition: all var(--duration-base) ease;
    user-select: none;
  }

  :global([data-theme='light']) .count-chip,
  :global(:not([data-theme='dark'])) .count-chip {
    background: rgba(255, 255, 255, 0.9);
    border-color: rgba(0, 0, 0, 0.08);
    color: #6b7280;
  }

  :global([data-theme='dark']) .count-chip {
    background: rgba(30, 41, 59, 0.95);
    border-color: rgba(255, 255, 255, 0.1);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
    color: #cbd5e1;
  }

  @media (max-width: 768px) {
    .count-chip {
      font-size: var(--font-size-xs);
      padding: 0.3rem var(--space-2-5);
      gap: 0.3rem;
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
