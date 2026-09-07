<!--
  ChatSources.svelte — the citation cards under an assistant answer.

  This is the feature's trust surface: it turns "the AI said so" into "here is
  the moment in the recording where it was said". Every href is built from the
  STRUCTURED sources payload via citationHref(), never from anything the model
  wrote — which is why the markdown renderer blocks relative URLs outright.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import { citationHref, formatClock } from '$lib/utils/chatMarkdown';
  import type { ChatSource } from '$lib/types/chat';

  export let sources: ChatSource[] = [];
  /** Collapsed by default under long answers; expanded while streaming context. */
  export let expanded = false;

  $: count = sources.length;

  /**
   * A citation with no `kind` predates #403 Stage 4 and is a transcript chunk.
   * Defaulting the OTHER way would render every historic citation as a summary.
   */
  function isDigest(source: ChatSource): boolean {
    return source.kind === 'digest';
  }

  /**
   * A `summary` citation (#464) is LLM-generated prose ABOUT the recording,
   * not extracted from it — a labelled interpretation, never a quote, and
   * (unlike a digest) not anchored to a moment in the recording at all. It
   * gets its own badge and its own clock-less rendering rather than reusing
   * the digest branch, so the two provenances stay visually distinguishable.
   */
  function isSummary(source: ChatSource): boolean {
    return source.kind === 'summary';
  }

  /**
   * A `recurrence` citation (W2.5) names a GROUP of items recurring across
   * multiple recordings — never one person's words, never a single moment,
   * and not even a single file the way `summary` is. It gets its own badge
   * and a "N recordings" count instead of a clock or a speaker name.
   */
  function isRecurrence(source: ChatSource): boolean {
    return source.kind === 'recurrence';
  }

  function recurrenceFileCount(source: ChatSource): number {
    return source.file_uuids && source.file_uuids.length > 0 ? source.file_uuids.length : 1;
  }

  function toggle(): void {
    expanded = !expanded;
  }
</script>

{#if count > 0}
  <div class="chat-sources">
    <button
      type="button"
      class="sources-toggle"
      on:click={toggle}
      aria-expanded={expanded}
      data-testid="chat-sources-toggle"
    >
      <svg
        class="chevron"
        class:open={expanded}
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        aria-hidden="true"
      >
        <polyline points="9 18 15 12 9 6" />
      </svg>
      <span>{$t('chat.sources.count', { count })}</span>
    </button>

    {#if expanded}
      <ul class="sources-list" data-testid="chat-sources-list">
        {#each sources as source (source.id)}
          <li>
            <a
              class="source-card"
              href={citationHref(source)}
              data-testid="chat-source-link"
              title={isSummary(source)
                ? $t('chat.sources.openSummary')
                : isRecurrence(source)
                  ? $t('chat.sources.openRecurrence')
                  : $t('chat.sources.openAt', { time: formatClock(source.start_time) })}
            >
              <span class="source-index">[{source.id}]</span>
              <span class="source-body">
                <span class="source-title">{source.title || $t('chat.sources.untitled')}</span>
                <span class="source-meta">
                  {#if isRecurrence(source)}
                    <span class="source-kind" data-testid="chat-source-recurrence">
                      {$t('chat.sources.recurrenceBadge')}
                    </span>
                  {:else if isSummary(source)}
                    <span class="source-kind" data-testid="chat-source-summary">
                      {$t('chat.sources.aiSummaryBadge')}
                    </span>
                  {:else if isDigest(source)}
                    <span class="source-kind" data-testid="chat-source-digest">
                      {$t('chat.sources.summaryBadge')}
                    </span>
                  {:else if source.speaker}
                    <span class="source-speaker">{source.speaker}</span>
                  {/if}
                  {#if isRecurrence(source)}
                    <span class="source-recurrence-count" data-testid="chat-source-recurrence-count">
                      {$t('chat.sources.recurrenceCount', { count: recurrenceFileCount(source) })}
                    </span>
                  {:else if !isSummary(source)}
                    <span class="source-time">{formatClock(source.start_time)}</span>
                  {/if}
                </span>
                {#if source.snippet}
                  <span class="source-snippet">{source.snippet}</span>
                {/if}
              </span>
            </a>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
{/if}

<style>
  .chat-sources {
    margin-top: 0.75rem;
  }

  .sources-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    background: none;
    border: none;
    padding: 0.25rem 0.35rem;
    margin-left: -0.35rem;
    border-radius: 6px;
    color: var(--text-secondary);
    font-size: 0.8rem;
    font-weight: 500;
    cursor: pointer;
  }

  .sources-toggle:hover {
    background-color: var(--button-hover);
    color: var(--text-color);
  }

  .chevron {
    transition: transform 0.15s ease;
  }

  .chevron.open {
    transform: rotate(90deg);
  }

  .sources-list {
    list-style: none;
    margin: 0.5rem 0 0;
    padding: 0;
    display: grid;
    gap: 0.5rem;
  }

  .source-card {
    display: flex;
    gap: 0.6rem;
    padding: 0.6rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
    text-decoration: none;
    color: inherit;
    transition:
      border-color 0.15s ease,
      background-color 0.15s ease;
  }

  .source-card:hover {
    border-color: rgba(var(--primary-color-rgb), 0.5);
    background-color: rgba(var(--primary-color-rgb), 0.05);
  }

  .source-index {
    flex-shrink: 0;
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--primary-on-surface);
    font-variant-numeric: tabular-nums;
  }

  .source-body {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    min-width: 0;
  }

  .source-title {
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text-color);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .source-meta {
    display: flex;
    gap: 0.5rem;
    font-size: 0.75rem;
    color: var(--text-secondary);
  }

  .source-speaker {
    font-weight: 500;
  }

  /* A digest is derived text, not a quote. The badge is what stops a summary
     from reading as something a participant said — light/dark parity via the
     shared tokens, never a hardcoded colour. */
  .source-kind {
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    font-size: 0.68rem;
    padding: 0.05rem 0.35rem;
    border-radius: 0.25rem;
    background: var(--surface-alt, var(--background-secondary));
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
  }

  .source-time,
  .source-recurrence-count {
    font-variant-numeric: tabular-nums;
  }

  .source-snippet {
    font-size: 0.8rem;
    color: var(--text-secondary);
    line-height: 1.45;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
</style>
