<script lang="ts">
  import SanitizedHtml from '$components/ui/SanitizedHtml.svelte';
  import { resolve } from '$app/paths';
  import type { SearchOccurrence } from '$stores/search';
  import { t } from '$stores/locale';
  import { formatClock } from '$lib/utils/formatting';

  export let occurrence: SearchOccurrence;
  export let fileUuid: string;
</script>

<div class="occurrence">
  <div class="occurrence-header">
    <a
      class="timestamp-link"
      href={resolve(`/files/[id]?t=${occurrence.start_time}`, { id: fileUuid })}
      title="{$t('search.jumpTo')} {formatClock(occurrence.start_time)}"
    >
      {formatClock(occurrence.start_time)}
    </a>
    {#if occurrence.speaker}
      <span class="speaker" dir="auto">
        {#if occurrence.speaker_highlighted}
          <SanitizedHtml html={occurrence.speaker_highlighted} />
        {:else}
          {occurrence.speaker}
        {/if}
      </span>
    {/if}
  </div>
  {#if occurrence.snippet}
    <div class="snippet" dir="auto" class:context-only={occurrence.match_type === 'title' || occurrence.match_type === 'speaker'} class:semantic-snippet={occurrence.highlight_type === 'semantic'}>
      <SanitizedHtml html={occurrence.snippet} />
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
    gap: var(--space-2);
    margin-bottom: var(--space-0-5);
    font-size: var(--font-size-xs);
  }

  .speaker {
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    overflow-wrap: anywhere;
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
    outline: 2px solid var(--focus-ring-color);
    outline-offset: 2px;
    border-radius: var(--radius-xs);
  }

  .snippet {
    font-size: var(--font-size-md);
    line-height: var(--line-height-body);
    color: var(--text-color);
    overflow-wrap: anywhere;
  }

  .snippet :global(mark) {
    background: var(--highlight-bg);
    color: inherit;
    padding: 0.05em 0.15em;
    border-radius: var(--radius-xs);
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
    border-radius: var(--radius-xs);
    font-weight: var(--font-weight-medium);
  }

  :global([data-theme='dark']) .snippet :global(mark.semantic) {
    background: rgba(251, 191, 36, 0.25);
  }

  .snippet.semantic-snippet {
    background: rgba(245, 158, 11, 0.08);
    border-inline-start: 3px solid rgba(245, 158, 11, 0.4);
    padding: var(--space-1) var(--space-2);
    border-start-start-radius: 0;
    border-end-start-radius: 0;
    border-start-end-radius: var(--radius-sm);
    border-end-end-radius: var(--radius-sm);
  }

  :global([data-theme='dark']) .snippet.semantic-snippet {
    background: rgba(251, 191, 36, 0.1);
    border-inline-start-color: rgba(251, 191, 36, 0.35);
  }
</style>
