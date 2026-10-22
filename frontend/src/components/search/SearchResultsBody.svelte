<script lang="ts">
  /**
   * The results area of `/search` once a query has run: the collapsible Summaries and
   * Transcripts sections, and (wide layout) the knowledge-panel aside beside the results
   * column. The page still owns the fetch, the store, the preview player and pagination.
   *
   * E2E contract (`backend/tests/e2e/test_search.py`): `.results-list > *` counts transcript
   * hits, so the aside and every notice live OUTSIDE `.results-list`; `.summary-results-list`
   * is a sibling of it, never nested inside it.
   */
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

  function handleInspect(event: CustomEvent<string>) {
    hoveredUuid = event.detail;
  }

  function handleToggleDetails(event: CustomEvent<string>) {
    pinnedUuid = pinnedUuid === event.detail ? null : event.detail;
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
      <SearchResultInfoCard hit={infoHit} variant="panel" />
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
          panelId={PANEL_ID}
          selected={pinnedUuid === hit.file_uuid}
          on:preview
          on:togglePlayback
          on:viewTranscript
          on:inspect={handleInspect}
          on:toggleDetails={handleToggleDetails}
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

  .results-layout.with-aside {
    grid-template-columns: minmax(0, 44rem) 19rem;
  }

  .result-info-aside {
    position: sticky;
    top: 0;
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
