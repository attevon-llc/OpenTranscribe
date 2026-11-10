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
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import HoldBadge from '$components/ui/HoldBadge.svelte';
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
        {#if collection.visibility === 'restricted'}
          <span class="badge restricted" title={$t('collectionsPanel.restrictedBadgeTitle')}>
            <MenuIcon name="lock" size={12} />
            {$t('collectionsPanel.restrictedBadge')}
          </span>
        {/if}
        <HoldBadge hold={collection.hold} />
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
        <!-- An organization's collections are listed to every member, but only
             their creator or an org admin (my_permission "owner") may delete one. -->
        {#if (collection.my_permission ?? 'owner') === 'owner'}
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
        {/if}
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
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    cursor: pointer;
    transition: all var(--duration-base);
    display: flex;
    justify-content: space-between;
    align-items: start;
    gap: var(--space-3);
  }

  .collection-card:hover {
    border-color: var(--primary-color);
    background: var(--card-hover);
  }

  .collection-card.selected {
    border-color: var(--primary-color);
    background: var(--primary-bg);
  }

  .collection-info {
    flex: 1;
    min-width: 0;
  }

  .collection-info h4 {
    margin: 0 0 var(--space-1) 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .description {
    margin: 0 0 var(--space-2) 0;
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
    gap: var(--space-3);
    font-size: 13px;
    color: var(--text-secondary);
  }

  .media-count {
    font-weight: 500;
  }

  .badge {
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-2xl);
    font-size: 12px;
    font-weight: 500;
  }

  .badge.restricted {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    background: var(--surface-secondary);
    color: var(--text-secondary);
    border: 1px solid var(--border-color);
  }

  .badge.public {
    background: rgba(34, 197, 94, 0.1);
    color: rgb(34, 197, 94);
  }

  .badge.prompt {
    background: rgba(var(--accent-rgb), 0.1);
    color: var(--primary-on-surface);
  }

  .collection-actions {
    display: flex;
    gap: var(--space-2);
  }

  .section-label {
    font-size: 0.8rem;
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    padding: var(--space-1) 0;
  }

  .share-button {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-sm);
    font-size: 0.8rem;
    cursor: pointer;
    transition: all var(--duration-base) ease;
    border: 1px solid;
    background-color: transparent;
    border-color: var(--border-color);
    color: var(--text-color);
  }

  .share-button:hover:not(:disabled) {
    background-color: var(--primary-color);
    border-color: var(--primary-color);
    color: var(--text-on-solid);
  }

  .edit-button, .delete-config-button {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-sm);
    font-size: 0.8rem;
    cursor: pointer;
    transition: all var(--duration-base) ease;
    border: 1px solid;
  }

  .edit-button {
    background-color: transparent;
    border-color: var(--border-color);
    color: var(--text-color);
  }

  .edit-button:hover:not(:disabled) {
    background-color: var(--primary-color);
    border-color: var(--primary-color);
    color: var(--text-on-solid);
  }

  .delete-config-button {
    background-color: transparent;
    border-color: var(--error-color);
    color: var(--error-color);
  }

  .delete-config-button:hover:not(:disabled) {
    background-color: var(--error-color);
    border-color: var(--error-color);
    color: var(--text-on-solid);
    transform: scale(1.02);
  }

  .btn-add {
    padding: var(--space-1-5) var(--space-3);
    background: var(--primary-color);
    color: var(--text-on-solid);
    border: none;
    border-radius: var(--radius-md);
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: all var(--duration-base);
    white-space: nowrap;
  }

  .btn-add:hover:not(:disabled) {
    background: var(--primary-color);
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
    background: rgba(var(--accent-rgb), 0.2);
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
      padding: var(--space-2) var(--space-4);
    }
  }
</style>
