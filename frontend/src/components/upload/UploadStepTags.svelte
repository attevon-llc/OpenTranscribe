<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import './upload-shared.css';

  export let selectedTags: string[] = [];
  export let availableTags: Array<{uuid: string; name: string; usage_count: number}> = [];
  export let hasPrevious = false;

  const dispatch = createEventDispatcher<{
    tagsChange: { tags: string[] };
    clearPrevious: void;
  }>();

  let filterQuery = '';
  let newTagName = '';

  $: filteredTags = availableTags.filter(tag =>
    tag.name.toLowerCase().includes(filterQuery.toLowerCase())
  );

  /**
   * Reactive so the checkboxes actually follow the selection — see the same note
   * in UploadStepCollections. `checked={isSelected(name)}` named a function, so
   * Svelte never tracked `selectedTags` as a dependency and the checkbox kept a
   * stale state after a chip was removed.
   */
  $: selectedTagNames = new Set(selectedTags);

  function isSelected(name: string): boolean {
    return selectedTags.includes(name);
  }

  function toggleTag(name: string) {
    if (isSelected(name)) {
      selectedTags = selectedTags.filter(t => t !== name);
    } else {
      selectedTags = [...selectedTags, name];
    }
    dispatch('tagsChange', { tags: selectedTags });
  }

  function createTag() {
    const name = newTagName.trim().slice(0, 50);
    if (!name || selectedTags.includes(name)) return;
    selectedTags = [...selectedTags, name];
    dispatch('tagsChange', { tags: selectedTags });
    newTagName = '';
  }

  function handleCreateKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault();
      e.stopPropagation();
      createTag();
    }
  }

  function removeTag(name: string) {
    selectedTags = selectedTags.filter(t => t !== name);
    dispatch('tagsChange', { tags: selectedTags });
  }
</script>

<div class="step-tags">
  <p class="step-hint">{$t('uploader.tagsHint')}</p>

  {#if hasPrevious && selectedTags.length > 0}
    <div class="previous-banner">
      <span class="previous-banner-text">{$t('uploader.usingPrevious')}</span>
      <button type="button" class="previous-banner-clear" on:click={() => dispatch('clearPrevious')}>
        {$t('uploader.clearPrevious')}
      </button>
    </div>
  {/if}

  <!-- Selected chips -->
  {#if selectedTags.length > 0}
    <div class="selected-chips">
      {#each selectedTags as tag}
        <span class="chip tag-chip">
          {tag}
          <button type="button" class="chip-remove" on:click={() => removeTag(tag)} title={$t('uploader.removeItem')}>×</button>
        </span>
      {/each}
    </div>
  {/if}

  <!-- Filter (only show if there are enough tags) -->
  {#if availableTags.length > 6}
    <input
      type="text"
      class="item-filter"
      placeholder={$t('upload.filterTags')}
      bind:value={filterQuery}
    />
  {/if}

  <!-- Tag list -->
  {#if filteredTags.length > 0}
    <div class="item-list">
      {#each filteredTags as tag}
        <label class="item-row">
          <input
            type="checkbox"
            checked={selectedTagNames.has(tag.name)}
            on:change={() => toggleTag(tag.name)}
          />
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"></path>
            <line x1="7" y1="7" x2="7.01" y2="7"></line>
          </svg>
          <span class="item-name">{tag.name}</span>
          {#if tag.usage_count > 0}
            <span class="item-count">({tag.usage_count})</span>
          {/if}
        </label>
      {/each}
    </div>
  {:else if filterQuery}
    <p class="empty-text">{$t('upload.noTagsMatch', { query: filterQuery })}</p>
  {:else}
    <p class="empty-text">{$t('upload.noTagsYet')}</p>
  {/if}

  <!-- Create new -->
  <div class="create-row">
    <input
      type="text"
      class="create-input"
      placeholder={$t('uploader.addTagPlaceholder')}
      bind:value={newTagName}
      on:keydown={handleCreateKeydown}
    />
    <button
      type="button"
      class="create-btn"
      on:click={createTag}
      disabled={!newTagName.trim()}
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="12" y1="5" x2="12" y2="19"></line>
        <line x1="5" y1="12" x2="19" y2="12"></line>
      </svg>
      {$t('tags.add')}
    </button>
  </div>
</div>
