<!--
  PickerCollectionsTab.svelte — pick whole collections.

  Selecting a collection is scope-by-reference: the backend expands it to files
  at query time, so a recording added to the collection later is automatically in
  scope for an existing conversation.
-->
<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';

  export let selected: string[] = [];

  const dispatch = createEventDispatcher<{ change: string[] }>();

  interface PickerCollection {
    uuid: string;
    name: string;
    media_count?: number;
  }

  let collections: PickerCollection[] = [];
  let loading = true;

  $: selectedSet = new Set(selected);

  onMount(async () => {
    try {
      const { data } = await axiosInstance.get('/collections', {
        params: { ownership: 'all', limit: 200 },
      });
      collections = Array.isArray(data) ? data : (data?.items ?? []);
    } catch {
      collections = [];
    } finally {
      loading = false;
    }
  });

  function toggle(uuid: string): void {
    dispatch(
      'change',
      selectedSet.has(uuid) ? selected.filter((id) => id !== uuid) : [...selected, uuid]
    );
  }
</script>

<div class="picker-tab">
  {#if loading}
    <div class="loading"><Spinner size="small" /></div>
  {:else}
    <ul class="picker-list" data-testid="picker-collections-list">
      {#each collections as collection (collection.uuid)}
        <li>
          <label class="picker-row">
            <input
              type="checkbox"
              checked={selectedSet.has(collection.uuid)}
              on:change={() => toggle(collection.uuid)}
              data-testid="picker-collection-checkbox"
            />
            <span class="row-label">{collection.name}</span>
            {#if collection.media_count !== undefined}
              <span class="row-count">{collection.media_count}</span>
            {/if}
          </label>
        </li>
      {/each}

      {#if collections.length === 0}
        <li class="empty">{$t('chat.picker.emptyCollections')}</li>
      {/if}
    </ul>
  {/if}
</div>

<style>
  .picker-tab {
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  .loading {
    display: flex;
    justify-content: center;
    padding: 2rem 0;
  }

  .picker-list {
    list-style: none;
    margin: 0;
    padding: 0;
    overflow-y: auto;
    max-height: 46vh;
  }

  .picker-row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.5rem 0.4rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.87rem;
    color: var(--text-color);
  }

  .picker-row:hover {
    background-color: var(--button-hover);
  }

  /* --- selection affordance ---
     A bare native checkbox against a list of similar-looking rows makes the
     selected set genuinely hard to read at a glance. accent-color paints the
     control itself, and the row tint + weight make the selection legible
     without scanning the checkbox column. */
  .picker-row input[type='checkbox'] {
    flex: none;
    width: 1rem;
    height: 1rem;
    accent-color: var(--primary-color);
    cursor: pointer;
  }

  .picker-row:has(input:checked) {
    background-color: rgba(var(--primary-color-rgb), 0.12);
    font-weight: 600;
  }

  .picker-row:focus-within {
    outline: 2px solid var(--primary-color);
    outline-offset: -2px;
  }

  .row-label {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-count {
    flex-shrink: 0;
    font-size: 0.75rem;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .empty {
    padding: 1.5rem 0.5rem;
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.85rem;
  }
</style>
