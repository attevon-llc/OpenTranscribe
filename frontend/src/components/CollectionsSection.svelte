<script lang="ts">
  import type { Collection } from '$lib/types/collection';
  import { slide } from 'svelte/transition';
  import { createEventDispatcher } from 'svelte';
  import CollectionsEditor from './CollectionsEditor.svelte';
  import { t } from '$stores/locale';
  import { supportSessionGate } from '$stores/supportSession';
  import FrozenNote from '$components/legal-hold/FrozenNote.svelte';
  import { fileFrozen } from '$lib/legalHold/fileHoldContext';

  export let collections: Collection[] = [];
  export let isExpanded: boolean = false;
  export let fileId: string;
  export let aiCollectionSuggestions: Array<{name: string, confidence: number, rationale?: string, description?: string}> = [];

  const dispatch = createEventDispatcher();
  // A held file's collections are part of its preserved record (legal-hold plan, Lane D).
  const frozen = fileFrozen();

  function toggleExpanded() {
    isExpanded = !isExpanded;
  }

  function handleCollectionsUpdated(event: CustomEvent<{ collections: Collection[] }>) {
    // Re-emit the event to parent component
    collections = event.detail.collections;
    dispatch('collectionsUpdated', { collections: event.detail.collections });
  }
</script>

<div class="collections-dropdown-section">
  <button
    class="section-toggle"
    on:click={toggleExpanded}
    on:keydown={e => e.key === 'Enter' && toggleExpanded()}
    title={$t('collectionsSection.toggleTooltip')}
    aria-expanded={isExpanded && !$supportSessionGate.readOnly}
  >
    <h4 class="section-toggle-heading">{$t('collectionsSection.title')}</h4>
    <div class="collections-preview">
      {#if collections && collections.length > 0}
        {#each collections.slice(0, 3) as collection (collection.uuid)}
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

  {#if isExpanded && !$supportSessionGate.readOnly}
    <div class="collections-content" transition:slide={{ duration: 200 }}>
      {#if $frozen}
        <div class="collections-frozen">
          {#each collections as collection (collection.uuid)}
            <span class="collection-chip">{collection.name || $t('collectionsSection.unnamed')}</span>
          {/each}
          <FrozenNote id="collections-frozen-note" />
        </div>
      {:else if fileId}
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
  .collections-frozen {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  .collections-dropdown-section {
    margin-bottom: 0;
  }

  .collections-preview {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-1-5);
    flex-wrap: wrap;
    flex: 1;
    margin: 0 var(--space-3);
  }

  .collection-chip {
    background: var(--primary-light);
    color: var(--primary-on-surface);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-2xl);
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-medium);
    display: inline-block;
  }

  .collection-chip.more {
    background: var(--surface-secondary);
    color: var(--text-secondary);
  }

  .no-collections {
    color: var(--text-secondary);
    font-size: var(--font-size-md);
  }

  .dropdown-toggle svg {
    transition: transform var(--duration-base) ease;
  }

  .collections-content {
    border: 1px solid var(--border-color);
    border-top: none;
    border-radius: 0 0 var(--radius-lg) var(--radius-lg);
    background: var(--surface-color);
    padding: var(--space-5);
  }

  .collections-content p {
    margin: 0;
    color: var(--text-secondary);
    font-style: italic;
    text-align: center;
    padding: var(--space-5);
  }
</style>
