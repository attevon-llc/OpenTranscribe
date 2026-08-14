<script lang="ts">
  import { t } from '$stores/locale';
  import { formatDate } from '$lib/utils/formatting';
  import { formatFileSize } from '$lib/utils/metadataMapper';
  import type { DocumentResponse } from '$lib/types/document';

  export let doc: DocumentResponse;

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

<a class="document-card" href="/documents/{doc.uuid}">
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
</style>
