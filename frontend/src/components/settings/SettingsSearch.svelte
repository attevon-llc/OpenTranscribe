<!--
  SettingsSearch.svelte — macOS-style search over settings.

  Rendered as the fixed header of the settings sidebar (and above the mobile section select).
  Receives a prebuilt fuzzy index (SettingsModal owns index construction so desktop
  + mobile instances share the corpus). When the query is non-empty it renders a flat,
  ranked, highlighted results list and dispatches `navigate` on selection; the parent
  hides the normal grouped nav while `active`.

  In the `sidebar` layout this component is a column: the field never scrolls, and the results
  list is the only scroll region, so no scrollbar can ever sit on top of the input.
-->
<script lang="ts">
  import { createEventDispatcher, tick } from 'svelte';
  import { t } from '$stores/locale';
  import type { FuzzyIndex } from '$lib/search/fuzzyMatcher';
  import type { SettingsSearchItem } from '$lib/search/settingsSearchIndex';
  import { highlightText } from '$lib/utils/searchHighlight';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';
  import SearchBar from '$components/ui/SearchBar.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';

  export let index: FuzzyIndex<SettingsSearchItem> | null = null;
  /** Bound out: true while a query is active (parent hides the grouped nav). */
  export let active = false;
  /** Unique suffix so desktop + mobile instances don't share DOM ids. */
  export let idPrefix = 'settings-search';
  /** `sidebar`: fixed header that hands the column to the results while searching. `inline`: flows in place. */
  export let layout: 'sidebar' | 'inline' = 'inline';
  /** The list beneath this header has scrolled, so the header draws its divider. */
  export let raised = false;

  const MAX_RESULTS = 8;

  const dispatch = createEventDispatcher<{
    navigate: { sectionId: SettingsSearchItem['sectionId']; anchorText: string };
  }>();

  let query = '';
  let activeIndex = 0;
  let resultsScrolled = false;

  $: trimmed = query.trim();
  $: active = trimmed.length > 0;
  $: results = trimmed && index ? index.search(trimmed, MAX_RESULTS) : [];
  // Reset the active row whenever the query changes so Enter never hits a stale row.
  $: resetActiveRow(query);
  function resetActiveRow(_query: string) {
    activeIndex = 0;
  }
  $: listId = `${idPrefix}-results`;
  $: countLabel = results.length
    ? $t('settingsSearch.resultsCount', { count: results.length })
    : '';
  // Said aloud, not shown: the field itself stays free of a counter, like Apple's.
  $: announcement = !active ? '' : results.length ? countLabel : $t('settingsSearch.noResultsTitle');
  $: if (!active) resultsScrolled = false;
  $: void keepActiveRowVisible(activeIndex);

  async function keepActiveRowVisible(_row: number) {
    await tick();
    document
      .getElementById(`${idPrefix}-result-${activeIndex}`)
      ?.scrollIntoView?.({ block: 'nearest' });
  }

  function selectResult(i: number) {
    const result = results[i];
    if (!result) return;
    dispatch('navigate', {
      sectionId: result.item.sectionId,
      anchorText: result.item.anchorText,
    });
    query = '';
  }

  function handleKeydown(event: CustomEvent<KeyboardEvent>) {
    const ev = event.detail;
    if (ev.key === 'Escape') {
      if (trimmed) {
        // Clear the query first; stop the modal's document Escape handler from closing.
        ev.preventDefault();
        ev.stopPropagation();
        query = '';
      }
      return;
    }
    if (!results.length) return;
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      activeIndex = (activeIndex + 1) % results.length;
    } else if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      activeIndex = (activeIndex - 1 + results.length) % results.length;
    } else if (ev.key === 'Enter') {
      ev.preventDefault();
      selectResult(activeIndex);
    }
  }
</script>

<div
  class="settings-search"
  class:sidebar={layout === 'sidebar'}
  class:active
  class:raised={raised || resultsScrolled}
  role="search"
  aria-label={$t('settingsSearch.ariaLabel')}
>
  <div class="search-field">
    <SearchBar
      bind:value={query}
      size="sm"
      variant="pill"
      type="search"
      debounceMs={0}
      role="combobox"
      inputId={`${idPrefix}-input`}
      ariaControls={listId}
      ariaExpanded={active}
      ariaActivedescendant={active && results.length ? `${idPrefix}-result-${activeIndex}` : undefined}
      placeholder={$t('settingsSearch.placeholder')}
      ariaLabel={$t('settingsSearch.ariaLabel')}
      clearLabel={$t('search.clearSearch')}
      on:keydown={handleKeydown}
    />
  </div>

  <div class="sr-only" role="status" aria-live="polite">{announcement}</div>

  {#if active}
    <div
      class="settings-search-results"
      on:scroll={(event) => (resultsScrolled = event.currentTarget.scrollTop > 0)}
    >
      {#if results.length}
        <ul id={listId} class="results-list" role="listbox" aria-label={$t('settingsSearch.ariaLabel')}>
          {#each results as result, i (result.item.sectionId + '|' + result.item.label + '|' + i)}
            <!-- Combobox/listbox pattern: keyboard interaction (Arrow keys + Enter) is
                 handled on the search input via aria-activedescendant, so the options
                 themselves are pointer-only by design. -->
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <li
              id={`${idPrefix}-result-${i}`}
              class="result-item"
              class:active={i === activeIndex}
              role="option"
              aria-selected={i === activeIndex}
              on:mouseenter={() => (activeIndex = i)}
              on:click={() => selectResult(i)}
            >
              <span class="result-label">
                <!-- eslint-disable-next-line svelte/no-at-html-tags -->
                {@html sanitizeHighlightHtml(highlightText(result.item.label, trimmed))}
              </span>
              <span class="result-section">{result.item.sectionLabel}</span>
            </li>
          {/each}
        </ul>
      {:else}
        <EmptyState
          title={$t('settingsSearch.noResultsTitle')}
          description={$t('settingsSearch.noResults', { query: trimmed })}
          padding="24px 16px"
        />
      {/if}
    </div>
  {/if}
</div>

<style>
  .settings-search {
    position: relative;
    /* Above the list it heads, so the raised shadow is not painted over by the list. */
    z-index: 1;
  }

  .search-field {
    padding: 0 0 12px;
    border-bottom: 1px solid transparent;
    transition:
      border-color 0.15s ease,
      box-shadow 0.15s ease;
  }

  .sidebar {
    display: flex;
    flex-direction: column;
    flex: 0 0 auto;
    min-height: 0;
  }

  .sidebar.active {
    flex: 1 1 auto;
  }

  /* About 0.75rem in from the sidebar edges, like the Settings apps this is modelled on. */
  .sidebar .search-field {
    padding: 12px 12px 10px;
  }

  .raised .search-field {
    border-bottom-color: var(--border-color);
    box-shadow: 0 6px 8px -6px color-mix(in srgb, var(--text-color) 35%, transparent);
  }

  .settings-search-results {
    margin-top: 8px;
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
    max-height: min(60vh, 24rem);
  }

  .sidebar .settings-search-results {
    flex: 1 1 auto;
    min-height: 0;
    max-height: none;
    margin-top: 0;
    padding: 8px;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  @media (prefers-reduced-motion: reduce) {
    .search-field {
      transition: none;
    }
  }

  .results-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .result-item {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 8px 10px;
    border-radius: 6px;
    cursor: pointer;
    border: 1px solid transparent;
  }

  .result-item.active {
    background: var(--hover-color, var(--surface-color));
    border-color: var(--border-color);
  }

  .result-label {
    font-size: 0.875rem;
    color: var(--text-color);
    line-height: 1.3;
  }

  .result-section {
    font-size: 0.72rem;
    color: var(--text-secondary);
  }
</style>
