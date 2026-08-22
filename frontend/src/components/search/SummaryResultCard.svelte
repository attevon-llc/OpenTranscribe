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
      {hit.title}
    </a>
    {#if hit.matches.length > 0}
      <span class="summary-match-count">
        {hit.matches.length === 1
          ? $t('search.summaryMatchFound', { count: hit.matches.length })
          : $t('search.summaryMatchesFound', { count: hit.matches.length })}
      </span>
    {/if}
    <button type="button" class="view-summary-btn" on:click={openSummary}>
      {$t('search.viewSummary')}
    </button>
  </div>

  {#if hit.matches.length > 0}
    <div class="summary-match-list">
      {#each hit.matches as match (match.key_path)}
        <button
          type="button"
          class="summary-match-row"
          on:click={() => openMatch(match)}
          aria-label="{$t('search.jumpToSummarySection')}: {formatKeyPath(match.key_path)}"
        >
          <span class="summary-match-path">{formatKeyPath(match.key_path)}</span>
          <span class="summary-match-snippet">{match.snippet}</span>
        </button>
      {/each}
    </div>
  {/if}
</article>

<style>
  .summary-result-card {
    background: var(--surface-color, #fff);
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 10px;
    padding: 1rem 1.25rem;
    margin-bottom: 0.75rem;
    transition: border-color 0.15s, box-shadow 0.15s;
  }

  .summary-result-card:hover {
    border-color: var(--primary-color, #4f46e5);
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.04);
  }

  .summary-result-header {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.75rem;
    margin-bottom: 0.5rem;
  }

  .summary-result-title {
    font-size: 1.0625rem;
    font-weight: 600;
    color: var(--primary-color, #4f46e5);
    text-decoration: none;
    line-height: 1.3;
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    flex: 1;
    min-width: 0;
  }

  .media-type-icon {
    flex-shrink: 0;
    opacity: 0.7;
  }

  .summary-result-title:hover {
    text-decoration: underline;
  }

  .summary-match-count {
    font-size: 0.75rem;
    color: var(--text-secondary, #6b7280);
    white-space: nowrap;
  }

  .view-summary-btn {
    flex-shrink: 0;
    padding: 0.375rem 0.75rem;
    background: none;
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 6px;
    color: var(--primary-color, #4f46e5);
    font-size: 0.8125rem;
    cursor: pointer;
    transition: all 0.15s;
  }

  .view-summary-btn:hover {
    background: rgba(79, 70, 229, 0.08);
    border-color: var(--primary-color, #4f46e5);
  }

  :global(.dark) .view-summary-btn:hover {
    background: rgba(129, 140, 248, 0.12);
  }

  .summary-match-list {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .summary-match-row {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.125rem;
    width: 100%;
    padding: 0.5rem 0.625rem;
    background: var(--hover-color, #f8fafc);
    border: 1px solid transparent;
    border-radius: 6px;
    text-align: left;
    cursor: pointer;
    transition: all 0.15s;
  }

  .summary-match-row:hover,
  .summary-match-row:focus-visible {
    border-color: var(--primary-color, #4f46e5);
    background: rgba(79, 70, 229, 0.06);
  }

  :global(.dark) .summary-match-row {
    background: rgba(255, 255, 255, 0.04);
  }

  :global(.dark) .summary-match-row:hover,
  :global(.dark) .summary-match-row:focus-visible {
    background: rgba(129, 140, 248, 0.1);
  }

  .summary-match-path {
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--text-secondary, #9ca3af);
  }

  .summary-match-snippet {
    font-size: 0.8125rem;
    line-height: 1.5;
    color: var(--text-color, #374151);
    overflow-wrap: break-word;
    word-break: break-word;
  }

  @media (max-width: 768px) {
    .summary-result-card {
      padding: 0.75rem;
    }

    .summary-result-header {
      flex-direction: column;
      align-items: flex-start;
    }

    .summary-result-title {
      font-size: 0.9375rem;
      word-break: break-word;
      overflow-wrap: break-word;
    }

    .view-summary-btn {
      align-self: flex-start;
      font-size: 0.75rem;
      padding: 0.25rem 0.5rem;
    }
  }
</style>
