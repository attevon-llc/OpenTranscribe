<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage, getErrorStatus, getErrorCode } from '$lib/utils/apiError';
  import AISuggestionsDropdown from './AISuggestionsDropdown.svelte';
  import SearchableMultiSelect from './SearchableMultiSelect.svelte';

  import type { Collection } from '$lib/types/collection';

  type AISuggestion = { name: string; confidence: number; rationale?: string; description?: string };

  export let fileId = "";
  export let collections: Collection[] = [];
  export let aiSuggestions: AISuggestion[] = [];

  // Filter AI suggestions to only show ones not already applied
  $: filteredAISuggestions = aiSuggestions.filter(suggestion =>
    !collections.some(col => col.name.toLowerCase() === suggestion.name.toLowerCase())
  );

  $: autoAppliedCollectionNames = collections
    .filter(c => c.source === 'auto_ai' || c.source === 'bulk_group')
    .map(c => c.name);

  // Ensure collections are always in the correct format
  $: {
    if (Array.isArray(collections)) {
      collections = collections.map((collection: Collection): Collection => {
        if (collection && typeof collection === 'object') {
          return {
            uuid: collection.uuid || `temp-${collection.name}`,
            name: collection.name || '',
            description: collection.description || '',
            source: collection.source || undefined
          };
        }
        return collection;
      });
    }
  }

  let allCollections: Collection[] = [];
  let newCollectionInput = '';
  let loading = false;

  // Event dispatcher
  const dispatch = createEventDispatcher<{
    collectionsUpdated: { collections: Collection[] };
    aiSuggestionAccepted: { suggestion: AISuggestion };
  }>();

  // Fetch all available collections
  async function fetchAllCollections() {
    try {
      const response = await axiosInstance.get('/collections');

      // Ensure all collections have valid IDs
      const validCollections = (response.data || []).filter((collection: Collection) =>
        collection && typeof collection === 'object' &&
        collection.uuid !== undefined && collection.uuid !== null &&
        collection.name !== undefined && collection.name !== null
      );

      allCollections = validCollections;
    } catch (err: unknown) {
      console.error('[CollectionsEditor] Error fetching collections:', err);

      // Show toast for critical errors
      if (getErrorStatus(err) === 401) {
        toastStore.error($t('collections.unauthorizedLogin'));
      } else if (getErrorCode(err) === 'ERR_NETWORK') {
        toastStore.error($t('collections.networkError'));
      }
      // Silent fail for collection loading - not critical
    }
  }

  // Add file to an existing collection
  async function addToCollection(collectionId: string) {
    loading = true;
    try {
      const collectionToAdd = allCollections.find(c => c.uuid === collectionId);
      if (!collectionToAdd) {
        console.error(`[CollectionsEditor] Collection with ID ${collectionId} not found`);
        throw new Error('Collection not found');
      }

      // Add file to collection
      const payload = { media_file_ids: [fileId] };
      const addUrl = `/collections/${collectionId}/media`;
      const response = await axiosInstance.post(addUrl, payload);

      // Add collection to local list if not already present
      if (!collections.some(c => c.uuid === collectionToAdd.uuid)) {
        collections = [...collections, collectionToAdd];
        dispatch('collectionsUpdated', { collections });
        toastStore.success($t('collections.addedTo', { name: collectionToAdd.name }));
      }
    } catch (err: unknown) {
      console.error('[CollectionsEditor] Error adding to collection:', err);

      // Extract error message
      const errorMessage = getErrorMessage(err, $t('collections.failedToAdd'));

      toastStore.error(errorMessage);
    } finally {
      loading = false;
    }
  }

  // Create a new collection or add to existing one
  async function createAndAddCollection() {
    if (!newCollectionInput.trim()) return;
    loading = true;

    const collectionName = newCollectionInput.trim();

    try {
      // Check if collection already exists
      const existingCollection = allCollections.find(
        c => c.name.toLowerCase() === collectionName.toLowerCase()
      );

      if (existingCollection) {
        // Collection exists - just add file to it
        await addToCollection(existingCollection.uuid);
        newCollectionInput = '';
        return;
      }

      // Collection doesn't exist - create it
      const createPayload = { name: collectionName };
      const createResponse = await axiosInstance.post('/collections', createPayload);
      const newCollection = createResponse.data;

      if (!newCollection || typeof newCollection.uuid === 'undefined') {
        console.error('[CollectionsEditor] Invalid collection received from server:', newCollection);
        throw new Error('Server returned an invalid collection');
      }

      // Add to allCollections if it's not already present
      if (!allCollections.some(c => c.uuid === newCollection.uuid)) {
        allCollections = [...allCollections, { ...newCollection, media_count: 0 }];
      }

      // Add current file to the new collection
      const addPayload = { media_file_ids: [fileId] };
      await axiosInstance.post(`/collections/${newCollection.uuid}/media`, addPayload);

      // Add to collections if it's not already present
      if (!collections.some(c => c.uuid === newCollection.uuid)) {
        collections = [...collections, newCollection];
        dispatch('collectionsUpdated', { collections });
        toastStore.success($t('collections.createdAndAdded', { name: newCollection.name }));
      } else {
        toastStore.success($t('collections.created', { name: newCollection.name }));
      }

      newCollectionInput = '';
    } catch (err: unknown) {
      console.error('[CollectionsEditor] Error with collection:', err);

      // Extract error message
      const errorMessage = getErrorMessage(err, $t('collections.failedToAdd'));

      // Show toast instead of inline error
      toastStore.error(errorMessage);
    } finally {
      loading = false;
    }
  }

  // Remove file from a collection
  async function removeFromCollection(collectionId: string) {
    loading = true;
    try {
      const collectionToRemove = collections.find(c => c.uuid === collectionId);
      if (!collectionToRemove) {
        console.error(`[CollectionsEditor] Collection with ID ${collectionId} not found in collections list`);
        throw new Error('Collection not found');
      }

      // Remove file from collection
      const deleteUrl = `/collections/${collectionId}/media`;
      const payload = { media_file_ids: [fileId] };
      await axiosInstance.delete(deleteUrl, { data: payload });

      // Update the local collections array
      const updatedCollections = collections.filter(c =>
        c.uuid !== undefined && c.uuid !== null && c.uuid !== collectionId
      );

      if (updatedCollections.length !== collections.length) {
        collections = updatedCollections;
        dispatch('collectionsUpdated', { collections });
        toastStore.success($t('collections.removedFrom', { name: collectionToRemove.name }));
      }
    } catch (err: unknown) {
      console.error('[CollectionsEditor] Error removing from collection:', err);

      // Extract error message
      const errorMessage = getErrorMessage(err, $t('collections.failedToRemove'));

      toastStore.error(errorMessage);
    } finally {
      loading = false;
    }
  }

  // Handle keydown event in the input field
  function handleInputKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter' && newCollectionInput.trim()) {
      event.preventDefault();
      createAndAddCollection();
    }
  }

  let suggestedCollections: Collection[] = [];
  let dropdownCollections: Array<{ id: string; name: string; count: number }> = [];

  // Get top 5 most used collections as chips
  $: suggestedCollections = allCollections
    .filter(collection => {
      // Exclude collections the file is already in
      const isAssigned = collections.some(c =>
        (c.uuid === collection.uuid) || (c.name === collection.name)
      );
      if (isAssigned) return false;

      // Exclude collections that are in AI suggestions (prioritize AI suggestions)
      const isInAISuggestions = filteredAISuggestions.some(aiSug =>
        aiSug.name.toLowerCase() === collection.name.toLowerCase()
      );
      if (isInAISuggestions) return false;

      return true;
    })
    .sort((a, b) => (b.media_count || 0) - (a.media_count || 0)) // Sort by media count
    .slice(0, 5); // Top 5 most used

  // Get all available collections for dropdown (excluding already assigned and AI suggestions)
  $: dropdownCollections = allCollections
    .filter(collection => {
      const isAssigned = collections.some(c => (c.uuid === collection.uuid) || (c.name === collection.name));
      if (isAssigned) return false;

      const isInAISuggestions = filteredAISuggestions.some(aiSug =>
        aiSug.name.toLowerCase() === collection.name.toLowerCase()
      );
      if (isInAISuggestions) return false;

      return true;
    })
    .map(collection => ({
      id: collection.uuid,
      name: collection.name,
      count: collection.media_count || 0
    }));

  // Handle multiselect collection selection
  async function handleCollectionSelect(event: CustomEvent<{ id: string | number }>) {
    const { id } = event.detail;
    await addToCollection(String(id));
  }

  // Handle AI suggestion acceptance
  async function handleAcceptAISuggestion(event: CustomEvent<{ suggestion: AISuggestion }>) {
    const { suggestion } = event.detail;
    newCollectionInput = suggestion.name;
    await createAndAddCollection();
    newCollectionInput = '';
    // Don't remove from aiSuggestions - let reactive filtering handle it
    // This allows suggestions to reappear if the collection is later removed
    dispatch('aiSuggestionAccepted', { suggestion });
  }

  onMount(() => {
    fetchAllCollections();
  });
</script>

<div class="collections-editor">
  <div class="collections-list">
    {#if collections.length === 0}
      <span class="no-collections">{$t('collections.noCollectionsYet')}</span>
    {/if}
    {#each collections.filter(c => c && c.uuid !== undefined) as collection (collection.uuid)}
      <span class="collection" class:ai-collection={collection.source === 'auto_ai' || collection.source === 'bulk_group'}>
        {#if collection.source === 'auto_ai' || collection.source === 'bulk_group'}
          <span class="ai-icon" title={collection.source === 'bulk_group' ? $t('autoLabel.bulkGrouped') : $t('autoLabel.autoAppliedCollection')}>
            <svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>
          </span>
        {/if}
        {collection.name}
        <button class="collection-remove" on:click={() => removeFromCollection(collection.uuid)} title={$t('collections.removeFromCollection')}>×</button>
      </span>
    {/each}

    <div class="collection-input-container">
      <input
        type="text"
        placeholder={$t('collections.addCollectionPlaceholder')}
        bind:value={newCollectionInput}
        on:keydown={handleInputKeydown}
        class="collection-input"
        disabled={loading}
        title={$t('collections.typeNewCollectionHint')}
      >
      {#if newCollectionInput.trim()}
        <button
          class="collection-add-button"
          on:click={createAndAddCollection}
          disabled={loading}
          title={$t('collections.createAndAddHint', { collectionName: newCollectionInput.trim() })}
        >
          {$t('collections.add')}
        </button>
      {/if}
    </div>
  </div>

  <!-- AI Suggestions Dropdown -->
  <AISuggestionsDropdown
    suggestions={filteredAISuggestions}
    type="collection"
    {loading}
    autoAppliedCollections={autoAppliedCollectionNames}
    on:accept={handleAcceptAISuggestion}
  />

  {#if suggestedCollections.length > 0}
    <div class="suggested-collections">
      <span class="suggested-label">{$t('collections.suggested')}</span>
      {#each suggestedCollections.filter(c => c && c.uuid !== undefined) as collection (collection.uuid)}
        <button
          class="suggested-collection"
          on:click={() => addToCollection(collection.uuid)}
          disabled={loading}
          title={$t('collections.addToExistingHint', { collectionName: collection.name })}
        >
          {collection.name}
        </button>
      {/each}
    </div>
  {/if}

  {#if dropdownCollections.length > 0}
    <div class="dropdown-section">
      <span class="dropdown-label">{$t('collections.selectFromAllCollections')}</span>
      <SearchableMultiSelect
        options={dropdownCollections}
        selectedIds={[]}
        placeholder={$t('collections.addFromLibraryPlaceholder')}
        showCounts={true}
        on:select={handleCollectionSelect}
      />
    </div>
  {/if}
</div>

<style>
  .collections-editor {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .collections-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
  }

  .collection {
    background-color: rgba(59, 130, 246, 0.1);
    color: var(--primary-on-surface);
    padding: 0.35rem 0.5rem;
    border-radius: 4px;
    font-size: 0.8rem;
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }

  .collection-remove {
    background: none;
    border: none;
    color: var(--text-light);
    font-size: 1rem;
    cursor: pointer;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
  }

  .collection-remove:hover {
    color: var(--error-color);
  }

  .collection-input-container {
    display: flex;
    align-items: center;
  }

  .collection-input {
    background: transparent;
    border: none;
    border-bottom: 1px dashed var(--border-color);
    padding: 0.35rem 0;
    font-size: 0.8rem;
    width: 100px;
    color: var(--text-color);
  }

  .collection-input:focus {
    border-bottom-color: var(--primary-color);
    outline: none;
  }

  .collection-add-button {
    background-color: var(--primary-color);
    color: white;
    border: none;
    border-radius: 4px;
    padding: 0.25rem 0.5rem;
    font-size: 0.8rem;
    cursor: pointer;
    margin-left: 0.5rem;
  }

  .collection-add-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .suggested-collections {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
    font-size: 0.8rem;
  }

  .suggested-label {
    color: var(--text-light);
  }

  .suggested-collection {
    background: none;
    border: 1px dashed var(--border-color);
    border-radius: 4px;
    padding: 0.25rem 0.5rem;
    font-size: 0.8rem;
    cursor: pointer;
    color: var(--text-light);
  }

  .suggested-collection:hover {
    border-color: var(--primary-color);
    color: var(--primary-on-surface);
    background-color: rgba(59, 130, 246, 0.05);
  }

  .suggested-collection:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .no-collections {
    color: var(--text-secondary);
    font-size: 0.85rem;
    font-style: italic;
    padding: 0.5rem 0;
  }

  /* Dark mode support */
  :global([data-theme='dark']) .collection {
    background: rgba(59, 130, 246, 0.2);
    color: var(--primary-on-surface);
  }

  :global([data-theme='dark']) .suggested-collection:hover {
    background-color: rgba(59, 130, 246, 0.1);
  }

  .dropdown-section {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .dropdown-label {
    color: var(--text-light);
    font-size: 0.8rem;
  }

  .ai-collection {
    background-color: rgba(59, 130, 246, 0.08);
    border: 1px solid rgba(59, 130, 246, 0.25);
  }

  .ai-icon {
    color: var(--ai-accent-color, var(--primary-color, #3b82f6));
    display: flex;
    align-items: center;
  }

  :global([data-theme='dark']) .ai-collection {
    background-color: rgba(96, 165, 250, 0.12);
    border-color: rgba(96, 165, 250, 0.3);
  }
</style>
