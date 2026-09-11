<!--
  ChatSources.svelte — the citation cards under an assistant answer.

  This is the feature's trust surface: it turns "the AI said so" into "here is
  the moment in the recording where it was said". Every href is built from the
  STRUCTURED sources payload via citationHref(), never from anything the model
  wrote — which is why the markdown renderer blocks relative URLs outright.
-->
<script context="module" lang="ts">
  // Module-scope so every ChatSources instance on the page (one per assistant
  // message) gets a disjoint id namespace — see ExpandableSection.svelte's
  // identical idiom. Keying aria-controls off `source.id` alone collides
  // across instances, since citation ids restart at 1 per message.
  let instanceUid = 0;
</script>

<script lang="ts">
  import { t } from '$stores/locale';
  import { citationHref, formatClock } from '$lib/utils/chatMarkdown';
  import type { ChatSource } from '$lib/types/chat';

  export let sources: ChatSource[] = [];
  /** Collapsed by default under long answers; expanded while streaming context. */
  export let expanded = false;

  const instanceId = ++instanceUid;

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

  function toggle(): void {
    expanded = !expanded;
  }

  // The card showed up to this many chars in full before issue #913 (the old
  // citations.py::SNIPPET_CHARS). Two justifications, kept both because they
  // answer different questions:
  //  (a) visual — the chat column is 52rem, .source-snippet is 0.8rem inside
  //      0.75rem padding plus the source-index gutter, which fits roughly
  //      105-115 chars/line; 2 lines hold ~210-230 chars, and 240 is the
  //      smallest round number above that. A narrower viewport clips SOONER,
  //      so the toggle is never wrongly absent there either.
  //  (b) behavioural, and the one that actually matters: 240 is exactly what
  //      the card rendered in full before this change, so no citation that
  //      exists today gains a toggle that reveals nothing new.
  // Deliberately NOT measured via DOM overflow (`scrollHeight > clientHeight`):
  // jsdom computes neither, so that affordance would be untestable — the same
  // reason theme-parity.test.ts scans source instead of rendering.
  const SNIPPET_COLLAPSE_CHARS = 240;

  /** Per-card expanded state, keyed by citation id. Svelte 4 reactivity keys
   * off assignment, so every mutation below reassigns a new Set rather than
   * mutating this one in place. */
  let expandedSnippets = new Set<number>();

  function hasMore(source: ChatSource): boolean {
    return !!source.snippet && source.snippet.length > SNIPPET_COLLAPSE_CHARS;
  }

  function toggleSnippet(id: number): void {
    const next = new Set(expandedSnippets);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    expandedSnippets = next;
  }

  function snippetRegionId(source: ChatSource): string {
    return `chat-source-snippet-${instanceId}-${source.id}`;
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
          <li class="source-item">
            <a
              class="source-card"
              href={citationHref(source)}
              data-testid="chat-source-link"
              title={isSummary(source)
                ? $t('chat.sources.openSummary')
                : $t('chat.sources.openAt', { time: formatClock(source.start_time) })}
            >
              <span class="source-index">[{source.id}]</span>
              <span class="source-body">
                <span class="source-title">{source.title || $t('chat.sources.untitled')}</span>
                <span class="source-meta">
                  {#if isSummary(source)}
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
                  {#if !isSummary(source)}
                    <span class="source-time">{formatClock(source.start_time)}</span>
                  {/if}
                </span>
                {#if source.snippet}
                  <span
                    class="source-snippet"
                    class:clamped={!expandedSnippets.has(source.id)}
                    id={snippetRegionId(source)}
                    data-testid="chat-source-snippet">{source.snippet}</span
                  >
                {/if}
              </span>
            </a>
            {#if hasMore(source)}
              <button
                type="button"
                class="snippet-toggle"
                data-testid="chat-source-snippet-toggle"
                aria-expanded={expandedSnippets.has(source.id)}
                aria-controls={snippetRegionId(source)}
                aria-label={expandedSnippets.has(source.id)
                  ? $t('chat.sources.collapseExcerptLabel', { index: source.id })
                  : $t('chat.sources.expandExcerptLabel', { index: source.id })}
                on:click={() => toggleSnippet(source.id)}
              >
                {expandedSnippets.has(source.id)
                  ? $t('chat.sources.showLess')
                  : $t('chat.sources.showMore')}
              </button>
            {/if}
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

  /* Card chrome lives on the LI, not the anchor: the "show more" button is a
     sibling of the link (never nested inside it — a button inside an <a> is
     invalid HTML and an interactive-in-interactive a11y failure), and both
     need to sit inside the same bordered card. */
  .source-item {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
    overflow: hidden;
    transition:
      border-color 0.15s ease,
      background-color 0.15s ease;
  }

  .source-item:hover {
    border-color: rgba(var(--primary-color-rgb), 0.5);
    background-color: rgba(var(--primary-color-rgb), 0.05);
  }

  .source-card {
    display: flex;
    gap: 0.6rem;
    padding: 0.6rem 0.75rem;
    text-decoration: none;
    color: inherit;
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

  .source-time {
    font-variant-numeric: tabular-nums;
  }

  .source-snippet {
    font-size: 0.8rem;
    color: var(--text-secondary);
    line-height: 1.45;
  }

  /* Collapsed default: 2 lines, whatever the snippet's real length. Expanded
     state (aria-expanded="true" on the toggle) drops this class and the full
     snippet renders. */
  .source-snippet.clamped {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .snippet-toggle {
    align-self: flex-start;
    margin: -0.2rem 0.75rem 0.6rem calc(0.75rem + 0.6rem + 1.6rem);
    padding: 0.15rem 0.4rem;
    border: none;
    border-radius: 4px;
    background: none;
    color: var(--text-secondary);
    font-size: 0.75rem;
    font-weight: 500;
    cursor: pointer;
  }

  .snippet-toggle:hover {
    background-color: var(--button-hover);
    color: var(--primary-color);
  }

  .snippet-toggle:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }
</style>
