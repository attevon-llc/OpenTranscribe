<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { slide } from 'svelte/transition';
  import { t } from '$stores/locale';
  import type { Tag } from '$lib/types/tag';
  import TagsEditor from './TagsEditor.svelte';

  export let file: any = null;
  export let isTagsExpanded: boolean = false;
  export let aiTagSuggestions: Array<{name: string, confidence: number, rationale?: string}> = [];

  const dispatch = createEventDispatcher<{ tagsUpdated: { tags: Tag[] } }>();

  function toggleTags() {
    isTagsExpanded = !isTagsExpanded;
  }

  // The file-detail page owns `file`; forward the editor's update instead of
  // mutating the prop, so the page can update its state and `reactiveFile`.
  function forwardTagsUpdated(event: CustomEvent<{ tags: Tag[] }>) {
    dispatch('tagsUpdated', event.detail);
  }
</script>

<div class="tags-dropdown-section">
  <button
    class="tags-header"
    on:click={toggleTags}
    on:keydown={e => e.key === 'Enter' && toggleTags()}
    title={$t('tags.toggleEditorHint')} aria-expanded={isTagsExpanded}>
    <h4 class="section-heading">{$t('tags.title')}</h4>
    <div class="tags-preview">
      {#if file?.tags && file.tags.length > 0}
        {#each file.tags.slice(0, 3) as tag, i}
          <span class="tag-chip">{tag && tag.name ? tag.name : tag}</span>
        {/each}
        {#if file.tags.length > 3}
          <span class="tag-chip more">{$t('tags.moreCount', { count: file.tags.length - 3 })}</span>
        {/if}
      {:else}
        <span class="no-tags">{$t('tags.noTags')}</span>
      {/if}
    </div>
    <span class="dropdown-toggle" aria-hidden="true">
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="transform: rotate({isTagsExpanded ? '180deg' : '0deg'})">
        <polyline points="6 9 12 15 18 9"></polyline>
      </svg>
    </span>
  </button>

  {#if isTagsExpanded}
    <div class="tags-content" transition:slide={{ duration: 200 }}>
      {#if file && file.uuid}
        <TagsEditor
          fileId={String(file.uuid)}
          tags={file.tags || []}
          aiSuggestions={aiTagSuggestions}
          on:tagsUpdated={forwardTagsUpdated}
        />
      {:else}
        <p>{$t('tags.loadingTags')}</p>
      {/if}
    </div>
  {/if}
</div>

<style>
  .tags-dropdown-section {
    margin-bottom: 0;
  }

  .tags-header {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 12px 16px;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .tags-header:hover {
    background: var(--surface-hover);
    border-color: var(--border-hover);
  }

  .section-heading {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .tags-preview {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }

  .tag-chip {
    background: var(--primary-light);
    color: var(--primary-color);
    padding: 3px 8px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 500;
    white-space: nowrap;
  }

  .tag-chip.more {
    background: var(--surface-secondary);
    color: var(--text-secondary);
  }

  .no-tags {
    color: var(--text-secondary);
    font-size: 12px;
    font-style: italic;
  }

  .dropdown-toggle svg {
    transition: transform 0.2s ease;
  }

  .tags-content {
    border: 1px solid var(--border-color);
    border-top: none;
    border-radius: 0 0 8px 8px;
    background: var(--surface-color);
    padding: 20px;
  }

  .tags-content p {
    margin: 0;
    color: var(--text-secondary);
    font-style: italic;
    text-align: center;
    padding: 20px;
  }
</style>
