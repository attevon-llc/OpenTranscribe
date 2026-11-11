<script context="module" lang="ts">
  import './collection-card.css';
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
    transition:slide
  >
    <div class="collection-info">
      <h4><button type="button" class="collection-select" on:click={() => dispatch('select', collection)}>{collection.name}</button></h4>
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
          class="btn btn-secondary share-button"
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
          class="btn btn-secondary edit-button"
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
            class="btn btn-danger-outline delete-config-button"
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
        class="btn btn-primary btn-add"
        disabled={addingToCollection}
        on:click|stopPropagation={() => dispatch('add', collection.uuid)}
      >
        {selectedMediaIds.length !== 1 ? $t('collectionsPanel.addFiles', { count: selectedMediaIds.length }) : $t('collectionsPanel.addFile', { count: selectedMediaIds.length })}
      </button>
    {/if}
  </div>
{/each}

<style>
  .badge.restricted {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    background: var(--surface-secondary);
    color: var(--text-secondary);
    border: 1px solid var(--border-color);
  }

  .badge.public {
    background: var(--success-bg);
    color: var(--success-text);
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
