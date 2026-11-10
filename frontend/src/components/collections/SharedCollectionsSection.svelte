<script lang="ts">
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
      role="button"
      tabindex="0"
      on:click={() => dispatch('select', shared)}
      on:keydown={(e) => (e.key === 'Enter' || e.key === ' ') && dispatch('select', shared)}
      transition:slide
    >
      <div class="collection-info">
        <h4>{shared.name}</h4>
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
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-semibold);
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .description {
    margin: 0 0 var(--space-2) 0;
    font-size: var(--font-size-md);
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
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .media-count {
    font-weight: var(--font-weight-medium);
  }

  .badge {
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-2xl);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
  }

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

  :global([data-theme='dark']) .badge.shared-permission {
    background: rgba(var(--accent-rgb), 0.15);
    color: #93c5fd;
  }

  :global([data-theme='dark']) .collection-card {
    background: var(--card-background);
    border-color: var(--border-color);
  }

  /* Mobile: tap-friendly buttons */
  @media (max-width: 768px) {
    .btn-add {
      min-height: 44px;
      padding: var(--space-2) var(--space-4);
    }
  }
</style>
