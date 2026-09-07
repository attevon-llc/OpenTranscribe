<script lang="ts" generics="T">
  import { createEventDispatcher, onDestroy } from 'svelte';
  import { clickOutside } from '$lib/actions/clickOutside';
  import { t } from '$stores/locale';

  /** Placeholder for the search input. Caller should pass translated text. */
  export let placeholder = '';
  /** The query text. Bindable: `<SearchableSelect bind:value>`. */
  export let value = '';
  /** Async fetcher invoked with the (debounced) query. */
  export let fetchFn: (q: string) => Promise<T[]>;
  /** How to render each result as a string. */
  export let getLabel: (item: T) => string;
  /** Minimum number of characters before searching. */
  export let minChars = 1;
  /** Debounce delay in milliseconds. */
  export let debounceMs = 250;
  /** Shown when a search yields no results. Defaults to the translated "No results". */
  export let emptyLabel: string | undefined = undefined;

  $: resolvedEmptyLabel = emptyLabel ?? $t('common.noResults');

  const dispatch = createEventDispatcher<{ select: T }>();

  let results: T[] = [];
  let open = false;
  let loading = false;
  let highlighted = -1;
  let timer: ReturnType<typeof setTimeout> | null = null;
  /** Guards against stale async responses overwriting newer ones. */
  let requestSeq = 0;

  const listboxId = 'searchable-select-listbox';
  const optionId = (i: number) => `searchable-select-option-${i}`;

  function scheduleSearch() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(runSearch, debounceMs);
  }

  onDestroy(() => {
    if (timer) clearTimeout(timer);
  });

  async function runSearch() {
    const q = value.trim();
    if (q.length < minChars) {
      results = [];
      open = false;
      loading = false;
      highlighted = -1;
      return;
    }
    const seq = ++requestSeq;
    loading = true;
    open = true;
    try {
      const items = await fetchFn(q);
      if (seq !== requestSeq) return;
      results = items;
      highlighted = items.length > 0 ? 0 : -1;
    } catch {
      if (seq !== requestSeq) return;
      results = [];
      highlighted = -1;
    } finally {
      if (seq === requestSeq) loading = false;
    }
  }

  function onInput() {
    scheduleSearch();
  }

  function selectItem(item: T) {
    dispatch('select', item);
    open = false;
    highlighted = -1;
  }

  function close() {
    open = false;
    highlighted = -1;
  }

  function onKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      if (open) {
        event.preventDefault();
        close();
      }
      return;
    }
    if (!open || results.length === 0) {
      if (event.key === 'ArrowDown' && results.length > 0) {
        event.preventDefault();
        open = true;
      }
      return;
    }
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        highlighted = (highlighted + 1) % results.length;
        break;
      case 'ArrowUp':
        event.preventDefault();
        highlighted = (highlighted - 1 + results.length) % results.length;
        break;
      case 'Enter':
        if (highlighted >= 0 && highlighted < results.length) {
          event.preventDefault();
          selectItem(results[highlighted]);
        }
        break;
      default:
        break;
    }
  }
</script>

<div class="searchable-select" use:clickOutside={{ enabled: open }} on:click_outside={close}>
  <input
    type="text"
    class="searchable-input"
    role="combobox"
    aria-expanded={open}
    aria-controls={listboxId}
    aria-autocomplete="list"
    aria-activedescendant={open && highlighted >= 0 ? optionId(highlighted) : undefined}
    {placeholder}
    bind:value
    on:input={onInput}
    on:keydown={onKeydown}
    on:focus={() => {
      if (results.length > 0) open = true;
    }}
  />

  {#if open}
    <div class="searchable-dropdown">
      {#if loading}
        <div class="searchable-status" aria-live="polite">
          <span class="searchable-spinner" aria-hidden="true"></span>
        </div>
      {:else if results.length === 0}
        <div class="searchable-status" aria-live="polite">{resolvedEmptyLabel}</div>
      {:else}
        <ul class="searchable-list" role="listbox" id={listboxId}>
          {#each results as item, i (i)}
            <li
              role="option"
              id={optionId(i)}
              class="searchable-option"
              class:highlighted={i === highlighted}
              aria-selected={i === highlighted}
              on:click={() => selectItem(item)}
              on:keydown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  selectItem(item);
                }
              }}
              on:mouseenter={() => (highlighted = i)}
            >
              {getLabel(item)}
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</div>

<style>
  .searchable-select {
    position: relative;
    width: 100%;
  }
  .searchable-input {
    width: 100%;
    padding: 8px 12px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 14px;
    line-height: 1.4;
  }
  .searchable-input::placeholder {
    color: var(--text-secondary);
  }
  .searchable-input:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
    border-color: var(--primary-color);
  }
  .searchable-dropdown {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    z-index: 1000;
    max-height: 280px;
    overflow-y: auto;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
  }
  .searchable-list {
    list-style: none;
    margin: 0;
    padding: 4px;
  }
  .searchable-option {
    padding: 8px 10px;
    border-radius: 6px;
    color: var(--text-color);
    font-size: 14px;
    cursor: pointer;
  }
  .searchable-option.highlighted {
    background: rgba(var(--primary-color-rgb), 0.12);
    color: var(--primary-on-surface);
  }
  .searchable-status {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px 12px;
    color: var(--text-secondary);
    font-size: 13px;
  }
  .searchable-spinner {
    width: 16px;
    height: 16px;
    border: 2px solid var(--border-color);
    border-top-color: var(--primary-color);
    border-radius: 50%;
    animation: searchable-spin 0.7s linear infinite;
  }
  @keyframes searchable-spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
