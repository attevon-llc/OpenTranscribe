<script lang="ts">
  import type { Collection } from '$lib/types/collection';
  import { onMount } from 'svelte';
  import { createEventDispatcher } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { apiCache, cacheKey, CacheTTL } from '$lib/apiCache';
  import { t } from '$stores/locale';
  import SearchableMultiSelect from './SearchableMultiSelect.svelte';

  export let selectedCollectionId: string | null = null;

  let collections: Collection[] = [];
  let loading = false;
  /** The option shape SearchableMultiSelect expects (its own `Option` type is component-local). */
  type MultiSelectOption = { id: string; name: string; count: number };
  let dropdownCollections: MultiSelectOption[] = [];

  const dispatch = createEventDispatcher();

  // Reactive: Prepare dropdown collections with proper format
  $: dropdownCollections = collections.map(collection => ({
    id: collection.uuid,
    name: collection.name,
    count: collection.media_count || 0
  }));

  // Reactive: Convert selected collection ID to array for multiselect (single selection mode)
  $: selectedCollectionIds = selectedCollectionId ? [selectedCollectionId] : [];

  export async function fetchCollections() {
    loading = true;
    try {
      const data = await apiCache.getOrFetch(
        cacheKey.collections(),
        async () => {
          const response = await axiosInstance.get('/collections', { params: { ownership: 'all' } });
          // Sort collections by media_count descending (most used first)
          return (response.data || []).sort((a: Collection, b: Collection) => {
            return (b.media_count || 0) - (a.media_count || 0);
          });
        },
        CacheTTL.COLLECTIONS
      );
      collections = data;
    } catch (err) {
      console.error('Error fetching collections:', err);
    } finally {
      loading = false;
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
   * @param {CustomEvent} event - Event with collection id
   */
  function handleCollectionDeselect(event: CustomEvent) {
    selectedCollectionId = null;
  }

  onMount(() => {
    fetchCollections();
  });
</script>

<div class="collections-filter">
  {#if loading}
    <p class="loading-text">{$t('collections.loadingCollections')}</p>
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
    gap: 0.5rem;
  }

  .loading-text,
  .empty-text {
    font-size: 0.9rem;
    color: var(--text-light);
    margin: 0;
  }

  .dropdown-section {
    margin-top: 0;
  }
</style>
