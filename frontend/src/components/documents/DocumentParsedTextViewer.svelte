<script lang="ts">
  /**
   * Renders a document's parsed chunks in reading order. Each chunk carries
   * `data-chunk-index` so the route (the coordinator, per this repo's convention —
   * children never own scroll/highlight) can jump to and flash a cited chunk via
   * `?chunk=<index>`, mirroring how the transcript viewer resolves `?t=<seconds>`.
   *
   * Chunk text is rendered as the backend returned it — `GET /documents/{uuid}/chunks`
   * now masks at read time (v400, #362 lane C5) using the same
   * services/redaction/spans.py transform transcript segment reads use, so this
   * component has nothing further to do for redaction.
   *
   * Search-within-document reuses `$lib/utils/searchHighlight` (the same utility
   * `TranscriptModal`/`SummaryModal` highlight with) via `{@html
   * sanitizeHighlightHtml(...)}` — never a second highlighter.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { DocumentChunkResponse } from '$lib/types/document';
  import type { SearchMatch } from '$lib/utils/searchHighlight';
  import { highlightTextWithMatches } from '$lib/utils/searchHighlight';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';

  export let chunks: DocumentChunkResponse[];
  /**
   * Search-within-document (v400, #362 lane C5). Reuses the SAME highlight
   * machinery `TranscriptModal`/`SummaryModal` use (`$lib/utils/searchHighlight`)
   * rather than a second highlighter — `SearchMatch.segmentIndex` maps onto
   * `chunk.chunk_index`, which is exactly the shape that utility was built for.
   */
  export let searchQuery: string = '';
  export let currentMatchIndex: number = 0;

  const dispatch = createEventDispatcher<{ matchesChanged: { total: number } }>();

  // Chunk indices that start a new page — computed once per `chunks` change so the
  // markup below has no side-effecting assignments in it.
  $: pageBreakBefore = (() => {
    const set = new Set<number>();
    let lastPage: number | null = null;
    for (const chunk of chunks) {
      if (chunk.page !== null && chunk.page !== lastPage) {
        set.add(chunk.chunk_index);
        lastPage = chunk.page;
      }
    }
    return set;
  })();

  // Every occurrence of `searchQuery`, across every chunk in reading order — the
  // same flat SearchMatch[] shape `highlightTextWithMatches` was designed to
  // highlight ONE currentMatchIndex out of, spanning many segments.
  $: matches = (() => {
    const query = searchQuery.trim();
    if (!query) return [] as SearchMatch[];
    const lowerQuery = query.toLowerCase();
    const found: SearchMatch[] = [];
    for (const chunk of chunks) {
      const lowerText = (chunk.text || '').toLowerCase();
      let index = lowerText.indexOf(lowerQuery);
      while (index !== -1) {
        found.push({
          segmentIndex: chunk.chunk_index,
          start: index,
          length: query.length,
          type: 'text',
        });
        index = lowerText.indexOf(lowerQuery, index + query.length);
      }
    }
    return found;
  })();

  $: dispatch('matchesChanged', { total: matches.length });
</script>

<div class="parsed-text">
  {#if chunks.length === 0}
    <p class="empty-note">{$t('documents.noChunksYet')}</p>
  {:else}
    {#each chunks as chunk (chunk.chunk_index)}
      {#if pageBreakBefore.has(chunk.chunk_index)}
        <div class="page-marker">{$t('documents.pageMarker', { page: chunk.page })}</div>
      {/if}
      <p
        class="chunk"
        class:table-chunk={chunk.block_types?.includes('table')}
        data-chunk-index={chunk.chunk_index}
      >
        {@html sanitizeHighlightHtml(
          highlightTextWithMatches(chunk.text, searchQuery, chunk.chunk_index, matches, currentMatchIndex)
        )}
      </p>
    {/each}
  {/if}
</div>

<style>
  .parsed-text {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    padding: 0.5rem 0.25rem;
    line-height: 1.6;
    font-size: 0.9rem;
    color: var(--text-primary);
  }

  .empty-note {
    color: var(--text-secondary);
    padding: 2rem 0;
    text-align: center;
  }

  .page-marker {
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    color: var(--text-secondary);
    padding-top: 0.5rem;
    border-top: 1px dashed var(--border-color);
  }

  .chunk {
    margin: 0;
    scroll-margin-top: 80px;
    border-radius: 6px;
    padding: 0.25rem 0.375rem;
    transition: background-color 0.3s ease;
  }

  .table-chunk {
    font-family: var(--font-mono, monospace);
    white-space: pre-wrap;
    font-size: 0.8rem;
    background: var(--background-color);
    padding: 0.5rem;
  }

  :global(.chunk.highlight-flash) {
    background-color: rgba(59, 130, 246, 0.18);
  }
</style>
