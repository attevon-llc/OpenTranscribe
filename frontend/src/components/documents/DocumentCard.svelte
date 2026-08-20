<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { formatDate } from '$lib/utils/formatting';
  import { formatFileSize } from '$lib/utils/metadataMapper';
  import type { DocumentResponse } from '$lib/types/document';

  export let doc: DocumentResponse;
  /** Gallery bulk-select (v400, #362 lane C3-remainder) — parity with the media
   * gallery's bulk operations. When true the card becomes a selection toggle
   * instead of a navigation link. */
  export let selectionMode = false;
  export let selected = false;

  const dispatch = createEventDispatcher<{ retry: { uuid: string }; toggleSelect: { uuid: string } }>();

  function handleRetry(event: MouseEvent) {
    // The card is an <a>; a click on the retry button must not also navigate.
    event.preventDefault();
    event.stopPropagation();
    dispatch('retry', { uuid: doc.uuid });
  }

  function handleCardClick(event: MouseEvent) {
    if (!selectionMode) return;
    event.preventDefault();
    dispatch('toggleSelect', { uuid: doc.uuid });
  }

  const EXT_ICON: Record<string, string> = {
    'application/pdf': '📄',
    'text/html': '🌐',
    'text/markdown': '📝',
    'text/csv': '📊',
    'text/plain': '📃',
  };

  $: icon =
    EXT_ICON[doc.content_type] ||
    (doc.content_type.includes('word')
      ? '📃'
      : doc.content_type.includes('presentation')
        ? '📊'
        : doc.content_type.includes('sheet')
          ? '📊'
          : '📎');

  $: statusClass =
    doc.status === 'completed'
      ? 'status-ready'
      : doc.status === 'error'
        ? 'status-error'
        : doc.status === 'processing'
          ? 'status-processing'
          : 'status-pending';
</script>

<a
  class="document-card"
  class:selection-mode={selectionMode}
  class:selected
  href="/documents/{doc.uuid}"
  on:click={handleCardClick}
>
  {#if selectionMode}
    <input
      type="checkbox"
      class="card-select-checkbox"
      checked={selected}
      on:click|stopPropagation={() => dispatch('toggleSelect', { uuid: doc.uuid })}
      aria-label={$t('gallery.selectItem')}
    />
  {/if}
  <div class="card-icon">{icon}</div>
  <div class="card-body">
    <span class="card-filename" title={doc.filename}>{doc.filename}</span>
    <div class="card-meta">
      <span class="status-badge {statusClass}">{doc.display_status}</span>
      {#if doc.page_count}
        <span class="meta-item">{$t('documents.pageCount', { count: doc.page_count })}</span>
      {/if}
      {#if doc.chunk_count > 0}
        <span class="meta-item">{$t('documents.chunkCount', { count: doc.chunk_count })}</span>
      {/if}
      <span class="meta-item">{formatFileSize(doc.file_size)}</span>
    </div>
    {#if doc.status === 'error' && doc.last_error_message}
      <span class="card-error" title={doc.last_error_message}>
        {doc.last_error_message}
      </span>
      <button type="button" class="card-retry" on:click={handleRetry}>
        {$t('gallery.retry')}
      </button>
    {/if}
    <span class="card-date">{formatDate(doc.created_at)}</span>
  </div>
</a>

<style>
  .document-card {
    display: flex;
    gap: 0.875rem;
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 10px;
    background: var(--surface-color);
    text-decoration: none;
    color: inherit;
    transition:
      border-color 0.15s ease,
      box-shadow 0.15s ease;
  }

  .document-card:hover {
    border-color: var(--primary-color, #3b82f6);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
  }

  .document-card.selection-mode {
    cursor: pointer;
  }

  .document-card.selected {
    border-color: var(--primary-color, #3b82f6);
    background: rgba(59, 130, 246, 0.06);
  }

  .card-select-checkbox {
    flex-shrink: 0;
    width: 18px;
    height: 18px;
    margin-top: 0.125rem;
    cursor: pointer;
    accent-color: var(--primary-color, #3b82f6);
  }

  .card-icon {
    font-size: 1.75rem;
    flex-shrink: 0;
    line-height: 1;
  }

  .card-body {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 0;
    flex: 1;
  }

  .card-filename {
    font-weight: 600;
    font-size: 0.9rem;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .card-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
    font-size: 0.75rem;
    color: var(--text-secondary);
  }

  .status-badge {
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    font-weight: 600;
    font-size: 0.7rem;
  }

  .status-ready {
    background: rgba(34, 197, 94, 0.15);
    color: #16a34a;
  }

  .status-processing {
    background: rgba(59, 130, 246, 0.15);
    color: #3b82f6;
  }

  .status-pending {
    background: rgba(156, 163, 175, 0.15);
    color: var(--text-secondary);
  }

  .status-error {
    background: rgba(239, 68, 68, 0.15);
    color: #ef4444;
  }

  .card-error {
    font-size: 0.75rem;
    color: #ef4444;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .card-date {
    font-size: 0.7rem;
    color: var(--text-secondary);
  }

  .card-retry {
    align-self: flex-start;
    margin-top: 0.125rem;
    padding: 0.2rem 0.6rem;
    font-size: 0.75rem;
    font-weight: 600;
    color: #ef4444;
    background: rgba(239, 68, 68, 0.1);
    border: 1px solid rgba(239, 68, 68, 0.3);
    border-radius: 6px;
    cursor: pointer;
    transition:
      background 0.15s ease,
      border-color 0.15s ease;
  }

  .card-retry:hover {
    background: rgba(239, 68, 68, 0.18);
    border-color: rgba(239, 68, 68, 0.5);
  }
</style>
