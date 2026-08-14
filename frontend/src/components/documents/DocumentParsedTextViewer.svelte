<script lang="ts">
  /**
   * Renders a document's parsed chunks in reading order. Each chunk carries
   * `data-chunk-index` so the route (the coordinator, per this repo's convention —
   * children never own scroll/highlight) can jump to and flash a cited chunk via
   * `?chunk=<index>`, mirroring how the transcript viewer resolves `?t=<seconds>`.
   *
   * Chunk text is rendered AS THE BACKEND RETURNED IT. `GET /documents/{uuid}/chunks`
   * does not currently apply redaction masking (unlike transcript segment reads,
   * which mask via services/redaction/spans.py at read time) — a real backend gap,
   * not something to paper over here. See this task's final report.
   */
  import { t } from '$stores/locale';
  import type { DocumentChunkResponse } from '$lib/types/document';

  export let chunks: DocumentChunkResponse[];

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
        {chunk.text}
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
