<script lang="ts" generics="T">
  import { createEventDispatcher, onDestroy } from 'svelte';
  import { clickOutside } from '$lib/actions/clickOutside';
  import { t } from '$stores/locale';
  import { anchoredPanel } from '$lib/actions/anchoredPanel';

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
  let inputEl: HTMLInputElement;
  /** Portaled to <body> by anchoredPanel, so outside the wrapper. */
  let panelEl: HTMLDivElement | undefined;

  const listboxId = 'searchable-select-listbox';
  const optionId = (i: number) => `searchable-select-option-${i}`;

  function scheduleSearch() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => void runSearch(), debounceMs);
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
          const item = results[highlighted];
          if (item !== undefined) selectItem(item);
        }
        break;
      default:
        break;
    }
  }
</script>

<div class="searchable-select" use:clickOutside={{ enabled: open, ignore: [panelEl ?? null] }} on:click_outside={close}>
  <input
    bind:this={inputEl}
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
    <div
      class="searchable-dropdown"
      bind:this={panelEl}
      use:anchoredPanel={{ anchor: inputEl, maxHeight: 280 }}
    >
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
  }
  .searchable-input::placeholder {
    color: var(--text-secondary);
  }
  /* Placed by anchoredPanel (fixed on <body>, flips above, max-height capped to the room). */
  .searchable-dropdown {
    overflow-y: auto;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
  }
  .searchable-list {
    list-style: none;
    margin: 0;
    padding: var(--space-1);
  }
  .searchable-option {
    padding: var(--space-2) var(--space-2-5);
    border-radius: var(--radius-md);
    color: var(--text-color);
    font-size: var(--font-size-md);
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
    padding: var(--space-4) var(--space-3);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }
  .searchable-spinner {
    width: 16px;
    height: 16px;
    border: 2px solid var(--border-color);
    border-top-color: var(--primary-color);
    border-radius: var(--radius-round);
    animation: searchable-spin 0.7s linear infinite;
  }
  @keyframes searchable-spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
