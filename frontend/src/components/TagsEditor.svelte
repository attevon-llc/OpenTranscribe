<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorStatus, getErrorCode } from '$lib/utils/apiError';
  import { addTagToFile, createTag, listTags, removeTagFromFile } from '$lib/api/tags';
  import AISuggestionsDropdown from './AISuggestionsDropdown.svelte';
  import SearchableMultiSelect from './SearchableMultiSelect.svelte';

  type Tag = { uuid: string; name: string; source?: string | null; usage_count?: number };
  type AISuggestion = { name: string; confidence: number; rationale?: string };

  export let fileId = "";
  export let tags: Tag[] = [];
  export let aiSuggestions: AISuggestion[] = [];

  // Filter AI suggestions to only show ones not already applied
  $: filteredAISuggestions = aiSuggestions.filter(suggestion =>
    !tags.some(tag => tag.name.toLowerCase() === suggestion.name.toLowerCase())
  );

  $: autoAppliedTagNames = tags
    .filter(tag => tag.source === 'auto_ai')
    .map(tag => tag.name);

  // Ensure tags are always in the correct format
  $: {
    if (Array.isArray(tags)) {
      // Handle case where backend returns tag names as strings instead of objects
      tags = tags.map((tag: Tag | string): Tag => {
        if (typeof tag === 'string') {
          // Convert string tag to object format with a temporary UUID
          return { uuid: `temp-${tag}`, name: tag };
        } else if (tag && typeof tag === 'object') {
          // Ensure tag object has required properties - preserve uuid and source
          return {
            uuid: tag.uuid !== undefined ? tag.uuid : `temp-${tag.name}`,
            name: tag.name || '',
            source: tag.source || undefined
          };
        }
        return tag;
      });
    }
  }

  let allTags: Tag[] = [];
  let newTagInput = '';
  let loading = false;

  // Event dispatcher
  const dispatch = createEventDispatcher<{
    tagsUpdated: { tags: Tag[] };
    aiSuggestionAccepted: { suggestion: AISuggestion };
  }>();

  // Fetch all available tags
  async function fetchAllTags() {
    try {
      const fetched = await listTags();

      // Ensure all tags have valid IDs before adding them to the allTags array
      allTags = (fetched || []).filter(
        (tag) => tag && typeof tag === 'object' && !!tag.uuid && !!tag.name
      );
    } catch (err: unknown) {
      console.error('[TagsEditor] Error fetching tags:', err);
      const status = getErrorStatus(err);
      if (status === 401) {
        toastStore.error($t('tags.unauthorizedLogin'));
      } else if (getErrorCode(err) === 'ERR_NETWORK') {
        toastStore.error($t('tags.networkError'));
      } else {
        toastStore.error($t('tags.failedToLoad'));
      }
    }
  }

  // Add a tag to the file
  async function addTag(tagId: string) {
    loading = true;
    try {
      // Adding tag to file

      // Get the tag name by ID and send tag_data with name field as required by backend
      const tagToAdd = allTags.find(t => t.uuid === tagId);
      if (!tagToAdd) {
        console.error(`[TagsEditor] Tag with ID ${tagId} not found in allTags`);
        throw new Error('Tag not found');
      }

      // Use the response from add_tag_to_file as the final tag object
      const finalTag = await addTagToFile(fileId, tagToAdd.name);

      // Ensure the tag has a valid UUID and name
      if (!finalTag || !finalTag.uuid || !finalTag.name) {
        console.error('[TagsEditor] Invalid tag received from server:', finalTag);
        throw new Error('Server returned an invalid tag');
      }

      // Only add the tag if it's not already present
      if (!tags.some(t => t.uuid === finalTag.uuid)) {
        tags = [...tags, finalTag];
        dispatch('tagsUpdated', { tags });
      }
    } catch (err: unknown) {
      if (getErrorStatus(err) === 401) {
        toastStore.error($t('tags.unauthorizedLogin'));
      } else {
        toastStore.error($t('tags.failedToAdd'));
      }
      console.error('[TagsEditor] Error adding tag:', err);
    } finally {
      loading = false;
    }
  }

  // Create a new tag and add it to the file
  async function createAndAddTag() {
    if (!newTagInput.trim()) return;
    loading = true;
    try {
      // Creating tag and adding to file

      // Step 1: Create the tag
      const newTag = await createTag(newTagInput.trim());

      // Ensure the tag has a valid UUID and name to prevent 'undefined' key errors
      if (!newTag || !newTag.uuid) {
        console.error('[TagsEditor] Invalid tag received from server:', newTag);
        throw new Error('Server returned an invalid tag');
      }

      // Then add the tag to the file, similar to addTag but using the new tag data
      const finalTag = await addTagToFile(fileId, newTag.name);
      if (!finalTag || !finalTag.uuid) {
        console.error('[TagsEditor] Invalid tag received after adding to file:', finalTag);
        throw new Error('Server returned an invalid tag after adding to file');
      }

      // Add to allTags if it's not already present
      if (!allTags.some(t => t.uuid === finalTag.uuid)) {
        allTags = [...allTags, finalTag];
      }

      // Add to tags if it's not already present
      if (!tags.some(t => t.uuid === finalTag.uuid)) {
        tags = [...tags, finalTag];
        newTagInput = '';
        dispatch('tagsUpdated', { tags });
      } else {
        newTagInput = '';
      }
    } catch (err: unknown) {
      // Safely check for error properties
      if (err && typeof err === 'object') {
        const errorObj = err as any;

        if (errorObj.response && errorObj.response.status === 401) {
          toastStore.error($t('tags.unauthorizedLogin'));
        } else if (
          errorObj.response &&
          typeof errorObj.response === 'object' &&
          'data' in errorObj.response &&
          errorObj.response.data &&
          typeof errorObj.response.data === 'object' &&
          'detail' in errorObj.response.data
        ) {
          toastStore.error($t('tags.failedToCreateWithDetail', { detail: errorObj.response.data.detail }));
        } else {
          toastStore.error($t('tags.failedToCreate'));
        }
      } else {
        toastStore.error($t('tags.failedToCreate'));
      }
      console.error('[TagsEditor] Error creating tag:', err);
    } finally {
      loading = false;
    }
  }

  // Remove a tag from the file
  async function removeTag(tagId: string) {
    loading = true;
    try {
      // Removing tag from file

      // Find the tag by ID to get its name - the backend needs tag name, not ID
      const tagToRemove = tags.find(t => t.uuid === tagId);
      if (!tagToRemove) {
        console.error(`[TagsEditor] Tag with ID ${tagId} not found in tags list`);
        throw new Error('Tag not found');
      }

      await removeTagFromFile(fileId, tagToRemove.name);

      // Update the local tags array
      // Filter out the removed tag using a safe comparison
      const updatedTags = tags.filter(t =>
        t.uuid !== undefined && t.uuid !== null && t.uuid !== tagId
      );

      // Only update if something actually changed
      if (updatedTags.length !== tags.length) {
        tags = updatedTags;
        dispatch('tagsUpdated', { tags });
      }
    } catch (err: unknown) {
      console.error('[TagsEditor] Error removing tag:', err);
      if (getErrorStatus(err) === 401) {
        toastStore.error($t('tags.unauthorizedLogin'));
      } else if (getErrorCode(err) === 'ERR_NETWORK') {
        toastStore.error($t('tags.networkError'));
      } else {
        toastStore.error($t('tags.failedToRemove'));
      }
    } finally {
      loading = false;
    }
  }

  // Handle keydown event in the input field
  function handleInputKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter' && newTagInput.trim()) {
      event.preventDefault();
      createAndAddTag();
    }
  }

  let suggestedTags: Tag[] = [];
  let dropdownTags: Array<{ id: string; name: string; count: number }> = []; // All available tags for dropdown

  // Get top 5 suggested tags as chips (most used)
  $: suggestedTags = allTags
    .filter(tag => {
      const isAssigned = tags.some(t => (t.uuid === tag.uuid) || (t.name === tag.name));
      if (isAssigned) return false;

      const isInAISuggestions = filteredAISuggestions.some(aiSug =>
        aiSug.name.toLowerCase() === tag.name.toLowerCase()
      );
      if (isInAISuggestions) return false;

      return true;
    })
    .sort((a, b) => (b.usage_count || 0) - (a.usage_count || 0)) // Sort by usage count
    .slice(0, 5); // Top 5 most used

  // Get all available tags for dropdown (excluding already assigned and AI suggestions)
  $: dropdownTags = allTags
    .filter(tag => {
      const isAssigned = tags.some(t => (t.uuid === tag.uuid) || (t.name === tag.name));
      if (isAssigned) return false;

      const isInAISuggestions = filteredAISuggestions.some(aiSug =>
        aiSug.name.toLowerCase() === tag.name.toLowerCase()
      );
      if (isInAISuggestions) return false;

      return true;
    })
    .map(tag => ({
      id: tag.uuid,
      name: tag.name,
      count: tag.usage_count || 0
    }));

  // Handle multiselect tag selection
  async function handleTagSelect(event: CustomEvent<{ id: string | number }>) {
    const { id } = event.detail;
    await addTag(String(id));
  }

  // Handle AI suggestion acceptance
  async function handleAcceptAISuggestion(event: CustomEvent<{ suggestion: AISuggestion }>) {
    const { suggestion } = event.detail;
    newTagInput = suggestion.name;
    await createAndAddTag();
    newTagInput = '';
    // Don't remove from aiSuggestions - let reactive filtering handle it
    // This allows suggestions to reappear if the tag is later removed
    dispatch('aiSuggestionAccepted', { suggestion });
  }

  onMount(() => {
    fetchAllTags();
  });
</script>

<div class="tags-editor">
  <div class="tags-list">
    {#if tags.length === 0}
      <span class="no-tags">{$t('tags.noTagsYet')}</span>
    {/if}
    {#each tags.filter(t => t && t.uuid !== undefined) as tag (tag.uuid)}
      <span class="tag" class:ai-tag={tag.source === 'auto_ai'}>
        {#if tag.source === 'auto_ai'}
          <span class="ai-icon" title={$t('autoLabel.autoAppliedTag')}>
            <svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>
          </span>
        {/if}
        {tag.name}
        <button class="tag-remove" on:click={() => removeTag(tag.uuid)} title={$t('tags.removeTag')}>×</button>
      </span>
    {/each}

    <div class="tag-input-container">
      <input
        type="text"
        placeholder={$t('tags.addTagPlaceholder')}
        bind:value={newTagInput}
        on:keydown={handleInputKeydown}
        class="tag-input"
        disabled={loading}
        title={$t('tags.typeNewTagHint')}
      >
      {#if newTagInput.trim()}
        <button
          class="tag-add-button"
          on:click={createAndAddTag}
          disabled={loading}
          title={$t('tags.createAndAddHint', { tagName: newTagInput.trim() })}
        >
          {$t('tags.add')}
        </button>
      {/if}
    </div>
  </div>

  <!-- AI Suggestions Dropdown -->
  <AISuggestionsDropdown
    suggestions={filteredAISuggestions}
    type="tag"
    {loading}
    autoAppliedTags={autoAppliedTagNames}
    on:accept={handleAcceptAISuggestion}
  />

  {#if suggestedTags.length > 0}
    <div class="suggested-tags">
      <span class="suggested-label">{$t('tags.suggested')}</span>
      {#each suggestedTags.filter(t => t && t.uuid !== undefined) as tag (tag.uuid)}
        <button
          class="suggested-tag"
          on:click={() => addTag(tag.uuid)}
          disabled={loading}
          title={$t('tags.addExistingTagHint', { tagName: tag.name })}
        >
          {tag.name}
        </button>
      {/each}
    </div>
  {/if}

  {#if dropdownTags.length > 0}
    <div class="dropdown-section">
      <span class="dropdown-label">{$t('tags.selectFromAllTags')}</span>
      <SearchableMultiSelect
        options={dropdownTags}
        selectedIds={[]}
        placeholder={$t('tags.addFromLibraryPlaceholder')}
        showCounts={true}
        on:select={handleTagSelect}
      />
    </div>
  {/if}
</div>

<style>
  .tags-editor {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .no-tags {
    color: var(--text-secondary);
    font-size: 0.85rem;
    font-style: italic;
    padding: 0.5rem 0;
  }

  .tags-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
  }

  .tag {
    background-color: rgba(59, 130, 246, 0.1);
    color: var(--primary-color);
    padding: 0.35rem 0.5rem;
    border-radius: 4px;
    font-size: 0.8rem;
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }

  .tag-remove {
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

  .tag-remove:hover {
    color: var(--error-color);
  }

  .tag-input-container {
    display: flex;
    align-items: center;
  }

  .tag-input {
    background: transparent;
    border: none;
    border-bottom: 1px dashed var(--border-color);
    padding: 0.35rem 0;
    font-size: 0.8rem;
    width: 100px;
    color: var(--text-color);
  }

  .tag-input:focus {
    border-bottom-color: var(--primary-color);
    outline: none;
  }

  .tag-add-button {
    background-color: #3b82f6;
    color: white;
    border: none;
    border-radius: 4px;
    padding: 0.25rem 0.5rem;
    font-size: 0.8rem;
    cursor: pointer;
    margin-left: 0.5rem;
  }

  .tag-add-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .suggested-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
    font-size: 0.8rem;
  }

  .suggested-label {
    color: var(--text-light);
  }

  .suggested-tag {
    background: none;
    border: 1px dashed var(--border-color);
    border-radius: 4px;
    padding: 0.25rem 0.5rem;
    font-size: 0.8rem;
    cursor: pointer;
    color: var(--text-light);
  }

  .suggested-tag:hover {
    border-color: var(--primary-color);
    color: var(--primary-color);
    background-color: rgba(59, 130, 246, 0.05);
  }

  .suggested-tag:disabled {
    opacity: 0.6;
    cursor: not-allowed;
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

  .ai-tag {
    background-color: rgba(59, 130, 246, 0.08);
    border: 1px solid rgba(59, 130, 246, 0.25);
  }

  .ai-icon {
    color: var(--ai-accent-color, var(--primary-color, #3b82f6));
    display: flex;
    align-items: center;
  }

  :global([data-theme='dark']) .ai-tag {
    background-color: rgba(96, 165, 250, 0.12);
    border-color: rgba(96, 165, 250, 0.3);
  }
</style>
