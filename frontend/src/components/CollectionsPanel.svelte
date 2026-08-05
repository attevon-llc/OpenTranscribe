<script lang="ts">
  import type { Collection } from '$lib/types/collection';
  import { onMount, onDestroy } from 'svelte';
  import { fade } from 'svelte/transition';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import ConfirmationModal from './ConfirmationModal.svelte';
  import ShareCollectionModal from './sharing/ShareCollectionModal.svelte';
  import { SharingApi } from '$lib/api/sharing';
  import type { SharedCollection } from '$lib/types/groups';
  import Spinner from './ui/Spinner.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import CollectionsList from '$components/collections/CollectionsList.svelte';
  import SharedCollectionsSection from '$components/collections/SharedCollectionsSection.svelte';
  import CollectionFormModal from '$components/collections/CollectionFormModal.svelte';

  // Props
  export let selectedMediaIds: string[] = [];  // UUIDs
  export let onCollectionSelect: (collectionId: string) => void = () => {};  // UUID
  export let viewMode: 'manage' | 'add' = 'manage';

  // State
  let collections: Collection[] = [];
  let sharedCollections: SharedCollection[] = [];
  let loadingShared = false;
  let showShareModal = false;
  let shareModalCollectionUuid = '';
  let shareModalCollectionName = '';
  let loading = false;
  let showCreateModal = false;
  let showEditModal = false;
  let showDeleteConfirm = false;
  let collectionToEdit: Collection | null = null;
  let collectionToDelete: Collection | null = null;
  let newCollectionName = '';
  let newCollectionDescription = '';
  let editCollectionName = '';
  let editCollectionDescription = '';
  let creating = false;
  let updating = false;
  let deleting = false;
  let selectedCollectionId: string | null = null;  // UUID
  let addingToCollection = false;

  // Search state
  let searchQuery = '';

  // Filtered collections based on search
  $: filteredCollections = searchQuery.trim()
    ? collections.filter(c =>
        c.name.toLowerCase().includes(searchQuery.trim().toLowerCase()) ||
        (c.description && c.description.toLowerCase().includes(searchQuery.trim().toLowerCase()))
      )
    : collections;

  $: filteredSharedCollections = searchQuery.trim()
    ? sharedCollections.filter(c =>
        c.name.toLowerCase().includes(searchQuery.trim().toLowerCase()) ||
        (c.description && c.description.toLowerCase().includes(searchQuery.trim().toLowerCase()))
      )
    : sharedCollections;

  // Prompt selection state
  let availablePrompts: any[] = [];
  let newCollectionPromptId: string | null = null;
  let editCollectionPromptId: string | null = null;
  let loadingPrompts = false;

  // Fetch collections
  async function fetchCollections() {
    loading = true;

    try {
      const response = await axiosInstance.get('/collections');
      collections = response.data;
    } catch (err: unknown) {
      console.error('Error fetching collections:', err);
      toastStore.error($t('collectionsPanel.failedToLoad'));
    } finally {
      loading = false;
    }
  }

  // System default prompt (Universal Content Analyzer) — used as the
  // default selection when creating new collections. Users can override.
  let systemDefaultPromptId: string | null = null;

  // Fetch available prompts for dropdown
  async function fetchPrompts() {
    loadingPrompts = true;
    try {
      const response = await axiosInstance.get('/prompts');
      availablePrompts = response.data.prompts || [];
      // Find the system default (Universal Content Analyzer) — used as
      // the pre-selected default when creating new collections.
      const systemDefault = availablePrompts.find((p: any) => p.is_system_default);
      if (systemDefault) {
        systemDefaultPromptId = systemDefault.uuid;
        // Apply to the Create form if it hasn't been touched yet
        if (newCollectionPromptId === null) {
          newCollectionPromptId = systemDefault.uuid;
        }
      }
    } catch (err: unknown) {
      console.error('Error fetching prompts:', err);
      // Non-critical: prompts dropdown will just be empty
    } finally {
      loadingPrompts = false;
    }
  }

  // Fetch shared collections
  async function fetchSharedCollections() {
    loadingShared = true;
    try {
      sharedCollections = await SharingApi.fetchSharedCollections();
    } catch (err: unknown) {
      console.error('Error fetching shared collections:', err);
    } finally {
      loadingShared = false;
    }
  }

  // Open share modal for a collection
  function openShareModal(collection: Collection) {
    shareModalCollectionUuid = collection.uuid;
    shareModalCollectionName = collection.name;
    showShareModal = true;
  }

  // Handle share completion - refresh collections
  function handleShared() {
    fetchCollections();
    fetchSharedCollections();
  }

  // Create new collection
  async function createCollection() {
    if (!newCollectionName.trim()) return;

    creating = true;

    try {
      const response = await axiosInstance.post('/collections', {
        name: newCollectionName.trim(),
        description: newCollectionDescription.trim() || null,
        default_prompt_id: newCollectionPromptId || null
      });

      collections = [...collections, { ...response.data, media_count: 0 }];

      // Reset form for potential next collection. Keep the system default prompt
      // pre-selected so users don't have to re-select it every time.
      newCollectionName = '';
      newCollectionDescription = '';
      newCollectionPromptId = systemDefaultPromptId;

      // If in add mode and media is selected, add to new collection
      if (viewMode === 'add' && selectedMediaIds.length > 0) {
        await addMediaToCollection(response.data.uuid);
        // Close create modal after adding media since the workflow is complete
        showCreateModal = false;
        // Trigger callback to refresh filters after adding media
        onCollectionSelect(response.data.uuid);
      } else {
        toastStore.success($t('collectionsPanel.createdSuccess', { name: response.data.name }));
        // Close the create modal but keep the manage collections modal open
        showCreateModal = false;
        // Don't trigger collection selection callback in manage mode during creation
      }
    } catch (err: unknown) {
      console.error('Error creating collection:', err);
      toastStore.error(getErrorMessage(err, $t('collectionsPanel.failedToCreate')));
    } finally {
      creating = false;
    }
  }

  // Update collection
  async function updateCollection() {
    if (!collectionToEdit || !editCollectionName.trim()) return;
    // Capture before the await — the closure below can't rely on the narrowing.
    const editing = collectionToEdit;

    updating = true;

    try {
      const response = await axiosInstance.put(`/collections/${collectionToEdit.uuid}`, {
        name: editCollectionName.trim(),
        description: editCollectionDescription.trim() || null,
        default_prompt_id: editCollectionPromptId || null
      });

      // Update local state
      collections = collections.map(col =>
        col.uuid === editing.uuid ? { ...col, ...response.data } : col
      );

      toastStore.success($t('collectionsPanel.updatedSuccess', { name: editCollectionName }));
      showEditModal = false;
      collectionToEdit = null;
    } catch (err: unknown) {
      console.error('Error updating collection:', err);
      toastStore.error(getErrorMessage(err, $t('collectionsPanel.failedToUpdate')));
    } finally {
      updating = false;
    }
  }

  // Open edit modal
  function openEditModal(collection: Collection) {
    collectionToEdit = collection;
    editCollectionName = collection.name;
    editCollectionDescription = collection.description || '';
    editCollectionPromptId = collection.default_prompt_id || null;
    showEditModal = true;
  }

  // Open delete confirmation
  function openDeleteConfirm(collection: Collection) {
    collectionToDelete = collection;
    showDeleteConfirm = true;
  }

  // Delete collection
  async function deleteCollection() {
    if (!collectionToDelete) return;
    const deletingCollection = collectionToDelete;

    deleting = true;

    try {
      await axiosInstance.delete(`/collections/${deletingCollection.uuid}`);
      collections = collections.filter(col => col.uuid !== deletingCollection.uuid);

      if (selectedCollectionId === deletingCollection.uuid) {
        selectedCollectionId = null;
      }

      toastStore.success($t('collectionsPanel.deletedSuccess', { name: collectionToDelete.name }));
      showDeleteConfirm = false;
      collectionToDelete = null;
    } catch (err: unknown) {
      console.error('Error deleting collection:', err);
      toastStore.error(getErrorMessage(err, $t('collectionsPanel.failedToDelete')));
    } finally {
      deleting = false;
    }
  }

  // Handle confirmation modal confirm
  function handleConfirmModalConfirm() {
    deleteCollection();
  }

  // Handle confirmation modal cancel
  function handleConfirmModalCancel() {
    showDeleteConfirm = false;
    collectionToDelete = null;
  }

  // Add selected media to collection
  async function addMediaToCollection(collectionId: string) {
    if (!selectedMediaIds.length) return;

    addingToCollection = true;

    try {
      const response = await axiosInstance.post(`/collections/${collectionId}/media`, {
        media_file_ids: selectedMediaIds
      });

      // Update media count
      collections = collections.map(col =>
        col.uuid === collectionId
          ? { ...col, media_count: col.media_count + (response.data.added || 0) }
          : col
      );

      // Show success message
      const collection = collections.find(c => c.uuid === collectionId);
      toastStore.success($t('collectionsPanel.addedSuccess', { count: response.data.added, name: collection?.name || 'collection' }));

      // Trigger callback to close modal and refresh
      onCollectionSelect(collectionId);
    } catch (err: unknown) {
      console.error('Error adding media to collection:', err);
      toastStore.error(getErrorMessage(err, $t('collectionsPanel.failedToAddMedia')));
    } finally {
      addingToCollection = false;
    }
  }

  // Handle collection click
  function handleCollectionClick(collection: Collection) {
    if (viewMode === 'manage') {
      selectedCollectionId = collection.uuid;
      onCollectionSelect(collection.uuid);
    } else if (viewMode === 'add' && selectedMediaIds.length > 0) {
      addMediaToCollection(collection.uuid);
    }
  }

  function handleShareWsEvent() {
    fetchCollections();
    fetchSharedCollections();
  }

  onMount(() => {
    window.addEventListener('collection-shared', handleShareWsEvent);
    window.addEventListener('share-revoked', handleShareWsEvent);
    window.addEventListener('share-updated', handleShareWsEvent);
    fetchCollections();
    fetchPrompts();
    fetchSharedCollections();
  });

  onDestroy(() => {
    window.removeEventListener('collection-shared', handleShareWsEvent);
    window.removeEventListener('share-revoked', handleShareWsEvent);
    window.removeEventListener('share-updated', handleShareWsEvent);
  });
</script>

<div class="collections-panel" transition:fade>
  <div class="panel-header">
    <h3>{$t('collectionsPanel.title')}</h3>
    <button
      class="btn-create"
      on:click={() => {
        showCreateModal = true;
      }}
      disabled={creating}
    >
      <span class="icon">+</span>
      {$t('collectionsPanel.newCollection')}
    </button>
  </div>

  <!-- Search -->
  {#if !loading && (collections.length > 0 || sharedCollections.length > 0)}
    <div class="search-wrapper">
      <svg class="search-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="11" cy="11" r="8"></circle>
        <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
      </svg>
      <input
        type="text"
        class="search-input"
        placeholder={$t('collectionsPanel.searchPlaceholder')}
        bind:value={searchQuery}
      />
      {#if searchQuery}
        <button
          class="search-clear"
          on:click={() => searchQuery = ''}
          title={$t('collectionsPanel.clearSearch')}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      {/if}
    </div>
  {/if}

  {#if loading}
    <div class="loading">
      <Spinner size="large" />
      {$t('collectionsPanel.loading')}
    </div>
  {:else if collections.length === 0 && sharedCollections.length === 0}
    <EmptyState title={$t('collectionsPanel.noCollectionsYet')} description={$t('collectionsPanel.createFirstHint')} padding="40px 20px" />
  {:else}
    <div class="collections-list">
      {#if searchQuery && filteredCollections.length === 0 && filteredSharedCollections.length === 0}
        <div class="no-results">
          <p>{$t('collectionsPanel.noSearchResults')}</p>
        </div>
      {/if}

      <!-- My Collections -->
      <CollectionsList
        collections={filteredCollections}
        {viewMode}
        {selectedMediaIds}
        {selectedCollectionId}
        {addingToCollection}
        on:select={(e) => handleCollectionClick(e.detail)}
        on:share={(e) => openShareModal(e.detail)}
        on:edit={(e) => openEditModal(e.detail)}
        on:delete={(e) => openDeleteConfirm(e.detail)}
        on:add={(e) => addMediaToCollection(e.detail)}
      />

      <!-- Shared with Me -->
      <SharedCollectionsSection
        sharedCollections={filteredSharedCollections}
        {viewMode}
        {selectedMediaIds}
        {selectedCollectionId}
        {addingToCollection}
        on:select={(e) => handleCollectionClick(e.detail)}
        on:add={(e) => addMediaToCollection(e.detail)}
      />
    </div>
  {/if}

  <!-- Create Collection Modal -->
  <!--
    Backdrop click is intentionally NOT bound to close.
    Users have in-progress form state; stray clicks shouldn't destroy it.
    Close via X button or Escape only.
  -->
  {#if showCreateModal}
    <CollectionFormModal
      idPrefix="collection"
      titleId="create-collection-title"
      title={$t('collectionsPanel.createNewCollection')}
      intro={$t('collectionsPanel.createIntro')}
      namePlaceholder={$t('collectionsPanel.namePlaceholder')}
      submitLabel={$t('collectionsPanel.createCollection')}
      busyLabel={$t('collectionsPanel.creating')}
      bind:name={newCollectionName}
      bind:description={newCollectionDescription}
      bind:promptId={newCollectionPromptId}
      busy={creating}
      {loadingPrompts}
      {availablePrompts}
      on:submit={createCollection}
      on:close={() => showCreateModal = false}
    />
  {/if}

  <!-- Edit Collection Modal -->
  {#if showEditModal && collectionToEdit}
    <CollectionFormModal
      idPrefix="edit-collection"
      titleId="edit-collection-title"
      title={$t('collectionsPanel.editCollectionTitle')}
      intro={$t('collectionsPanel.editIntro')}
      namePlaceholder={$t('collectionsPanel.collectionName')}
      submitLabel={$t('collectionsPanel.updateCollection')}
      busyLabel={$t('collectionsPanel.updating')}
      bind:name={editCollectionName}
      bind:description={editCollectionDescription}
      bind:promptId={editCollectionPromptId}
      busy={updating}
      {loadingPrompts}
      {availablePrompts}
      on:submit={updateCollection}
      on:close={() => showEditModal = false}
    />
  {/if}

<!-- Share Collection Modal -->
{#if showShareModal}
  <ShareCollectionModal
    collectionUuid={shareModalCollectionUuid}
    collectionName={shareModalCollectionName}
    on:shared={handleShared}
    on:close={() => showShareModal = false}
  />
{/if}

<!-- Delete Confirmation Modal -->
<ConfirmationModal
  bind:isOpen={showDeleteConfirm}
  title={$t('collectionsPanel.deleteCollectionTitle')}
  message={collectionToDelete ? $t('collectionsPanel.deleteConfirmMessage', { name: collectionToDelete.name }) : ''}
  confirmText={deleting ? $t('collectionsPanel.deleting') : $t('collectionsPanel.deleteCollectionButton')}
  cancelText={$t('collectionsPanel.cancel')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={handleConfirmModalConfirm}
  on:cancel={handleConfirmModalCancel}
  on:close={handleConfirmModalCancel}
/>
</div>

<style>
  .collections-panel {
    /* No background/border — the parent modal-container provides the surface.
       Previously this created a nested "card-in-card" look with a gray inside white. */
    background: transparent;
    border: none;
    padding: 0;
    height: 100%;
    display: flex;
    flex-direction: column;
  }

  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
  }

  .panel-header h3 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .btn-create {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 0.6rem 1.2rem;
    background-color: #3b82f6;
    color: white;
    border: none;
    border-radius: 8px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .btn-create:hover:not(:disabled) {
    background-color: #2563eb;
    color: white;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
    text-decoration: none;
  }

  .btn-create:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-create:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .icon {
    font-size: 18px;
    line-height: 1;
    color: white;
    font-weight: bold;
  }

  .search-wrapper {
    position: relative;
    margin-bottom: 12px;
  }

  .search-icon {
    position: absolute;
    left: 10px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--text-secondary);
    pointer-events: none;
  }

  .search-input {
    width: 100%;
    padding: 0.5rem 2rem 0.5rem 2.25rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.875rem;
    transition: border-color 0.15s, box-shadow 0.15s;
    box-sizing: border-box;
  }

  .search-input:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px var(--primary-light, rgba(59, 130, 246, 0.1));
  }

  .search-input::placeholder {
    color: var(--text-secondary);
    opacity: 0.7;
  }

  .search-clear {
    position: absolute;
    right: 6px;
    top: 50%;
    transform: translateY(-50%);
    background: none;
    border: none;
    cursor: pointer;
    padding: 4px;
    color: var(--text-secondary);
    border-radius: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: color 0.15s;
  }

  .search-clear:hover {
    color: var(--text-color);
  }

  .no-results {
    text-align: center;
    padding: 1.5rem 0;
    color: var(--text-secondary);
    font-size: 0.875rem;
  }

  .no-results p {
    margin: 0;
  }

  .loading {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 40px;
    color: var(--text-secondary);
  }

  .collections-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
    overflow-y: auto;
    flex: 1;
    max-height: 400px;
    padding-right: 8px;
  }

  /* Custom scrollbar styling */
  .collections-list::-webkit-scrollbar {
    width: 6px;
  }

  .collections-list::-webkit-scrollbar-track {
    background: var(--surface-color);
    border-radius: 3px;
  }

  .collections-list::-webkit-scrollbar-thumb {
    background: var(--border-color);
    border-radius: 3px;
  }

  .collections-list::-webkit-scrollbar-thumb:hover {
    background: var(--text-secondary);
  }
</style>
