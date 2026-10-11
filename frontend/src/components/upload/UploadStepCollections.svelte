<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { getErrorStatus } from '$lib/utils/apiError';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';
  import SearchableMultiSelect from '$components/SearchableMultiSelect.svelte';
  import CollectionFormModal, { type PromptOption } from '$components/collections/CollectionFormModal.svelte';
  import './upload-shared.css';

  export let selectedCollections: Array<{uuid: string; name: string}> = [];
  export let availableCollections: Array<{uuid: string; name: string; media_count?: number}> = [];
  export let hasPrevious = false;

  const dispatch = createEventDispatcher<{
    collectionsChange: { collections: Array<{uuid: string; name: string}> };
    collectionsListUpdated: { collections: Array<{uuid: string; name: string; media_count?: number}> };
    clearPrevious: void;
  }>();

  let showCreateModal = false;
  let newName = '';
  let newDescription = '';
  let newPromptId: string | null = null;
  let availablePrompts: PromptOption[] = [];
  let loadingPrompts = false;
  let creating = false;

  /** The same dropdown the filter sidebar uses (UX review item 4). */
  $: options = availableCollections.map((c) => ({
    id: c.uuid,
    name: c.name,
    count: c.media_count,
  }));
  $: selectedUuids = selectedCollections.map((c) => c.uuid);

  function handleSelect(e: CustomEvent<{ id: string | number }>) {
    const found = availableCollections.find((c) => c.uuid === e.detail.id);
    if (!found || selectedUuids.includes(found.uuid)) return;
    selectedCollections = [...selectedCollections, { uuid: found.uuid, name: found.name }];
    dispatch('collectionsChange', { collections: selectedCollections });
  }

  function handleDeselect(e: CustomEvent<{ id: string | number }>) {
    removeCollection(String(e.detail.id));
  }

  function removeCollection(uuid: string) {
    selectedCollections = selectedCollections.filter(c => c.uuid !== uuid);
    dispatch('collectionsChange', { collections: selectedCollections });
  }

  // Same prompt list and system-default preselection as the gallery's Collections panel,
  // since this opens that panel's own create modal.
  async function openCreateModal() {
    showCreateModal = true;
    if (availablePrompts.length > 0) return;
    loadingPrompts = true;
    try {
      const response = await axiosInstance.get('/prompts');
      availablePrompts = response.data.prompts || [];
      const systemDefault = availablePrompts.find((p) => p.is_system_default);
      if (systemDefault && newPromptId === null) newPromptId = systemDefault.uuid;
    } catch (err: unknown) {
      // Non-critical: the modal's prompt dropdown is simply empty.
      console.error('Error fetching prompts:', err);
    } finally {
      loadingPrompts = false;
    }
  }

  async function createCollection() {
    const name = newName.trim();
    if (!name) return;

    creating = true;
    try {
      const response = await axiosInstance.post('/collections', {
        name,
        description: newDescription.trim() || null,
        default_prompt_id: newPromptId || null,
      });
      const col = response.data;
      availableCollections = [...availableCollections, { uuid: col.uuid, name: col.name }];
      dispatch('collectionsListUpdated', { collections: availableCollections });
      selectedCollections = [...selectedCollections, { uuid: col.uuid, name: col.name }];
      dispatch('collectionsChange', { collections: selectedCollections });
      newName = '';
      newDescription = '';
      showCreateModal = false;
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

  {#if availableCollections.length > 0}
    <SearchableMultiSelect
      {options}
      selectedIds={selectedUuids}
      placeholder={$t('uploader.selectCollectionsPlaceholder')}
      maxHeight="220px"
      showCounts={true}
      on:select={handleSelect}
      on:deselect={handleDeselect}
    />
  {:else}
    <p class="empty-text">{$t('upload.noCollectionsYet')}</p>
  {/if}

  <div class="create-row">
    <button type="button" class="create-btn" on:click={openCreateModal}>
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="12" y1="5" x2="12" y2="19"></line>
        <line x1="5" y1="12" x2="19" y2="12"></line>
      </svg>
      {$t('uploader.createNewCollection')}
    </button>
  </div>
</div>

<!-- Backdrop click deliberately does not close it (the modal only closes via X / Escape), the
     same as in the gallery, so a stray click cannot discard a half-typed name. -->
{#if showCreateModal}
  <CollectionFormModal
    idPrefix="collection"
    titleId="create-collection-title"
    title={$t('collectionsPanel.createNewCollection')}
    intro={$t('collectionsPanel.createIntro')}
    namePlaceholder={$t('collectionsPanel.namePlaceholder')}
    submitLabel={$t('collectionsPanel.createCollection')}
    busyLabel={$t('collectionsPanel.creating')}
    bind:name={newName}
    bind:description={newDescription}
    bind:promptId={newPromptId}
    busy={creating}
    {loadingPrompts}
    {availablePrompts}
    on:submit={createCollection}
    on:close={() => (showCreateModal = false)}
  />
{/if}
