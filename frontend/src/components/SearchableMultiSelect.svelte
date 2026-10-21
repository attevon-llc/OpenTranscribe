<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { anchoredPanel } from '$lib/actions/anchoredPanel';

  type Option = { id: string | number; name: string; count?: number };

  export let options: Option[] = []; // Array of {id, name, count?}
  export let selectedIds: Array<string | number> = []; // Array of selected IDs
  export let placeholder = "";
  export let maxHeight = "200px";
  export let disabled = false;
  export let showCounts = false; // Show usage counts in dropdown

  $: resolvedPlaceholder = placeholder || $t('select.placeholder');

  const dispatch = createEventDispatcher<{
    select: { id: string | number };
    deselect: { id: string | number };
    /**
     * The term typed into this dropdown's own search box.
     *
     * Client-side filtering of `options` still happens regardless. This event
     * exists so a consumer whose option list is SERVER-paginated can refetch —
     * without it, a caller had to render a second, separate search input beside
     * the dropdown to reach options beyond the fetched page, which is exactly
     * the redundant double-search the filter sidebar used to show.
     */
    search: { term: string };
  }>();

  let isOpen = false;
  let searchTerm = '';
  let dropdownElement: HTMLDivElement;
  let toggleElement: HTMLButtonElement;
  // The panel is portaled to <body> (anchoredPanel), so it is outside `dropdownElement`.
  let panelElement: HTMLDivElement | undefined;

  // Filter options based on search
  $: filteredOptions = options.filter(option => {
    if (!searchTerm) return true;
    return option.name.toLowerCase().includes(searchTerm.toLowerCase());
  });

  // Sort by count (if available), then alphabetically — never reorder on selection
  $: sortedOptions = [...filteredOptions].sort((a, b) => {
    if (showCounts && a.count !== undefined && b.count !== undefined) {
      if (b.count !== a.count) return b.count - a.count;
    }

    return a.name.localeCompare(b.name);
  });

  function toggleOption(optionId: string | number) {
    if (selectedIds.includes(optionId)) {
      selectedIds = selectedIds.filter(id => id !== optionId);
      dispatch('deselect', { id: optionId });
    } else {
      selectedIds = [...selectedIds, optionId];
      dispatch('select', { id: optionId });
    }
  }

  function handleClickOutside(event: MouseEvent) {
    const target = event.target as Node;
    if (dropdownElement && !dropdownElement.contains(target) && !panelElement?.contains(target)) {
      isOpen = false;
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && isOpen) {
      event.preventDefault();
      event.stopPropagation();
      isOpen = false;
      toggleElement?.focus();
    }
  }

  onMount(() => {
    document.addEventListener('click', handleClickOutside);
    return () => {
      document.removeEventListener('click', handleClickOutside);
    };
  });
</script>

<div class="multiselect-container" bind:this={dropdownElement}>
  <button
    bind:this={toggleElement}
    aria-haspopup="listbox"
    aria-expanded={isOpen}
    on:keydown={handleKeydown}
    class="multiselect-toggle"
    class:disabled
    on:click={() => !disabled && (isOpen = !isOpen)}
    type="button"
  >
    <span class="toggle-text">
      {#if selectedIds.length > 0}
        {$t('select.selected', { count: selectedIds.length })}
      {:else}
        {resolvedPlaceholder}
      {/if}
    </span>
    <svg
      class="toggle-icon"
      class:open={isOpen}
      width="12"
      height="12"
      viewBox="0 0 12 12"
    >
      <path d="M2 4l4 4 4-4" stroke="currentColor" stroke-width="2" fill="none"/>
    </svg>
  </button>

  {#if isOpen}
    <div
      class="dropdown-panel"
      role="presentation"
      bind:this={panelElement}
      use:anchoredPanel={{ anchor: toggleElement }}
      on:keydown={handleKeydown}
    >
      <div class="search-box">
        <input
          type="text"
          placeholder={$t('select.searchPlaceholder')}
          bind:value={searchTerm}
          class="search-input"
          on:click|stopPropagation
          on:input={() => dispatch('search', { term: searchTerm })}
        />
      </div>

      <div class="options-list" style="max-height: {maxHeight}">
        {#if sortedOptions.length === 0}
          <div class="empty-message">{$t('select.noOptions')}</div>
        {:else}
          {#each sortedOptions as option (option.id)}
            <label class="option-item">
              <input
                type="checkbox"
                checked={selectedIds.includes(option.id)}
                on:change={() => toggleOption(option.id)}
                on:click|stopPropagation
              />
              <span class="option-name">{option.name}</span>
              {#if showCounts && option.count !== undefined && option.count > 0}
                <span class="option-count">{option.count}</span>
              {/if}
            </label>
          {/each}
        {/if}
      </div>
    </div>
  {/if}
</div>

<style>
  .multiselect-container {
    position: relative;
    width: 100%;
  }

  .multiselect-toggle {
    width: 100%;
    padding: 0.5rem 0.75rem;
    background-color: var(--card-background);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 0.9rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: space-between;
    transition: all 0.2s;
  }

  .multiselect-toggle:hover:not(.disabled) {
    border-color: var(--primary-color);
    background-color: var(--table-row-hover);
  }

  .multiselect-toggle.disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .toggle-text {
    flex: 1;
    text-align: left;
    color: var(--text-color);
  }

  .toggle-icon {
    transition: transform 0.2s;
    color: var(--text-secondary);
  }

  .toggle-icon.open {
    transform: rotate(180deg);
  }

  /* Placed by anchoredPanel: fixed on <body>, flips above / caps max-height to the room. */
  .dropdown-panel {
    display: flex;
    flex-direction: column;
    background-color: var(--card-background);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    box-shadow: var(--dropdown-shadow);
    overflow: hidden;
  }

  .search-box {
    flex: none;
    padding: 0.5rem;
    border-bottom: 1px solid var(--border-color);
    background-color: var(--card-background);
  }

  .search-input {
    width: 100%;
    padding: 0.4rem 0.6rem;
    font-size: 0.85rem;
  }

  .search-input::placeholder {
    color: var(--text-secondary);
    opacity: 0.7;
  }

  .options-list {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    background-color: var(--card-background);
  }

  .option-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0.75rem;
    cursor: pointer;
    transition: background-color 0.15s;
    user-select: none;
    background-color: var(--card-background);
    width: 100%;
    min-height: 40px;
  }

  .option-item:hover {
    background-color: var(--table-row-hover);
  }

  .option-item input[type="checkbox"] {
    flex-shrink: 0;
    cursor: pointer;
    width: 16px;
    height: 16px;
  }

  .option-name {
    flex: 1;
    font-size: 0.9rem;
    color: var(--text-color);
    text-align: left;
    overflow: visible;
    white-space: normal;
    word-wrap: break-word;
  }


  /* Extra safety - ensure no parent is hiding content */
  .option-item * {
    visibility: visible;
    opacity: 1;
  }

  .option-count {
    background-color: var(--border-color);
    color: var(--text-secondary);
    padding: 0.1rem 0.4rem;
    border-radius: 10px;
    font-size: 0.75rem;
    font-weight: 500;
    margin-left: auto;
    flex-shrink: 0;
  }

  .empty-message {
    padding: 1rem;
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.85rem;
  }
</style>
