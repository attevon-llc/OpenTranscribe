<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { getErrorStatus } from '$lib/utils/apiError';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';
  import './upload-shared.css';

  export let selectedCollections: Array<{uuid: string; name: string}> = [];
  export let availableCollections: Array<{uuid: string; name: string; media_count?: number}> = [];
  export let hasPrevious = false;

  const dispatch = createEventDispatcher<{
    collectionsChange: { collections: Array<{uuid: string; name: string}> };
    collectionsListUpdated: { collections: Array<{uuid: string; name: string; media_count?: number}> };
    clearPrevious: void;
  }>();

  let filterQuery = '';
  let newName = '';
  let creating = false;

  $: filteredCollections = availableCollections.filter(c =>
    c.name.toLowerCase().includes(filterQuery.toLowerCase())
  );

  function isSelected(uuid: string): boolean {
    return selectedCollections.some(c => c.uuid === uuid);
  }

  function toggleCollection(collection: {uuid: string; name: string}) {
    if (isSelected(collection.uuid)) {
      selectedCollections = selectedCollections.filter(c => c.uuid !== collection.uuid);
    } else {
      selectedCollections = [...selectedCollections, collection];
    }
    dispatch('collectionsChange', { collections: selectedCollections });
  }

  function removeCollection(uuid: string) {
    selectedCollections = selectedCollections.filter(c => c.uuid !== uuid);
    dispatch('collectionsChange', { collections: selectedCollections });
  }

  async function createCollection() {
    const name = newName.trim();
    if (!name) return;

    creating = true;
    try {
      const response = await axiosInstance.post('/collections', { name });
      const col = response.data;
      availableCollections = [...availableCollections, { uuid: col.uuid, name: col.name }];
      dispatch('collectionsListUpdated', { collections: availableCollections });
      selectedCollections = [...selectedCollections, { uuid: col.uuid, name: col.name }];
      dispatch('collectionsChange', { collections: selectedCollections });
      newName = '';
      toastStore.success($t('uploader.collectionCreated'));
    } catch (err: unknown) {
      if (getErrorStatus(err) === 409) {
        toastStore.warning($t('uploader.collectionAlreadyExists'));
      } else {
        toastStore.error($t('uploader.createCollectionFailed'));
      }
    } finally {
      creating = false;
    }
  }

  function handleCreateKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault();
      e.stopPropagation();
      createCollection();
    }
  }
</script>

<div class="step-collections">
  <p class="step-hint">{$t('uploader.collectionsHint')}</p>

  {#if hasPrevious && selectedCollections.length > 0}
    <div class="previous-banner">
      <span class="previous-banner-text">{$t('uploader.usingPrevious')}</span>
      <button type="button" class="previous-banner-clear" on:click={() => dispatch('clearPrevious')}>
        {$t('uploader.clearPrevious')}
      </button>
    </div>
  {/if}

  <!-- Selected chips -->
  {#if selectedCollections.length > 0}
    <div class="selected-chips">
      {#each selectedCollections as collection}
        <span class="chip">
          {collection.name}
          <button type="button" class="chip-remove" on:click={() => removeCollection(collection.uuid)} title={$t('uploader.removeItem')}>×</button>
        </span>
      {/each}
    </div>
  {/if}

  <!-- Filter (only show if there are enough collections) -->
  {#if availableCollections.length > 6}
    <input
      type="text"
      class="item-filter"
      placeholder={$t('upload.filterCollections')}
      bind:value={filterQuery}
    />
  {/if}

  <!-- Collection list -->
  {#if filteredCollections.length > 0}
    <div class="item-list">
      {#each filteredCollections as collection}
        <label class="item-row">
          <input
            type="checkbox"
            checked={isSelected(collection.uuid)}
            on:change={() => toggleCollection(collection)}
          />
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
          </svg>
          <span class="item-name">{collection.name}</span>
        </label>
      {/each}
    </div>
  {:else if filterQuery}
    <p class="empty-text">{$t('upload.noCollectionsMatch', { query: filterQuery })}</p>
  {:else}
    <p class="empty-text">{$t('upload.noCollectionsYet')}</p>
  {/if}

  <!-- Create new -->
  <div class="create-row">
    <input
      type="text"
      class="create-input"
      placeholder={$t('uploader.selectCollection')}
      bind:value={newName}
      on:keydown={handleCreateKeydown}
      disabled={creating}
    />
    <button
      type="button"
      class="create-btn"
      on:click={createCollection}
      disabled={!newName.trim() || creating}
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="12" y1="5" x2="12" y2="19"></line>
        <line x1="5" y1="12" x2="19" y2="12"></line>
      </svg>
      {$t('uploader.createNewCollection')}
    </button>
  </div>
</div>
