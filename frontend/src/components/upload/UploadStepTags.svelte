<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import SearchableMultiSelect from '$components/SearchableMultiSelect.svelte';
  import './upload-shared.css';

  export let selectedTags: string[] = [];
  export let availableTags: Array<{uuid: string; name: string; usage_count: number}> = [];
  export let hasPrevious = false;

  const dispatch = createEventDispatcher<{
    tagsChange: { tags: string[] };
    clearPrevious: void;
  }>();

  let newTagName = '';

  /**
   * The same dropdown the filter sidebar uses (UX review item 4). Tags created in this
   * step are not in `availableTags` yet, so they are appended so they stay listed (and
   * deselectable) rather than only appearing as chips.
   */
  $: options = [
    ...availableTags.map((tag) => ({ id: tag.name, name: tag.name, count: tag.usage_count })),
    ...selectedTags
      .filter((name) => !availableTags.some((tag) => tag.name === name))
      .map((name) => ({ id: name, name, count: 0 })),
  ];

  function handleSelect(e: CustomEvent<{ id: string | number }>) {
    const name = String(e.detail.id);
    if (selectedTags.includes(name)) return;
    selectedTags = [...selectedTags, name];
    dispatch('tagsChange', { tags: selectedTags });
  }

  function handleDeselect(e: CustomEvent<{ id: string | number }>) {
    removeTag(String(e.detail.id));
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

  {#if availableTags.length > 0 || selectedTags.length > 0}
    <SearchableMultiSelect
      {options}
      selectedIds={selectedTags}
      placeholder={$t('uploader.selectTagsPlaceholder')}
      maxHeight="220px"
      showCounts={true}
      on:select={handleSelect}
      on:deselect={handleDeselect}
    />
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
