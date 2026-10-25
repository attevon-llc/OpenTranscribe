<script context="module" lang="ts">
  /** Cards whose match list starts open; past this a page reads as a compact list. */
  export const DEFAULT_OPEN_RESULTS = 5;
</script>

<script lang="ts">
  /**
   * The results area of `/search` once a query has run: the collapsible Summaries and
   * Transcripts sections, and (wide layout) the knowledge-panel aside beside the results
   * column. The page still owns the fetch, the store, the preview player and pagination.
   *
   * E2E contract (`backend/tests/e2e/test_search.py`): `.results-list > *` counts transcript
   * hits, so the aside, the collapse-all control and every notice live OUTSIDE
   * `.results-list`; `.summary-results-list` is a sibling of it, never nested inside it.
   *
   * Match lists: the first `DEFAULT_OPEN_RESULTS` cards start open and the rest collapsed;
   * a user's choice per file arrives in `matchListOpen` (the page keeps it in `searchStore`
   * for the session) and changes leave as `setMatchListsOpen`.
   */
  import { createEventDispatcher } from 'svelte';
  import type { SearchHit, SummaryHit } from '$stores/search';
  import { t } from '$stores/locale';
  import ExpandableSection from '$components/ui/ExpandableSection.svelte';
  import RetrievalQualityNotice from '$components/RetrievalQualityNotice.svelte';
  import SearchResultCard from './SearchResultCard.svelte';
  import SummaryResultCard from './SummaryResultCard.svelte';
  import SearchResultInfoCard from './SearchResultInfoCard.svelte';

  export let query: string;
  export let searchMode: string;
  export let showSummarySection: boolean;
  export let showTranscriptSection: boolean;
  export let results: SearchHit[];
  export let summaryResults: SummaryHit[];
  export let summaryTotal: number;
  export let summaryUnavailable: string | null = null;
  export let activePreview: { fileUuid: string; startTime: number; playing: boolean } | null = null;
  /** Wide enough for the info aside; otherwise "Details" expands inline in each card. */
  export let panelMode = false;
  /** The user's per-file open/closed choice for each card's match list. */
  export let matchListOpen: Record<string, boolean> = {};

  const dispatch = createEventDispatcher<{
    setMatchListsOpen: { fileUuids: string[]; open: boolean };
  }>();

  const PANEL_ID = 'search-result-info-panel';

  let summaryOpen = true;
  let transcriptOpen = true;
  let hoveredUuid: string | null = null;
  let pinnedUuid: string | null = null;

  $: allSemanticOnly = results.length > 0 && results.every((r) => r.semantic_only);
  // Pinned beats hovered beats the top result; a pin or hover that is no longer in the
  // result set (new query, new page) falls through instead of showing a stale file.
  $: infoHit =
    results.find((r) => r.file_uuid === pinnedUuid) ??
    results.find((r) => r.file_uuid === hoveredUuid) ??
    results[0] ??
    null;
  $: showAside = panelMode && showTranscriptSection && transcriptOpen && infoHit !== null;
  $: pinnedHit = results.find((r) => r.file_uuid === pinnedUuid) ?? null;
  $: openByUuid = Object.fromEntries(
    results.map((r, index) => [r.file_uuid, matchListOpen[r.file_uuid] ?? index < DEFAULT_OPEN_RESULTS])
  ) as Record<string, boolean>;
  $: anyOpen = results.some((r) => openByUuid[r.file_uuid]);

  function handleInspect(event: CustomEvent<string>) {
    hoveredUuid = event.detail;
  }

  function handleToggleDetails(event: CustomEvent<string>) {
    pinnedUuid = pinnedUuid === event.detail ? null : event.detail;
  }

  function handleToggleMatches(event: CustomEvent<string>) {
    const uuid = event.detail;
    dispatch('setMatchListsOpen', { fileUuids: [uuid], open: !openByUuid[uuid] });
  }

  function setAllMatchLists() {
    dispatch('setMatchListsOpen', { fileUuids: results.map((r) => r.file_uuid), open: !anyOpen });
  }
</script>

<div class="results-layout" class:with-aside={showAside}>
  <div class="results-main">
    {#if showSummarySection}
      <div class="summary-results-section">
        <ExpandableSection title={$t('search.summaryResultsHeading')} headingLevel={2} bind:expanded={summaryOpen}>
          {#if summaryUnavailable}
            <div class="summary-unavailable-notice">
              <p class="state-title">{$t('search.summaryUnavailableTitle')}</p>
              <p class="state-hint">{$t('search.summaryUnavailableBody')}</p>
            </div>
          {:else if summaryResults.length === 0}
            <p class="section-info">{$t('search.noSummaryResults', { query })}</p>
          {:else}
            <p class="section-info">{$t('search.summariesFound', { count: summaryTotal })}</p>
            <div class="summary-results-list">
              {#each summaryResults as hit (hit.file_uuid)}
                <SummaryResultCard {hit} on:openMatch />
              {/each}
            </div>
          {/if}
        </ExpandableSection>
      </div>
    {/if}

    {#if showTranscriptSection}
      <div class="transcript-results-section">
        {#if showSummarySection}
          <ExpandableSection title={$t('search.transcriptResultsHeading')} headingLevel={2} bind:expanded={transcriptOpen}>
            {@render transcriptBlock()}
          </ExpandableSection>
        {:else}
          {@render transcriptBlock()}
        {/if}
      </div>
    {/if}
  </div>

  {#if showAside && infoHit}
    <aside class="result-info-aside" id={PANEL_ID} aria-label={$t('search.resultDetails')}>
      <!-- Re-keyed on the pin so a newly pinned file gets a short highlight: in the wide
           layout the panel often already shows the hit being pinned. -->
      {#key pinnedHit?.file_uuid}
        <div class="aside-inner" class:just-pinned={pinnedHit !== null}>
          <SearchResultInfoCard
            hit={infoHit}
            {query}
            variant="panel"
            pinned={pinnedHit !== null}
            on:unpin={() => (pinnedUuid = null)}
            on:filterTag
            on:filterCollection
          />
        </div>
      {/key}
    </aside>
  {/if}
</div>

{#snippet transcriptBlock()}
  <!-- Smart mode only: Exact mode is literal BM25 and untouched by the fusion ranking
       #461 measured. -->
  {#if searchMode === 'hybrid'}
    <div class="quality-notice-slot">
      <RetrievalQualityNotice surface="search" />
    </div>
  {/if}

  {#if results.length === 0}
    <div class="state-container">
      <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" stroke-linejoin="round" class="empty-icon" aria-hidden="true">
        <circle cx="11" cy="11" r="8"></circle>
        <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
      </svg>
      <p class="state-title">{$t('searchPage.noResults', { query })}</p>
      <p class="state-hint">{$t('search.noResultsHint')}</p>
    </div>
  {:else}
    {#if results.length > 1}
      <div class="results-toolbar">
        <button type="button" class="text-btn collapse-all" on:click={setAllMatchLists}>
          {anyOpen ? $t('search.collapseAll') : $t('search.expandAll')}
        </button>
      </div>
    {/if}
    <div class="results-list">
      {#if allSemanticOnly}
        <div class="no-keyword-notice">
          <p>{$t('search.noKeywordMatches')}</p>
        </div>
      {/if}
      {#each results as hit (hit.file_uuid)}
        <SearchResultCard
          {hit}
          {activePreview}
          {panelMode}
          {query}
          panelId={PANEL_ID}
          selected={pinnedUuid === hit.file_uuid}
          matchesOpen={openByUuid[hit.file_uuid] ?? true}
          on:preview
          on:togglePlayback
          on:viewTranscript
          on:inspect={handleInspect}
          on:toggleDetails={handleToggleDetails}
          on:toggleMatches={handleToggleMatches}
          on:filterTag
          on:filterCollection
        />
      {/each}
    </div>
  {/if}
{/snippet}

<style>
  /* Results column capped at a readable measure and start-aligned; the info aside sits
     beside it only when the page says there is room (panelMode). */
  .results-layout {
    display: grid;
    grid-template-columns: minmax(0, 44rem);
    gap: 2rem;
    align-items: start;
  }

  /* The reading column takes its full 44rem first; the panel gets what is left (at least
     19rem, at most 24rem on a wide monitor). */
  .results-layout.with-aside {
    grid-template-columns: minmax(0, 44rem) minmax(19rem, 1fr);
  }

  .result-info-aside {
    position: sticky;
    top: 0;
    max-width: 24rem;
  }

  .aside-inner.just-pinned {
    border-radius: 12px;
    animation: pinned-pulse 0.6s ease-out;
  }

  @keyframes pinned-pulse {
    from {
      box-shadow: 0 0 0 6px rgba(var(--primary-color-rgb), 0.35);
    }
    to {
      box-shadow: 0 0 0 0 rgba(var(--primary-color-rgb), 0);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .aside-inner.just-pinned {
      animation: none;
    }
  }

  .results-toolbar {
    display: flex;
    justify-content: flex-end;
    margin: -0.25rem 0 0.25rem;
  }

  .text-btn {
    padding: 0.125rem 0;
    background: none;
    border: none;
    /* form-elements.css styles every bare <button> with a drop shadow and a hover
       scale + fill; a text link-button wants none of them. */
    box-shadow: none;
    color: var(--primary-on-surface);
    font-size: var(--font-size-sm);
    cursor: pointer;
  }

  .text-btn:hover {
    text-decoration: underline;
    background: none;
    box-shadow: none;
    transform: none;
  }

  .text-btn:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
    border-radius: 2px;
  }

  .summary-results-section {
    margin-bottom: 1.5rem;
  }

  .section-info {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    margin: 0.25rem 0 0.5rem;
  }

  .summary-unavailable-notice {
    padding: 1rem;
    border-radius: 8px;
    background: var(--warning-bg, #fef3c7);
    border: 1px solid var(--warning-border, #f59e0b);
    margin-bottom: 0.75rem;
  }

  .summary-results-list,
  .results-list {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .quality-notice-slot {
    margin-bottom: 12px;
  }

  .state-container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 4rem 2rem;
    text-align: center;
  }

  .state-container svg {
    color: var(--text-secondary);
    margin-bottom: 1rem;
  }

  .empty-icon {
    opacity: 0.35;
  }

  .state-title {
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    margin: 0 0 0.375rem;
  }

  .state-hint {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    margin: 0;
  }

  .no-keyword-notice {
    padding: 12px 16px;
    margin-bottom: 12px;
    background: var(--color-warning-bg, #fef3c7);
    color: var(--color-warning-text, #92400e);
    border-radius: 8px;
    font-size: var(--font-size-sm);
  }

  :global([data-theme='dark']) .no-keyword-notice {
    background: rgba(245, 158, 11, 0.1);
    color: #fbbf24;
  }

  :global([data-theme='dark']) .summary-unavailable-notice {
    background: rgba(245, 158, 11, 0.1);
    border-color: rgba(245, 158, 11, 0.35);
  }
</style>
