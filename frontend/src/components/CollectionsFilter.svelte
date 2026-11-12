<script lang="ts">
  import type { Collection } from '$lib/types/collection';
  import { onMount } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { apiCache, cacheKey, CacheTTL } from '$lib/apiCache';
  import { latestFacetLoad } from '$lib/gallery/filterData';
  import { t } from '$stores/locale';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import SearchableMultiSelect from './SearchableMultiSelect.svelte';

  export let selectedCollectionId: string | null = null;

  let collections: Collection[] = [];
  let loading = false;
  let error: string | null = null;
  /** The option shape SearchableMultiSelect expects (its own `Option` type is component-local). */
  type MultiSelectOption = { id: string; name: string; count: number };
  // Reactive: Prepare dropdown collections with proper format
  $: dropdownCollections = collections.map((collection): MultiSelectOption => ({
    id: collection.uuid,
    name: collection.name,
    count: collection.media_count || 0
  }));

  // Reactive: Convert selected collection ID to array for multiselect (single selection mode)
  $: selectedCollectionIds = selectedCollectionId ? [selectedCollectionId] : [];

  const collectionsLoad = latestFacetLoad(() =>
    apiCache.getOrFetch(
      cacheKey.collections(),
      async (): Promise<Collection[]> => {
        const response = await axiosInstance.get('/collections', { params: { ownership: 'all' } });
        // Sort collections by media_count descending (most used first)
        return (response.data || []).sort((a: Collection, b: Collection) => {
          return (b.media_count || 0) - (a.media_count || 0);
        });
      },
      CacheTTL.COLLECTIONS
    )
  );

  /**
   * A failed load shows an error with a retry. It used to fall through to "no collections
   * created", which a user cannot tell apart from having none (the #743a pattern the other
   * facets already fixed).
   */
  export async function fetchCollections() {
    loading = true;
    error = null;
    const outcome = await collectionsLoad();
    if (outcome.kind === 'superseded') return;
    loading = false;
    if (outcome.kind === 'ok') {
      collections = outcome.value;
    } else if (outcome.kind === 'error') {
      console.error('Error fetching collections:', outcome.cause);
      collections = [];
      error = outcome.message;
    }
  }

  /**
   * Handle collection selection from multiselect dropdown
   * Single selection mode: only one collection can be selected at a time
   * @param {CustomEvent} event - Event with collection id
   */
  function handleCollectionSelect(event: CustomEvent) {
    const collectionId = event.detail.id;
    // If clicking the same collection, deselect it
    if (selectedCollectionId === collectionId) {
      selectedCollectionId = null;
    } else {
      // Otherwise, select the new collection (replacing any previous selection)
      selectedCollectionId = collectionId;
    }
  }

  /**
   * Handle collection deselection from multiselect dropdown
   */
  function handleCollectionDeselect() {
    selectedCollectionId = null;
  }

  onMount(() => {
    void fetchCollections();
  });
</script>

<div class="collections-filter">
  {#if loading}
    <p class="loading-text">{$t('collections.loadingCollections')}</p>
  {:else if error}
    <EmptyState
      icon="warning"
      title={$t('filter.collectionsLoadFailed')}
      description={$t('filter.facetLoadFailedHelp')}
      padding="12px 0"
    >
      <button class="retry-button" data-testid="collections-retry" on:click={fetchCollections}
        >{$t('filter.retry')}</button
      >
    </EmptyState>
  {:else if collections.length === 0}
    <p class="empty-text">{$t('collections.noCollectionsCreated')}</p>
  {:else}
    <div class="dropdown-section">
      <SearchableMultiSelect
        options={dropdownCollections}
        selectedIds={selectedCollectionIds}
        placeholder={$t('collections.selectCollectionToFilter')}
        maxHeight="300px"
        showCounts={true}
        on:select={handleCollectionSelect}
        on:deselect={handleCollectionDeselect}
      />
    </div>
  {/if}
</div>

<style>
  .collections-filter {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .loading-text,
  .empty-text {
    font-size: var(--font-size-body);
    color: var(--text-secondary);
    margin: 0;
  }

  .dropdown-section {
    margin-top: 0;
  }
</style>
