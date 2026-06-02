<script lang="ts">
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
          <span class="badge shared-permission">{$t('sharing.permission' + shared.my_permission.charAt(0).toUpperCase() + shared.my_permission.slice(1))}</span>
        </div>
        <SharedByAttribution sharedBy={shared.shared_by} />
      </div>

      {#if viewMode === 'add' && selectedMediaIds.length > 0 && shared.my_permission !== 'viewer'}
        <button
          class="btn-add"
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

  .section-label {
    font-size: 0.8rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
    padding: 4px 0;
  }

  .section-label.shared-label {
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--border-color);
  }

  .shared-card {
    border-left: 3px solid var(--primary-color);
  }

  .badge.shared-permission {
    background: rgba(59, 130, 246, 0.1);
    color: var(--primary-color, #3b82f6);
    text-transform: capitalize;
  }

  :global([data-theme='dark']) .badge.shared-permission {
    background: rgba(59, 130, 246, 0.15);
    color: #93c5fd;
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

  /* Mobile: tap-friendly buttons */
  @media (max-width: 768px) {
    .btn-add {
      min-height: 44px;
      padding: 0.5rem 1rem;
    }
  }
</style>
