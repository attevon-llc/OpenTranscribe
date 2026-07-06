<!--
  SearchBar.svelte — shared search input primitive.

  One styled, accessible, theme-aware search field used across the app (settings
  search, transcript find, summary find) so every surface looks and behaves the
  same. Presentational only: the parent owns the actual matching and passes back a
  counter / loading state.

  Modes:
  - Plain filter (settings): bind:value, listen to `search` (debounced) + forwarded
    `keydown` for arrow navigation of an external results list.
  - Find-in-document (transcript/summary): set `showNav`, feed `counterText`
    ("2 of 17"), `loading`, `statusText`; listen to `next`/`previous`.
-->
<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import { createDebouncedHandler } from '$lib/utils/debounce';
  import Spinner from '$components/ui/Spinner.svelte';

  export let value = '';
  export let placeholder = '';
  export let ariaLabel = '';
  export let disabled = false;
  /** Show an inline spinner + status (two-phase search in progress). */
  export let loading = false;
  /** Show prev/next match navigation buttons (find-in-document mode). */
  export let showNav = false;
  /** Visible match counter, e.g. "2 of 17" or "2 in view". */
  export let counterText = '';
  /** Human status announced politely to assistive tech, e.g. "searching entire transcript…". */
  export let statusText = '';
  /** Style the counter as a "no results" state. */
  export let noResults = false;
  /** Disable prev/next (no matches to move between). */
  export let navDisabled = false;
  export let autofocus = false;
  export let size: 'sm' | 'md' = 'md';
  /** Debounce (ms) before emitting `search`; 0 = immediate. */
  export let debounceMs = 200;
  /** Accessible labels — pass i18n strings; English fallbacks keep the primitive usable standalone. */
  export let clearLabel = 'Clear search';
  export let nextLabel = 'Next match';
  export let previousLabel = 'Previous match';
  /** Optional combobox wiring for consumers with a results listbox. */
  export let inputId: string | undefined = undefined;
  export let role: string | undefined = undefined;
  export let ariaControls: string | undefined = undefined;
  export let ariaExpanded: boolean | undefined = undefined;
  export let ariaActivedescendant: string | undefined = undefined;

  const dispatch = createEventDispatcher<{
    search: { value: string };
    input: { value: string };
    clear: void;
    next: void;
    previous: void;
    escape: void;
    keydown: KeyboardEvent;
    focus: FocusEvent;
    blur: FocusEvent;
  }>();

  let inputEl: HTMLInputElement;

  const debounced = createDebouncedHandler(() => {
    dispatch('search', { value });
  }, debounceMs);

  function handleInput(event: Event) {
    value = (event.target as HTMLInputElement).value;
    dispatch('input', { value });
    if (debounceMs > 0) {
      debounced.trigger();
    } else {
      dispatch('search', { value });
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      dispatch('escape');
    } else if (event.key === 'Enter' && showNav) {
      event.preventDefault();
      if (event.shiftKey) dispatch('previous');
      else dispatch('next');
    }
    // Always forward so consumers can handle Arrow/Enter for external result lists.
    dispatch('keydown', event);
  }

  export function focus() {
    inputEl?.focus();
  }

  export function selectAll() {
    inputEl?.select();
  }

  function clear() {
    value = '';
    debounced.cleanup();
    dispatch('input', { value: '' });
    dispatch('clear');
    inputEl?.focus();
  }

  onMount(() => {
    if (autofocus) inputEl?.focus();
  });

  onDestroy(() => debounced.cleanup());
</script>

<div class="search-bar search-bar-{size}" class:disabled>
  <span class="search-bar-icon" aria-hidden="true">
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      <circle cx="11" cy="11" r="8" />
      <path d="M21 21l-4.35-4.35" />
    </svg>
  </span>

  <input
    bind:this={inputEl}
    {value}
    id={inputId}
    type="text"
    class="search-bar-input"
    {placeholder}
    {disabled}
    {role}
    aria-label={ariaLabel || placeholder}
    aria-controls={ariaControls}
    aria-expanded={ariaExpanded}
    aria-activedescendant={ariaActivedescendant}
    aria-autocomplete={role === 'combobox' ? 'list' : undefined}
    autocomplete="off"
    autocorrect="off"
    autocapitalize="off"
    spellcheck="false"
    on:input={handleInput}
    on:keydown={handleKeydown}
    on:focus={(e) => dispatch('focus', e)}
    on:blur={(e) => dispatch('blur', e)}
  />

  <div class="search-bar-status" aria-live="polite">
    {#if loading}
      <!-- Two-phase: show the provisional (loaded-window) counter next to the spinner
           so progress is visible without hiding what's already found. -->
      <span class="search-bar-loading">
        {#if counterText}<span class="search-bar-counter">{counterText}</span>{/if}
        <Spinner size="small" />
        {#if statusText}<span class="search-bar-counter search-bar-status-text">{statusText}</span>{/if}
      </span>
    {:else if counterText}
      <span class="search-bar-counter" class:no-results={noResults}>{counterText}</span>
    {/if}
  </div>

  {#if showNav}
    <div class="search-bar-nav">
      <button
        type="button"
        class="search-bar-btn"
        on:click={() => dispatch('previous')}
        disabled={disabled || navDisabled}
        aria-label={previousLabel}
        title={previousLabel}
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <polyline points="15,18 9,12 15,6" />
        </svg>
      </button>
      <button
        type="button"
        class="search-bar-btn"
        on:click={() => dispatch('next')}
        disabled={disabled || navDisabled}
        aria-label={nextLabel}
        title={nextLabel}
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <polyline points="9,6 15,12 9,18" />
        </svg>
      </button>
    </div>
  {/if}

  {#if value}
    <button
      type="button"
      class="search-bar-clear"
      on:click={clear}
      {disabled}
      aria-label={clearLabel}
      title={clearLabel}
    >
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <path d="M18 6L6 18M6 6l12 12" />
      </svg>
    </button>
  {/if}
</div>

<style>
  .search-bar {
    display: flex;
    align-items: center;
    gap: 6px;
    width: 100%;
    padding: 0 8px;
    background: var(--input-background, var(--surface-color));
    border: 1px solid var(--input-border, var(--border-color));
    border-radius: 6px;
    transition:
      border-color 0.15s ease,
      box-shadow 0.15s ease;
  }

  .search-bar-md {
    height: 40px;
  }

  .search-bar-sm {
    height: 32px;
  }

  /* Google-style: soft elevation on focus, not a hard colored ring. */
  .search-bar:focus-within {
    border-color: var(--border-color);
    box-shadow: 0 1px 6px rgba(0, 0, 0, 0.15);
  }

  .search-bar.disabled {
    opacity: 0.6;
    pointer-events: none;
  }

  .search-bar-icon {
    display: flex;
    align-items: center;
    color: var(--text-secondary);
    flex-shrink: 0;
  }

  .search-bar-input {
    flex: 1;
    min-width: 0;
    height: 100%;
    padding: 0;
    border: none;
    background: transparent;
    color: var(--text-color);
    font-size: 0.9rem;
    outline: none;
    box-shadow: none;
  }

  /* The container owns the focus treatment — the inner input must stay flat, so it
     doesn't inherit the global input:focus ring (which looks like a box-in-a-box). */
  .search-bar-input:focus,
  .search-bar-input:focus-visible {
    border: none;
    outline: none;
    box-shadow: none;
  }

  .search-bar-input::placeholder {
    color: var(--text-secondary);
  }

  .search-bar-status {
    display: flex;
    align-items: center;
    flex-shrink: 0;
  }

  .search-bar-loading {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .search-bar-status-text {
    font-style: italic;
    opacity: 0.85;
  }

  .search-bar-counter {
    font-size: 0.78rem;
    font-weight: 500;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .search-bar-counter.no-results {
    color: var(--error-color, #ef4444);
  }

  .search-bar-nav {
    display: flex;
    align-items: center;
    gap: 2px;
    flex-shrink: 0;
  }

  .search-bar-btn,
  .search-bar-clear {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 26px;
    height: 26px;
    padding: 0;
    background: none;
    border: none;
    border-radius: 4px;
    color: var(--text-secondary);
    cursor: pointer;
    transition:
      background 0.15s ease,
      color 0.15s ease;
    flex-shrink: 0;
  }

  .search-bar-btn {
    border: 1px solid var(--border-color);
  }

  .search-bar-btn:hover:not(:disabled),
  .search-bar-clear:hover:not(:disabled) {
    background: var(--button-hover, var(--hover-color));
    color: var(--text-color);
  }

  .search-bar-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .search-bar-btn:focus-visible,
  .search-bar-clear:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }
</style>
