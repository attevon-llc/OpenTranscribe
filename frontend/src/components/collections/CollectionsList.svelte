<script context="module" lang="ts">
  // Re-exported for the components that already import it from here; the
  // canonical declaration lives in $lib/types/collection.
  export type { Collection } from '$lib/types/collection';
</script>

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { slide } from 'svelte/transition';
  import { t } from '$stores/locale';
  import ShareBadge from '$components/sharing/ShareBadge.svelte';
  import type { Collection } from '$lib/types/collection';

  // Props
  export let collections: Collection[] = [];
  export let viewMode: 'manage' | 'add' = 'manage';
  export let selectedMediaIds: string[] = [];
  export let selectedCollectionId: string | null = null;
  export let addingToCollection = false;

  const dispatch = createEventDispatcher<{
    select: Collection;
    edit: Collection;
    delete: Collection;
    share: Collection;
    add: string;
  }>();
</script>

{#if collections.length > 0}
  <div class="section-label">{$t('sharing.myCollections')}</div>
{/if}
{#each collections as collection (collection.uuid)}
  <div
    class="collection-card"
    class:selected={selectedCollectionId === collection.uuid}
    role="button"
    tabindex="0"
    on:click={() => dispatch('select', collection)}
    on:keydown={(e) => (e.key === 'Enter' || e.key === ' ') && dispatch('select', collection)}
    transition:slide
  >
    <div class="collection-info">
      <h4>{collection.name}</h4>
      {#if collection.description}
        <p class="description">{collection.description}</p>
      {/if}
      <div class="meta">
        <span class="media-count">{collection.media_count} {collection.media_count !== 1 ? $t('collectionsPanel.files') : $t('collectionsPanel.file')}</span>
        {#if collection.is_public}
          <span class="badge public">{$t('collectionsPanel.public')}</span>
        {/if}
        {#if collection.default_prompt_name}
          <span class="badge prompt">{collection.default_prompt_name}</span>
        {/if}
        {#if (collection.share_count ?? 0) > 0}
          <ShareBadge shareCount={collection.share_count} isShared={true} />
        {/if}
      </div>
    </div>

    {#if viewMode === 'manage'}
      <div class="collection-actions">
        <button
          class="share-button"
          title={$t('sharing.shareCollection')}
          on:click|stopPropagation={() => dispatch('share', collection)}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/>
            <polyline points="16 6 12 2 8 6"/>
            <line x1="12" y1="2" x2="12" y2="15"/>
          </svg>
        </button>
        <button
          class="edit-button"
          title={$t('collectionsPanel.editCollection')}
          on:click|stopPropagation={() => dispatch('edit', collection)}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
            <path d="m18.5 2.5 a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
          </svg>
        </button>
        <button
          class="delete-config-button"
          title={$t('collectionsPanel.deleteCollection')}
          on:click|stopPropagation={() => dispatch('delete', collection)}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="3,6 5,6 21,6"/>
            <path d="m19,6v14a2,2 0 0,1 -2,2H7a2,2 0 0,1 -2,-2V6m3,0V4a2,2 0 0,1 2,-2h4a2,2 0 0,1 2,2v2"/>
            <line x1="10" y1="11" x2="10" y2="17"/>
            <line x1="14" y1="11" x2="14" y2="17"/>
          </svg>
        </button>
      </div>
    {:else if viewMode === 'add' && selectedMediaIds.length > 0}
      <button
        class="btn-add"
        disabled={addingToCollection}
        on:click|stopPropagation={() => dispatch('add', collection.uuid)}
      >
        {selectedMediaIds.length !== 1 ? $t('collectionsPanel.addFiles', { count: selectedMediaIds.length }) : $t('collectionsPanel.addFile', { count: selectedMediaIds.length })}
      </button>
    {/if}
  </div>
{/each}

<style>
  .collection-card {
    background: var(--card-background);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 16px;
    cursor: pointer;
    transition: all 0.2s;
    display: flex;
    justify-content: space-between;
    align-items: start;
    gap: 12px;
  }

  .collection-card:hover {
    border-color: var(--primary-color);
    background: var(--card-hover);
  }

  .collection-card.selected {
    border-color: var(--primary-color);
    background: var(--primary-bg, rgba(59, 130, 246, 0.05));
  }

  .collection-info {
    flex: 1;
    min-width: 0;
  }

  .collection-info h4 {
    margin: 0 0 4px 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .description {
    margin: 0 0 8px 0;
    font-size: 14px;
    color: var(--text-secondary);
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .meta {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .media-count {
    font-weight: 500;
  }

  .badge {
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 500;
  }

  .badge.public {
    background: rgba(34, 197, 94, 0.1);
    color: rgb(34, 197, 94);
  }

  .badge.prompt {
    background: rgba(59, 130, 246, 0.1);
    color: var(--primary-color);
  }

  .collection-actions {
    display: flex;
    gap: 8px;
  }

  .section-label {
    font-size: 0.8rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
    padding: 4px 0;
  }

  .share-button {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    font-size: 0.8rem;
    cursor: pointer;
    transition: all 0.2s ease;
    border: 1px solid;
    background-color: transparent;
    border-color: var(--border-color);
    color: var(--text-color);
  }

  .share-button:hover:not(:disabled) {
    background-color: #3b82f6;
    border-color: var(--primary-color);
    color: white;
  }

  .edit-button, .delete-config-button {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    font-size: 0.8rem;
    cursor: pointer;
    transition: all 0.2s ease;
    border: 1px solid;
  }

  .edit-button {
    background-color: transparent;
    border-color: var(--border-color);
    color: var(--text-color);
  }

  .edit-button:hover:not(:disabled) {
    background-color: #3b82f6;
    border-color: var(--primary-color);
    color: white;
  }

  .delete-config-button {
    background-color: transparent;
    border-color: var(--error-color);
    color: var(--error-color);
  }

  .delete-config-button:hover:not(:disabled) {
    background-color: var(--error-color);
    border-color: var(--error-color);
    color: white;
    transform: scale(1.02);
  }

  .btn-add {
    padding: 6px 12px;
    background: #3b82f6;
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
  }

  .btn-add:hover:not(:disabled) {
    background: #2563eb;
  }

  .btn-add:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  :global([data-theme='dark']) .collection-card {
    background: var(--card-background);
    border-color: var(--border-color);
  }

  :global([data-theme='dark']) .badge.public {
    background: rgba(34, 197, 94, 0.2);
    color: #4ade80;
  }

  :global([data-theme='dark']) .badge.prompt {
    background: rgba(59, 130, 246, 0.2);
    color: #60a5fa;
  }

  /* Mobile: tap-friendly buttons */
  @media (max-width: 768px) {
    .collection-actions button {
      min-width: 44px;
      min-height: 44px;
    }

    .btn-add {
      min-height: 44px;
      padding: 0.5rem 1rem;
    }
  }
</style>
