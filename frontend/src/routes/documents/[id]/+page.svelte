<script lang="ts">
  /**
   * Document detail: coordinator (data loading, websocket, citation-jump) —
   * children are thin (DocumentOriginalViewer, DocumentParsedTextViewer), per this
   * repo's coordinator/child convention (frontend/src/components/CLAUDE.md).
   */
  import { onDestroy, onMount, tick } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import Spinner from '$components/ui/Spinner.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import CommentSection from '$components/CommentSection.svelte';
  import SummarySearch from '$components/SummarySearch.svelte';
  import DocumentOriginalViewer from '$components/documents/DocumentOriginalViewer.svelte';
  import DocumentParsedTextViewer from '$components/documents/DocumentParsedTextViewer.svelte';
  import ShareDocumentModal from '$components/documents/ShareDocumentModal.svelte';
  import {
    getDocument,
    getDocumentChunks,
    getDocumentDownloadUrl,
    deleteDocument,
  } from '$lib/api/documents';
  import type { DocumentResponse, DocumentChunkResponse } from '$lib/types/document';

  export let data: { uuid: string };
  $: documentUuid = data.uuid;

  let doc: DocumentResponse | null = null;
  let chunks: DocumentChunkResponse[] = [];
  let loading = true;
  let error = '';
  let activeTab: 'original' | 'text' = 'text';
  let showDeleteConfirm = false;
  let deleting = false;
  let showShareModal = false;
  let commentCount = 0;

  // Search-within-document (v400, #362 lane C5) — state owned by the coordinator
  // (this page), matching count/highlighting delegated to DocumentParsedTextViewer
  // via $lib/utils/searchHighlight, the same reuse rule SummaryModal's own find bar
  // follows for its own child.
  let searchQuery = '';
  let currentMatchIndex = 0;
  let totalMatches = 0;

  // v400 (#362 lane C3-remainder) — Share and Delete are owner-only on the backend
  // (`_get_owned_document(..., min_permission="owner")` in documents.py); a viewer or
  // editor sharee who clicked either got a bare 404 "Document not found" (the same
  // 404 a stranger gets, by design — see that function's docstring). Gate the
  // buttons on `doc.my_permission`, mirroring `files/[id]/+page.svelte`'s own
  // `canEdit` derivation and the `null`-means-owner convention `my_permission` uses.
  $: canManage = !!doc && (!doc.my_permission || doc.my_permission === 'owner');

  // document_status ticks (parsing progress) — see stores/websocket.ts for why this
  // is a raw window CustomEvent rather than the shared progressive-notification path.
  let parseStatus: { status: string; message: string; progress: number } | null = null;
  let documentStatusHandler: ((event: Event) => void) | null = null;

  async function loadDocument() {
    try {
      doc = await getDocument(documentUuid);
      if (doc.status === 'completed') {
        const chunkResponse = await getDocumentChunks(documentUuid);
        chunks = chunkResponse.chunks;
      }
      error = '';
    } catch (err) {
      error = $t('documents.detailLoadFailed');
    } finally {
      loading = false;
    }
  }

  async function applyChunkHighlight() {
    const chunkParam = $page.url.searchParams.get('chunk');
    if (chunkParam === null || activeTab !== 'text') return;
    const chunkIndex = parseInt(chunkParam, 10);
    if (isNaN(chunkIndex)) return;

    await tick();
    setTimeout(() => {
      const el = document.querySelector(`[data-chunk-index="${chunkIndex}"]`);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.classList.add('highlight-flash');
        setTimeout(() => el.classList.remove('highlight-flash'), 2000);
      }
    }, 300);
  }

  function switchTab(tab: 'original' | 'text') {
    activeTab = tab;
    if (tab === 'text') void applyChunkHighlight();
  }

  // Search-within-document handlers — mirrors TranscriptModal/SummaryModal's own
  // next/previous/clear cycle, just driving DocumentParsedTextViewer's props
  // instead of local highlighting.
  function handleSearch(event: CustomEvent<{ query: string }>) {
    searchQuery = event.detail.query;
    currentMatchIndex = 0;
    void scrollToCurrentMatch();
  }

  function handleClearSearch() {
    searchQuery = '';
    currentMatchIndex = 0;
  }

  function handleNextMatch() {
    if (totalMatches === 0) return;
    currentMatchIndex = (currentMatchIndex + 1) % totalMatches;
    void scrollToCurrentMatch();
  }

  function handlePreviousMatch() {
    if (totalMatches === 0) return;
    currentMatchIndex = currentMatchIndex > 0 ? currentMatchIndex - 1 : totalMatches - 1;
    void scrollToCurrentMatch();
  }

  async function scrollToCurrentMatch() {
    await tick();
    setTimeout(() => {
      const current = document.querySelector('.transcript-search-highlight.current');
      if (current) {
        current.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }, 50);
  }

  async function handleDownload(forceDownload: boolean) {
    if (!doc) return;
    try {
      const { url } = await getDocumentDownloadUrl(documentUuid, forceDownload);
      window.open(url, '_blank');
    } catch {
      toastStore.error($t('documents.downloadFailed'));
    }
  }

  async function confirmDelete() {
    deleting = true;
    try {
      await deleteDocument(documentUuid);
      toastStore.success($t('documents.deleted'));
      await goto('/documents');
    } catch {
      toastStore.error($t('documents.deleteFailed'));
      deleting = false;
      showDeleteConfirm = false;
    }
  }

  onMount(() => {
    loadDocument().then(() => {
      if ($page.url.searchParams.get('chunk') !== null) {
        activeTab = 'text';
        void applyChunkHighlight();
      }
    });

    documentStatusHandler = (event: Event) => {
      const detail = (event as CustomEvent).detail as {
        document_id?: string;
        status?: string;
        message?: string;
        progress?: number;
      };
      if (!detail?.document_id || detail.document_id !== documentUuid) return;

      parseStatus = {
        status: detail.status || 'processing',
        message: detail.message || '',
        progress: detail.progress ?? 0,
      };

      if (detail.status === 'completed' || detail.status === 'error') {
        loadDocument();
      }
    };
    window.addEventListener('document-status', documentStatusHandler);
  });

  onDestroy(() => {
    if (documentStatusHandler) {
      window.removeEventListener('document-status', documentStatusHandler);
    }
  });
</script>

<svelte:head>
  <title>{doc?.filename || $t('documents.pageTitle')} - OpenTranscribe</title>
</svelte:head>

<div class="detail-page">
  {#if loading}
    <div class="loading-state">
      <Spinner size="large" />
    </div>
  {:else if error || !doc}
    <div class="error-state">
      <p>{error || $t('documents.notFound')}</p>
      <a href="/documents">{$t('documents.backToList')}</a>
    </div>
  {:else}
    <header class="detail-header">
      <div class="header-main">
        <a href="/documents" class="back-link">&larr; {$t('documents.backToList')}</a>
        <h1 title={doc.filename}>{doc.filename}</h1>
        <div class="header-meta">
          <span class="status-badge">{doc.display_status}</span>
          {#if doc.page_count}
            <span>{$t('documents.pageCount', { count: doc.page_count })}</span>
          {/if}
          {#if doc.chunk_count > 0}
            <span>{$t('documents.chunkCount', { count: doc.chunk_count })}</span>
          {/if}
        </div>
      </div>
      <div class="header-actions">
        {#if canManage}
          <button type="button" class="action-btn" on:click={() => (showShareModal = true)}>
            {$t('sharing.shareButton')}
          </button>
        {/if}
        <button type="button" class="action-btn" on:click={() => handleDownload(true)}>
          {$t('documents.download')}
        </button>
        {#if canManage}
          <button
            type="button"
            class="action-btn action-btn-danger"
            on:click={() => (showDeleteConfirm = true)}
          >
            {$t('documents.delete')}
          </button>
        {/if}
      </div>
    </header>

    {#if doc.status === 'processing' || doc.status === 'pending'}
      <div class="parse-progress">
        <Spinner size="small" />
        <span>
          {parseStatus?.message || $t('documents.parsingInProgress')}
          {#if parseStatus}({Math.round(parseStatus.progress)}%){/if}
        </span>
      </div>
    {:else if doc.status === 'error'}
      <div class="parse-error">
        <p>{doc.last_error_message || $t('documents.parseFailedGeneric')}</p>
      </div>
    {/if}

    <div class="tab-nav">
      <button
        type="button"
        class="tab-btn"
        class:active={activeTab === 'original'}
        on:click={() => switchTab('original')}
      >
        {$t('documents.tabOriginal')}
      </button>
      <button
        type="button"
        class="tab-btn"
        class:active={activeTab === 'text'}
        on:click={() => switchTab('text')}
        disabled={doc.status !== 'completed'}
      >
        {$t('documents.tabParsedText')}
      </button>
    </div>

    {#if activeTab === 'text' && doc.status === 'completed'}
      <SummarySearch
        bind:searchQuery
        totalMatches={totalMatches}
        {currentMatchIndex}
        on:search={handleSearch}
        on:clearSearch={handleClearSearch}
        on:nextMatch={handleNextMatch}
        on:previousMatch={handlePreviousMatch}
      />
    {/if}

    <div class="tab-content">
      {#if activeTab === 'original'}
        <DocumentOriginalViewer
          documentUuid={doc.uuid}
          contentType={doc.content_type}
          filename={doc.filename}
        />
      {:else if doc.status === 'completed'}
        <DocumentParsedTextViewer
          {chunks}
          {searchQuery}
          {currentMatchIndex}
          on:matchesChanged={(e) => (totalMatches = e.detail.total)}
        />
      {:else}
        <p class="not-ready-note">{$t('documents.textNotReadyYet')}</p>
      {/if}
    </div>

    <section class="notes-section">
      <CommentSection
        mode="document"
        documentId={doc.uuid}
        on:commentsChanged={(e) => (commentCount = e.detail.count)}
      />
    </section>
  {/if}
</div>

{#if showShareModal && doc}
  <ShareDocumentModal
    documentUuid={doc.uuid}
    documentName={doc.filename}
    on:close={() => (showShareModal = false)}
    on:shared={() => (showShareModal = false)}
  />
{/if}

<ConfirmationModal
  bind:isOpen={showDeleteConfirm}
  title={$t('documents.deleteConfirmTitle')}
  message={$t('documents.deleteConfirmMessage')}
  confirmText={deleting ? $t('documents.deleting') : $t('documents.delete')}
  cancelText={$t('documents.cancel')}
  confirmButtonClass="modal-warning-button"
  cancelButtonClass="modal-primary-button"
  on:confirm={confirmDelete}
  on:cancel={() => (showDeleteConfirm = false)}
/>

<style>
  .detail-page {
    max-width: 1100px;
    margin: 0 auto;
    padding: 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .loading-state,
  .error-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.75rem;
    padding: 4rem 1rem;
    color: var(--text-secondary);
  }

  .detail-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    flex-wrap: wrap;
  }

  .back-link {
    display: inline-block;
    font-size: 0.8rem;
    color: var(--text-secondary);
    text-decoration: none;
    margin-bottom: 0.375rem;
  }

  .back-link:hover {
    color: var(--primary-color, #3b82f6);
  }

  .header-main h1 {
    margin: 0 0 0.375rem;
    font-size: 1.35rem;
    color: var(--text-primary);
    overflow-wrap: anywhere;
  }

  .header-meta {
    display: flex;
    gap: 0.75rem;
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  .status-badge {
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    background: rgba(59, 130, 246, 0.15);
    color: #3b82f6;
    font-weight: 600;
    font-size: 0.72rem;
  }

  .header-actions {
    display: flex;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .action-btn {
    padding: 0.5rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    color: var(--text-primary);
    cursor: pointer;
    font-size: 0.85rem;
    font-weight: 500;
  }

  .action-btn:hover {
    background: var(--button-hover);
  }

  .action-btn-danger {
    color: #ef4444;
    border-color: rgba(239, 68, 68, 0.3);
  }

  .action-btn-danger:hover {
    background: rgba(239, 68, 68, 0.08);
  }

  .parse-progress {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    padding: 0.75rem 1rem;
    background: rgba(59, 130, 246, 0.08);
    border: 1px solid rgba(59, 130, 246, 0.25);
    border-radius: 8px;
    font-size: 0.85rem;
    color: var(--text-primary);
  }

  .parse-error {
    padding: 0.75rem 1rem;
    background: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.25);
    border-radius: 8px;
    color: #ef4444;
    font-size: 0.85rem;
  }

  .parse-error p {
    margin: 0;
  }

  .tab-nav {
    display: flex;
    gap: 0.25rem;
    border-bottom: 1px solid var(--border-color);
  }

  .tab-btn {
    padding: 0.625rem 1rem;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-secondary);
    cursor: pointer;
    font-size: 0.875rem;
    font-weight: 500;
  }

  .tab-btn:hover:not(:disabled) {
    color: var(--text-primary);
  }

  .tab-btn.active {
    color: var(--primary-color, #3b82f6);
    border-bottom-color: var(--primary-color, #3b82f6);
  }

  .tab-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .tab-content {
    min-height: 400px;
  }

  .not-ready-note {
    padding: 3rem 1rem;
    text-align: center;
    color: var(--text-secondary);
  }

  .notes-section {
    margin-top: 0.5rem;
  }
</style>
