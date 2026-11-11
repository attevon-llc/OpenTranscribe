<script lang="ts">
  import './collection-card.css';
  import { SHARE_PERMISSION_KEYS, labelFor } from '$lib/i18n/keyMaps';
  import { createEventDispatcher } from 'svelte';
  import { slide } from 'svelte/transition';
  import { t } from '$stores/locale';
  import SharedByAttribution from '$components/sharing/SharedByAttribution.svelte';
  import type { SharedCollection } from '$lib/types/groups';

  // Props
  export let sharedCollections: SharedCollection[] = [];
  export let viewMode: 'manage' | 'add' = 'manage';
  export let selectedMediaIds: string[] = [];
  export let selectedCollectionId: string | null = null;
  export let addingToCollection = false;

  const dispatch = createEventDispatcher<{
    select: SharedCollection;
    add: string;
  }>();
</script>

{#if sharedCollections.length > 0}
  <div class="section-label shared-label">{$t('sharing.sharedWithMe')}</div>
  {#each sharedCollections as shared (shared.uuid)}
    <div
      class="collection-card shared-card"
      class:selected={selectedCollectionId === shared.uuid}
      transition:slide
    >
      <div class="collection-info">
        <h4><button type="button" class="collection-select" on:click={() => dispatch('select', shared)}>{shared.name}</button></h4>
        {#if shared.description}
          <p class="description">{shared.description}</p>
        {/if}
        <div class="meta">
          <span class="media-count">{shared.media_count} {shared.media_count !== 1 ? $t('collectionsPanel.files') : $t('collectionsPanel.file')}</span>
          <span class="badge shared-permission">{labelFor($t, SHARE_PERMISSION_KEYS, shared.my_permission)}</span>
        </div>
        <SharedByAttribution sharedBy={shared.shared_by} />
      </div>

      {#if viewMode === 'add' && selectedMediaIds.length > 0 && shared.my_permission !== 'viewer'}
        <button
          class="btn btn-primary btn-add"
          disabled={addingToCollection}
          on:click|stopPropagation={() => dispatch('add', shared.uuid)}
        >
          {selectedMediaIds.length !== 1 ? $t('collectionsPanel.addFiles', { count: selectedMediaIds.length }) : $t('collectionsPanel.addFile', { count: selectedMediaIds.length })}
        </button>
      {/if}
    </div>
  {/each}
{/if}

<style>
  .section-label {
    font-size: 0.8rem;
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    padding: var(--space-1) 0;
  }

  .section-label.shared-label {
    margin-top: var(--space-3);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-color);
  }

  .shared-card {
    border-left: 3px solid var(--primary-color);
  }

  .badge.shared-permission {
    background: rgba(var(--accent-rgb), 0.1);
    color: var(--primary-on-surface);
    text-transform: capitalize;
  }

  /* Mobile: tap-friendly buttons */
  @media (max-width: 768px) {
    .btn-add {
      min-height: 44px;
      padding: var(--space-2) var(--space-4);
    }
  }
</style>
