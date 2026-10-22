<script lang="ts">
  import type { SearchOccurrence } from '$stores/search';
  import { t } from '$stores/locale';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';
  import { formatClock } from '$lib/utils/formatting';

  export let occurrence: SearchOccurrence;
  export let fileUuid: string;
</script>

<div class="occurrence">
  <div class="occurrence-header">
    <a
      class="timestamp-link"
      href="/files/{fileUuid}?t={occurrence.start_time}"
      title="{$t('search.jumpTo')} {formatClock(occurrence.start_time)}"
    >
      {formatClock(occurrence.start_time)}
    </a>
    {#if occurrence.speaker}
      <span class="speaker" dir="auto">
        {#if occurrence.speaker_highlighted}
          {@html sanitizeHighlightHtml(occurrence.speaker_highlighted)}
        {:else}
          {occurrence.speaker}
        {/if}
      </span>
    {/if}
  </div>
  {#if occurrence.snippet}
    <div class="snippet" dir="auto" class:context-only={occurrence.match_type === 'title' || occurrence.match_type === 'speaker'} class:semantic-snippet={occurrence.highlight_type === 'semantic'}>
      {@html sanitizeHighlightHtml(occurrence.snippet)}
    </div>
  {/if}
</div>

<style>
  .occurrence {
    min-width: 0;
  }

  /* One muted line above the snippet: timestamp link, then the speaker as plain text.
     The old chip + bordered-timestamp pair made every row two boxes tall before the
     snippet started (docs/design/search_results_ux_audit.md). */
  .occurrence-header {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 0.125rem;
    font-size: var(--font-size-xs);
  }

  .speaker {
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    overflow-wrap: break-word;
    word-break: break-word;
  }

  .timestamp-link {
    font-family: var(--font-mono);
    color: var(--primary-on-surface);
    text-decoration: none;
  }

  .timestamp-link:hover {
    text-decoration: underline;
  }

  .timestamp-link:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
    border-radius: 2px;
  }

  .snippet {
    font-size: var(--font-size-md);
    line-height: var(--line-height-body);
    color: var(--text-color, #374151);
    overflow-wrap: break-word;
    word-break: break-word;
  }

  .snippet :global(mark) {
    background: var(--highlight-bg);
    color: inherit;
    padding: 0.05em 0.15em;
    border-radius: 2px;
    font-weight: var(--font-weight-medium);
  }

  .snippet.context-only {
    opacity: 0.7;
    font-style: italic;
  }

  .snippet :global(mark.semantic) {
    background: rgba(245, 158, 11, 0.2);
    color: inherit;
    padding: 0.05em 0.15em;
    border-radius: 2px;
    font-weight: var(--font-weight-medium);
  }

  :global([data-theme='dark']) .snippet :global(mark.semantic) {
    background: rgba(251, 191, 36, 0.25);
  }

  .snippet.semantic-snippet {
    background: rgba(245, 158, 11, 0.08);
    border-inline-start: 3px solid rgba(245, 158, 11, 0.4);
    padding: 0.25rem 0.5rem;
    border-start-start-radius: 0;
    border-end-start-radius: 0;
    border-start-end-radius: 4px;
    border-end-end-radius: 4px;
  }

  :global([data-theme='dark']) .snippet.semantic-snippet {
    background: rgba(251, 191, 36, 0.1);
    border-inline-start-color: rgba(251, 191, 36, 0.35);
  }
</style>
