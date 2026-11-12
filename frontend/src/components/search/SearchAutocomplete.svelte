<script lang="ts">
  import { resolve } from '$app/paths';
  import { createEventDispatcher, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import { anchoredPanel } from '$lib/actions/anchoredPanel';
  import { getErrorCode } from '$lib/utils/apiError';
  import { createDebouncedHandler } from '$lib/utils/debounce';

  export let value: string = '';
  export let placeholder: string = '';

  const dispatch = createEventDispatcher();

  interface SearchSuggestion {
    type?: string;
    text?: string;
    title?: string;
    file_uuid?: string;
  }

  let suggestions: SearchSuggestion[] = [];
  let showSuggestions = false;
  let selectedIndex = -1;
  let inputEl: HTMLInputElement;
  let abortController: AbortController | null = null;
  let suppressFocusReopen = false;
  let searchExecuted = false;
  let isFocused = false;
  const debouncedFetch = createDebouncedHandler(() => fetchSuggestions(value), 200);

  onDestroy(() => {
    debouncedFetch.cleanup();
    abortController?.abort();
  });

  async function fetchSuggestions(query: string) {
    if (query.length < 2) {
      suggestions = [];
      showSuggestions = false;
      return;
    }

    // Cancel any in-flight request
    abortController?.abort();
    abortController = new AbortController();

    try {
      const res = await axiosInstance.get('/search/suggestions', {
        params: { q: query, limit: 8 },
        signal: abortController.signal,
      });
      suggestions = res.data || [];
      showSuggestions = suggestions.length > 0;
      selectedIndex = -1;
    } catch (e: unknown) {
      // Ignore cancelled requests
      if (getErrorCode(e) === 'ERR_CANCELED') return;
      suggestions = [];
      showSuggestions = false;
    }
  }

  function handleInput() {
    searchExecuted = false;

    // Emptying the field by typing must clear results exactly like the ✕ button
    // does (#742). Without this the page keeps rendering the previous results
    // over an empty search box. Focus is deliberately NOT moved here — unlike
    // handleClear(), the user is already in the input.
    if (value === '') {
      debouncedFetch.cleanup();
      abortController?.abort();
      suggestions = [];
      showSuggestions = false;
      selectedIndex = -1;
      dispatch('clear');
      return;
    }

    debouncedFetch.trigger();
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault();
      // Cancel any pending fetch to prevent suggestions from reappearing
      debouncedFetch.cleanup();
      abortController?.abort();

      const picked = selectedIndex >= 0 ? suggestions[selectedIndex] : undefined;
      if (showSuggestions && picked) {
        selectSuggestion(picked);
      } else {
        // Execute search and clear suggestions
        showSuggestions = false;
        suggestions = [];
        selectedIndex = -1;
        suppressFocusReopen = true;
        searchExecuted = true;
        dispatch('search');
      }
      return;
    }

    if (!showSuggestions) {
      return;
    }

    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        selectedIndex = Math.min(selectedIndex + 1, suggestions.length - 1);
        break;
      case 'ArrowUp':
        e.preventDefault();
        selectedIndex = Math.max(selectedIndex - 1, -1);
        break;
      case 'Escape':
        showSuggestions = false;
        selectedIndex = -1;
        break;
    }
  }

  function selectSuggestion(suggestion: SearchSuggestion) {
    showSuggestions = false;
    selectedIndex = -1;

    // If it's a file suggestion, navigate directly to the file
    if (suggestion.type === 'title' && suggestion.file_uuid) {
      void goto(resolve('/files/[id]', { id: suggestion.file_uuid }));
      return;
    }

    // Otherwise, populate search input for other suggestion types
    value = suggestion.text || suggestion.title || '';
    dispatch('select', value);
  }

  function handleBlur() {
    isFocused = false;
    // Delay to allow click on suggestion
    setTimeout(() => {
      showSuggestions = false;
    }, 200);
  }

  function handleFocus() {
    isFocused = true;
    if (suppressFocusReopen) {
      suppressFocusReopen = false;
      return;
    }
    // Don't reopen suggestions after search was executed - wait for new typing
    if (searchExecuted) {
      return;
    }
    if (suggestions.length > 0 && value.length >= 2) {
      showSuggestions = true;
    }
  }

  function handleClear() {
    value = '';
    suggestions = [];
    showSuggestions = false;
    selectedIndex = -1;
    searchExecuted = false;
    inputEl?.focus();
    dispatch('clear');
  }

  function getSuggestionIcon(type: string): string {
    switch (type) {
      case 'title': return 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z';
      case 'speaker': return 'M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2';
      default: return 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z';
    }
  }
</script>

<div class="autocomplete-wrapper">
  <input
    bind:this={inputEl}
    bind:value
    on:input={handleInput}
    on:keydown={handleKeydown}
    on:blur={handleBlur}
    on:focus={handleFocus}
    type="text"
    class="search-input"
    class:has-value={value.length > 0}
    {placeholder}
    autocomplete="off"
    role="combobox"
    aria-controls="search-suggestions-listbox"
    aria-expanded={showSuggestions}
    aria-autocomplete="list"
  />

  {#if value.length > 0}
    <button class="clear-btn" on:mousedown|preventDefault={handleClear} title={$t('search.clearSearch')} aria-label={$t('search.clearSearch')}>
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"></circle>
        <line x1="15" y1="9" x2="9" y2="15"></line>
        <line x1="9" y1="9" x2="15" y2="15"></line>
      </svg>
    </button>
  {/if}

  {#if showSuggestions}
    <div
      class="suggestions-dropdown"
      role="listbox"
      id="search-suggestions-listbox"
      use:anchoredPanel={{ anchor: inputEl }}
    >
      {#each suggestions as suggestion, i (i)}
        <button
          class="suggestion-item"
          class:selected={i === selectedIndex}
          on:mousedown|preventDefault={() => selectSuggestion(suggestion)}
          role="option"
          aria-selected={i === selectedIndex}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d={getSuggestionIcon(suggestion.type || 'content')}></path>
          </svg>
          <span class="suggestion-text">{suggestion.text || suggestion.title || suggestion}</span>
          {#if suggestion.type}
            <span class="suggestion-type">{suggestion.type}</span>
          {/if}
        </button>
      {/each}
    </div>
  {/if}

  {#if value.startsWith('speaker:') && !showSuggestions && !searchExecuted && isFocused}
    <div class="operator-hint">
      <span class="hint-label">{$t('search.searchTips')}</span>
      <code>{$t('search.speakerOperatorExample')}</code>
    </div>
  {/if}
</div>

<style>
  .autocomplete-wrapper {
    position: relative;
    flex: 1;
  }

  .search-input {
    width: 100%;
    height: 44px;
    padding: 0 var(--space-4);
    font-size: var(--font-size-body);
    color-scheme: light dark;
  }

  .search-input.has-value {
    padding-right: var(--space-10);
  }

  .clear-btn {
    position: absolute;
    right: 0.5rem;
    top: 0;
    bottom: 0;
    margin: auto 0;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    padding: 0;
    border: none;
    border-radius: var(--radius-round);
    background: none;
    color: var(--text-secondary);
    cursor: pointer;
    transition: color var(--duration-fast), background-color var(--duration-fast);
  }

  .clear-btn:hover {
    color: var(--text-color);
    background-color: var(--hover-color);
  }

  /* Placed by anchoredPanel: fixed on <body>, flips above, scrolls when short on room. */
  .suggestions-dropdown {
    background: var(--card-background);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    box-shadow: var(--dropdown-shadow);
    overflow-y: auto;
  }

  .suggestion-item {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    padding: var(--space-2) var(--space-3);
    border: none;
    background: none;
    color: var(--text-color);
    font-size: var(--font-size-sm);
    text-align: start;
    cursor: pointer;
    transition: background var(--duration-fast);
  }

  .suggestion-item:hover,
  .suggestion-item.selected {
    background: var(--hover-color);
  }

  .suggestion-item svg {
    color: var(--text-secondary);
    flex-shrink: 0;
  }

  .suggestion-text {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .suggestion-type {
    font-size: var(--font-size-2xs);
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .operator-hint {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    margin-top: var(--space-1);
    padding: var(--space-2) var(--space-3);
    background: var(--card-background);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    box-shadow: var(--dropdown-shadow);
    z-index: var(--z-dropdown);
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .hint-label {
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
  }

  .operator-hint code {
    font-family: monospace;
    font-size: var(--font-size-xs);
    background: rgba(var(--primary-color-rgb), 0.08);
    color: var(--primary-on-surface);
    padding: var(--space-0-5) var(--space-1-5);
    border-radius: var(--radius-sm);
  }
</style>
