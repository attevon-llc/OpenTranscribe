<!--
  SummarySearch.svelte — find-in-summary input.

  Thin wrapper over the shared SearchBar so summary find looks/behaves like every
  other search surface. Presentational only: SummaryModal owns the matching and
  passes back totalMatches/currentMatchIndex. Public props/events are unchanged so
  the parent needs no updates.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import SearchBar from '$components/ui/SearchBar.svelte';

  export let searchQuery: string = '';
  export let totalMatches: number = 0;
  export let currentMatchIndex: number = 0;
  export let disabled: boolean = false;

  const dispatch = createEventDispatcher<{
    search: { query: string };
    clearSearch: void;
    nextMatch: void;
    previousMatch: void;
    keydown: KeyboardEvent;
  }>();

  $: counterText = searchQuery && totalMatches > 0 ? `${currentMatchIndex + 1}/${totalMatches}` : '';

  function handleKeydown(event: CustomEvent<KeyboardEvent>) {
    // Enter/Shift+Enter are handled by SearchBar (next/previous); forward the rest
    // so the parent modal keeps its other shortcuts (matches prior behavior).
    if (event.detail.key !== 'Enter') dispatch('keydown', event.detail);
  }
</script>

<div class="summary-search">
  <SearchBar
    bind:value={searchQuery}
    {disabled}
    size="sm"
    debounceMs={0}
    showNav
    navDisabled={totalMatches === 0}
    {counterText}
    placeholder={$t('search.placeholder')}
    ariaLabel={$t('search.placeholder')}
    clearLabel={$t('search.clearSearch')}
    nextLabel={$t('search.nextMatch')}
    previousLabel={$t('search.previousMatch')}
    on:search={({ detail }) => dispatch('search', { query: detail.value })}
    on:clear={() => dispatch('clearSearch')}
    on:next={() => dispatch('nextMatch')}
    on:previous={() => dispatch('previousMatch')}
    on:keydown={handleKeydown}
  />

  {#if searchQuery && totalMatches === 0}
    <div class="no-results">
      <span>{$t('search.noResults', { query: searchQuery })}</span>
    </div>
  {/if}
</div>

<style>
  .summary-search {
    padding: 0.75rem 1.5rem;
    border-bottom: 1px solid var(--border-color);
    background-color: var(--bg-secondary, var(--surface-color));
  }

  .no-results {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    padding: 1rem;
    color: var(--text-muted, var(--text-secondary));
    font-size: 0.9rem;
  }
</style>
