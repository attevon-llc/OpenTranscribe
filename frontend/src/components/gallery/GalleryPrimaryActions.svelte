<script lang="ts">
  import { galleryStore, galleryState } from '../../stores/gallery';
  import { t } from '../../stores/locale';

  // Normal-mode-only trio (issue #747): row 1 right of the two-row toolbar.
  // "Select items" is NOT here — it lives in `GallerySelectionActions`, beside
  // the count chip in row 2, because entering selection mode expands it in
  // place into the selection toolbar rather than replacing this whole group.
  function handleUploadClick() { galleryStore.triggerUpload(); }
  function handleCollectionsClick() { galleryStore.triggerCollections(); }
  function handleTagsClick() { galleryStore.triggerTags(); }
</script>

{#if !$galleryState.isSelecting}
  <div class="gallery-primary-actions">
    <button
      class="action-btn upload-btn"
      on:click={handleUploadClick}
      title={$t('gallery.bulk.addMediaTooltip')}
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        <polyline points="17 8 12 3 7 8"></polyline>
        <line x1="12" y1="3" x2="12" y2="15"></line>
      </svg>
      <span>{$t('nav.addMedia')}</span>
    </button>
    <button
      class="action-btn collections-btn"
      on:click={handleCollectionsClick}
      title={$t('gallery.bulk.collectionsTooltip')}
    >
      <span>{$t('nav.collections')}</span>
    </button>
    <button
      class="action-btn tags-btn"
      on:click={handleTagsClick}
      title={$t('gallery.bulk.tagsTooltip')}
    >
      <span>{$t('nav.tags')}</span>
    </button>
  </div>
{/if}

<style>
  .gallery-primary-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .action-btn {
    color: white;
    border: none;
    padding: 0.375rem 0.75rem;
    border-radius: 10px;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 0.35rem;
    white-space: nowrap;
    flex-shrink: 0;
    font-family: inherit;
  }

  .action-btn:hover:not(:disabled) {
    transform: scale(1.02);
  }

  .action-btn:active:not(:disabled) {
    transform: scale(1);
  }

  .action-btn svg {
    width: 14px;
    height: 14px;
    flex-shrink: 0;
  }

  /* Primary action — solid blue */
  .upload-btn {
    background-color: var(--primary-color, var(--primary-color));
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .upload-btn:hover:not(:disabled) {
    background-color: var(--primary-hover, #2563eb);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  /* Secondary actions — surface with border (not colored) */
  .collections-btn,
  .tags-btn {
    background-color: var(--surface-color);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
  }

  .collections-btn:hover:not(:disabled),
  .tags-btn:hover:not(:disabled) {
    background-color: var(--button-hover, #f1f5f9);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.08);
  }

  :global([data-theme='dark']) .collections-btn:hover:not(:disabled),
  :global([data-theme='dark']) .tags-btn:hover:not(:disabled) {
    background-color: rgba(255, 255, 255, 0.08);
  }

  @media (max-width: 1200px) {
    .gallery-primary-actions {
      flex-wrap: wrap;
      gap: 0.375rem;
    }

    .action-btn {
      padding: 0.35rem 0.65rem;
      font-size: 0.8rem;
    }
  }

  @media (max-width: 768px) {
    .action-btn svg {
      width: 16px;
      height: 16px;
    }
  }

  @media (max-width: 480px) {
    .action-btn {
      padding: 0.35rem 0.6rem;
      font-size: 0.75rem;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .action-btn {
      transition: none;
    }

    .action-btn:hover:not(:disabled) {
      transform: none;
    }
  }
</style>
