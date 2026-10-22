<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { SummaryHit, SummarySectionMatch } from '$stores/search';
  import { t } from '$stores/locale';

  export let hit: SummaryHit;

  const dispatch = createEventDispatcher<{
    openMatch: { fileUuid: string; title: string; keyPath: string | null };
  }>();

  // Purely-presentational transform of an already-downloaded key_path string into a
  // readable label, e.g. "major_topics[0].key_points[2]" -> "Major Topics #1 › Key Points #3".
  // No backend round trip needed for this — it's cosmetic formatting of data the page
  // already has, the same exception `SummaryDisplay.svelte`'s formatFieldName relies on.
  function formatKeyPath(keyPath: string): string {
    return keyPath
      .split('.')
      .map((segment) => {
        const m = segment.match(/^([a-zA-Z0-9_]+)(?:\[(\d+)\])?$/);
        if (!m) return segment;
        const [, key, idx] = m;
        const label = key
          .replace(/_/g, ' ')
          .replace(/\b\w/g, (c) => c.toUpperCase());
        return idx !== undefined ? `${label} #${Number(idx) + 1}` : label;
      })
      .join(' › ');
  }

  // Same progressive disclosure as a transcript hit: a summary can match 8+ leaves, which
  // pushed every transcript result below the fold (docs/design/search_results_ux_audit.md).
  const MAX_VISIBLE = 2;
  let expanded = false;

  $: visibleMatches = expanded ? hit.matches : hit.matches.slice(0, MAX_VISIBLE);
  $: hiddenCount = hit.matches.length - MAX_VISIBLE;
  $: matchListId = `summary-match-list-${hit.file_uuid}`;

  function openMatch(match: SummarySectionMatch) {
    dispatch('openMatch', {
      fileUuid: hit.file_uuid,
      title: hit.title,
      keyPath: match.key_path,
    });
  }

  function openSummary() {
    dispatch('openMatch', {
      fileUuid: hit.file_uuid,
      title: hit.title,
      keyPath: null,
    });
  }
</script>

<article class="summary-result-card">
  <div class="summary-result-header">
    <a href="/files/{hit.file_uuid}" class="summary-result-title">
      <svg class="media-type-icon" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
        <polyline points="14,2 14,8 20,8"></polyline>
        <line x1="16" y1="13" x2="8" y2="13"></line>
        <line x1="16" y1="17" x2="8" y2="17"></line>
      </svg>
      <span dir="auto">{hit.title}</span>
    </a>
    {#if hit.matches.length > 0}
      <p class="summary-match-count">
        {hit.matches.length === 1
          ? $t('search.summaryMatchFound', { count: hit.matches.length })
          : $t('search.summaryMatchesFound', { count: hit.matches.length })}
      </p>
    {/if}
  </div>

  {#if hit.matches.length > 0}
    <div class="summary-match-list" id={matchListId}>
      {#each visibleMatches as match (match.key_path)}
        <button
          type="button"
          class="summary-match-row"
          on:click={() => openMatch(match)}
          aria-label="{$t('search.jumpToSummarySection')}: {formatKeyPath(match.key_path)}"
        >
          <span class="summary-match-path">{formatKeyPath(match.key_path)}</span>
          <span class="summary-match-snippet" dir="auto">{match.snippet}</span>
        </button>
      {/each}
    </div>
  {/if}

  <div class="summary-actions">
    {#if hiddenCount > 0}
      <button
        type="button"
        class="text-btn show-more"
        aria-expanded={expanded}
        aria-controls={matchListId}
        on:click={() => (expanded = !expanded)}
      >
        {expanded ? $t('search.showLess') : $t('search.showMoreSections', { count: hiddenCount })}
      </button>
    {/if}
    <button type="button" class="text-btn view-summary-btn" on:click={openSummary}>
      {$t('search.viewSummary')}
    </button>
  </div>
</article>

<style>
  .summary-result-card {
    padding: 0.875rem 1rem;
    margin-bottom: 0.5rem;
    border-radius: 10px;
    transition: background-color 0.15s;
  }

  .summary-result-card:hover,
  .summary-result-card:focus-within {
    background: var(--hover-color);
  }

  .summary-result-title {
    display: block;
    min-width: 0;
    font-size: var(--font-size-lg);
    font-weight: var(--font-weight-medium);
    color: var(--primary-on-surface);
    text-decoration: none;
    line-height: var(--line-height-tight);
    overflow-wrap: break-word;
  }

  .summary-result-title:hover {
    text-decoration: underline;
  }

  .media-type-icon {
    vertical-align: -2px;
    margin-inline-end: 0.375rem;
    opacity: 0.7;
  }

  .summary-match-count {
    margin: 0.25rem 0 0.5rem;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .summary-match-list {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .summary-match-row {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.125rem;
    width: 100%;
    padding: 0.375rem 0.625rem;
    background: none;
    border: none;
    border-inline-start: 2px solid var(--border-color);
    border-radius: 0;
    box-shadow: none;
    text-align: start;
    cursor: pointer;
    transition:
      border-color 0.15s,
      background-color 0.15s;
  }

  .summary-match-row:hover {
    border-inline-start-color: var(--primary-color);
    background: rgba(var(--primary-color-rgb), 0.06);
    box-shadow: none;
    transform: none;
  }

  .summary-match-row:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .summary-match-path {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  /* Body text is regular; without this it inherits the global button's 500. */
  .summary-match-snippet {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-regular);
    line-height: var(--line-height-body);
    color: var(--text-color);
    overflow-wrap: break-word;
    word-break: break-word;
  }

  .summary-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 1rem;
    margin-top: 0.5rem;
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

  @media (max-width: 768px) {
    .summary-result-card {
      padding: 0.75rem 0.5rem;
    }

    .summary-result-title {
      font-size: var(--font-size-body);
    }
  }
</style>
