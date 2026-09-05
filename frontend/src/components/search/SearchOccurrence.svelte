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
    {#if occurrence.speaker}
      <span class="speaker">
        {#if occurrence.speaker_highlighted}
          {@html sanitizeHighlightHtml(occurrence.speaker_highlighted)}
        {:else}
          {occurrence.speaker}
        {/if}
      </span>
    {/if}
    <a
      class="timestamp-link"
      href="/files/{fileUuid}?t={occurrence.start_time}"
      title="{$t('search.jumpTo')} {formatClock(occurrence.start_time)}"
    >
      {formatClock(occurrence.start_time)}
    </a>
  </div>
  {#if occurrence.snippet}
    <div class="snippet" class:context-only={occurrence.match_type === 'title' || occurrence.match_type === 'speaker'} class:semantic-snippet={occurrence.highlight_type === 'semantic'}>
      {@html sanitizeHighlightHtml(occurrence.snippet)}
    </div>
  {/if}
</div>

<style>
  .occurrence {
    padding: 0.5rem 0;
    border-top: 1px solid var(--border-color, #e5e7eb);
  }

  .occurrence:first-child {
    border-top: none;
    padding-top: 0;
  }

  .occurrence-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.25rem;
  }

  .speaker {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--primary-color, #4f46e5);
    background: rgba(79, 70, 229, 0.12);
    padding: 0.125rem 0.375rem;
    border-radius: 4px;
    overflow-wrap: break-word;
    word-break: break-word;
  }

  .speaker :global(mark) {
    background: rgba(250, 204, 21, 0.5);
    color: inherit;
    padding: 0 0.1em;
    border-radius: 2px;
  }

  .timestamp-link {
    font-size: 0.75rem;
    font-family: monospace;
    color: var(--primary-color, #4f46e5);
    text-decoration: none;
    padding: 0.125rem 0.375rem;
    border-radius: 4px;
    background: var(--surface-color, #f9fafb);
    border: 1px solid var(--border-color, #e5e7eb);
    transition: all 0.15s;
  }

  .timestamp-link:hover {
    background: var(--primary-color, #4f46e5);
    color: white;
    border-color: var(--primary-color, #4f46e5);
  }

  .snippet {
    font-size: 0.8125rem;
    line-height: 1.5;
    color: var(--text-color, #374151);
    overflow-wrap: break-word;
    word-break: break-word;
  }

  .snippet :global(mark) {
    background: rgba(250, 204, 21, 0.4);
    color: inherit;
    padding: 0.05em 0.15em;
    border-radius: 2px;
    font-weight: 500;
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
    font-weight: 500;
  }

  :global([data-theme='dark']) .snippet :global(mark.semantic) {
    background: rgba(251, 191, 36, 0.25);
  }

  .snippet.semantic-snippet {
    background: rgba(245, 158, 11, 0.08);
    border-left: 3px solid rgba(245, 158, 11, 0.4);
    padding: 0.25rem 0.5rem;
    border-radius: 0 4px 4px 0;
  }

  :global([data-theme='dark']) .snippet.semantic-snippet {
    background: rgba(251, 191, 36, 0.1);
    border-left-color: rgba(251, 191, 36, 0.35);
  }

</style>
