<!--
  PickerTagsTab.svelte — pick by tag.

  Rendered as toggle chips rather than a checkbox list: tags are short, numerous
  and usually chosen a couple at a time, so chips make the current selection
  readable at a glance.
-->
<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';

  export let selected: string[] = [];

  const dispatch = createEventDispatcher<{ change: string[] }>();

  interface PickerTag {
    name: string;
    count?: number;
  }

  let tags: PickerTag[] = [];
  let loading = true;
  let filter = '';

  $: selectedSet = new Set(selected);
  $: visible = filter.trim()
    ? tags.filter((tag) => tag.name.toLowerCase().includes(filter.trim().toLowerCase()))
    : tags;

  onMount(async () => {
    try {
      const { data } = await axiosInstance.get('/tags');
      tags = Array.isArray(data) ? data : (data?.items ?? []);
    } catch {
      tags = [];
    } finally {
      loading = false;
    }
  });

  function toggle(name: string): void {
    dispatch(
      'change',
      selectedSet.has(name) ? selected.filter((n) => n !== name) : [...selected, name]
    );
  }
</script>

<div class="picker-tab">
  {#if loading}
    <div class="loading"><Spinner size="small" /></div>
  {:else}
    <input
      class="tag-filter"
      type="search"
      bind:value={filter}
      placeholder={$t('chat.picker.searchTags')}
      aria-label={$t('chat.picker.searchTags')}
    />

    <div class="tag-cloud" data-testid="picker-tags-list">
      {#each visible as tag (tag.name)}
        <button
          type="button"
          class="tag-chip"
          class:selected={selectedSet.has(tag.name)}
          on:click={() => toggle(tag.name)}
          aria-pressed={selectedSet.has(tag.name)}
          data-testid="picker-tag-chip"
        >
          {tag.name}
          {#if tag.count !== undefined}<span class="tag-count">{tag.count}</span>{/if}
        </button>
      {/each}

      {#if visible.length === 0}
        <p class="empty">{$t('chat.picker.emptyTags')}</p>
      {/if}
    </div>
  {/if}
</div>

<style>
  .picker-tab {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    min-height: 0;
  }

  .loading {
    display: flex;
    justify-content: center;
    padding: 2rem 0;
  }

  .tag-filter {
    width: 100%;
    padding: 0.45rem 0.7rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.87rem;
  }

  .tag-filter:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  .tag-cloud {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    overflow-y: auto;
    max-height: 42vh;
    align-content: flex-start;
  }

  .tag-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.3rem 0.7rem;
    border: 1px solid var(--border-color);
    border-radius: 999px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8rem;
    cursor: pointer;
    transition:
      border-color 0.15s ease,
      background-color 0.15s ease;
  }

  .tag-chip:hover {
    background-color: var(--button-hover);
  }

  .tag-chip.selected {
    border-color: var(--primary-color);
    background-color: rgba(var(--primary-color-rgb), 0.12);
    color: var(--primary-color);
    font-weight: 500;
  }

  .tag-count {
    font-size: 0.72rem;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .empty {
    width: 100%;
    padding: 1.5rem 0.5rem;
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.85rem;
    margin: 0;
  }
</style>
