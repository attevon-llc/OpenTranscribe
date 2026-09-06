<script lang="ts">
  import type { Collection } from '$lib/types/collection';
  import { slide } from 'svelte/transition';
  import { createEventDispatcher } from 'svelte';
  import CollectionsEditor from './CollectionsEditor.svelte';
  import { t } from '$stores/locale';

  export let collections: Collection[] = [];
  export let isExpanded: boolean = false;
  export let fileId: string;
  export let aiCollectionSuggestions: Array<{name: string, confidence: number, rationale?: string, description?: string}> = [];

  const dispatch = createEventDispatcher();

  function toggleExpanded() {
    isExpanded = !isExpanded;
  }

  function handleCollectionsUpdated(event: any) {
    // Re-emit the event to parent component
    collections = event.detail.collections;
    dispatch('collectionsUpdated', { collections: event.detail.collections });
  }
</script>

<div class="collections-dropdown-section">
  <button
    class="collections-header"
    on:click={toggleExpanded}
    on:keydown={e => e.key === 'Enter' && toggleExpanded()}
    title={$t('collectionsSection.toggleTooltip')}
    aria-expanded={isExpanded}
  >
    <h4 class="section-heading">{$t('collectionsSection.title')}</h4>
    <div class="collections-preview">
      {#if collections && collections.length > 0}
        {#each collections.slice(0, 3) as collection}
          <span class="collection-chip">{collection.name || $t('collectionsSection.unnamed')}</span>
        {/each}
        {#if collections.length > 3}
          <span class="collection-chip more">{$t('collectionsSection.moreCount', { count: collections.length - 3 })}</span>
        {/if}
      {:else}
        <span class="no-collections">{$t('collectionsSection.noCollections')}</span>
      {/if}
    </div>
    <span class="dropdown-toggle" aria-hidden="true">
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="transform: rotate({isExpanded ? '180deg' : '0deg'})">
        <polyline points="6 9 12 15 18 9"></polyline>
      </svg>
    </span>
  </button>

  {#if isExpanded}
    <div class="collections-content" transition:slide={{ duration: 200 }}>
      {#if fileId}
        <CollectionsEditor
          {fileId}
          {collections}
          aiSuggestions={aiCollectionSuggestions}
          on:collectionsUpdated={handleCollectionsUpdated}
        />
      {:else}
        <p>{$t('collectionsSection.loading')}</p>
      {/if}
    </div>
  {/if}
</div>

<style>
  .collections-dropdown-section {
    margin-bottom: 0;
  }

  .collections-header {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 12px 16px;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .collections-header:hover {
    background: var(--surface-hover);
    border-color: var(--border-hover);
  }

  .section-heading {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .collections-preview {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    flex-wrap: wrap;
    flex: 1;
    margin: 0 12px;
  }

  .collection-chip {
    background: var(--primary-light);
    color: var(--primary-on-surface);
    padding: 3px 8px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 500;
    display: inline-block;
  }

  .collection-chip.more {
    background: var(--surface-secondary);
    color: var(--text-secondary);
  }

  .no-collections {
    color: var(--text-secondary);
    font-size: 14px;
  }

  .dropdown-toggle svg {
    transition: transform 0.2s ease;
  }

  .collections-content {
    border: 1px solid var(--border-color);
    border-top: none;
    border-radius: 0 0 8px 8px;
    background: var(--surface-color);
    padding: 20px;
  }

  .collections-content p {
    margin: 0;
    color: var(--text-secondary);
    font-style: italic;
    text-align: center;
    padding: 20px;
  }

  /* Dark mode support */
  :global([data-theme='dark']) .collection-chip {
    background: rgba(59, 130, 246, 0.2);
    color: #93bbfc;
  }

  :global([data-theme='dark']) .collection-chip.more {
    background: rgba(255, 255, 255, 0.1);
  }
</style>
