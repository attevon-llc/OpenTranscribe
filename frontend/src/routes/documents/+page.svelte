<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import DocumentUploadPanel from '$components/documents/DocumentUploadPanel.svelte';
  import DocumentCard from '$components/documents/DocumentCard.svelte';
  import { listDocuments, reparseDocument } from '$lib/api/documents';
  import type { DocumentResponse } from '$lib/types/document';

  const PAGE_SIZE = 50;

  let documents: DocumentResponse[] = [];
  let total = 0;
  let loading = true;
  let loadingMore = false;
  let error = '';
  let pollHandle: ReturnType<typeof setInterval> | null = null;

  let search = '';
  let sortBy: 'created_at' | 'filename' | 'file_size' = 'created_at';
  let sortOrder: 'asc' | 'desc' = 'desc';
  let searchDebounce: ReturnType<typeof setTimeout> | null = null;

  // `documents.length < total` is the "past position 200" fix (#362 lane C3): the
  // page used to fetch a single fixed batch of 200 and never ask for more, so a
  // document beyond that position was invisible no matter what the backend's own
  // per-page cap allowed. This drives the Load More affordance below instead.
  $: hasMore = documents.length < total;

  async function loadDocuments(reset: boolean) {
    if (reset) {
      loading = true;
      documents = [];
    } else {
      loadingMore = true;
    }
    try {
      const response = await listDocuments(reset ? 0 : documents.length, PAGE_SIZE, {
        search: search.trim() || undefined,
        sortBy,
        sortOrder,
      });
      documents = reset ? response.documents : [...documents, ...response.documents];
      total = response.total;
      error = '';
    } catch (err) {
      error = $t('documents.listLoadFailed');
    } finally {
      loading = false;
      loadingMore = false;
    }
  }

  function handleSearchInput() {
    if (searchDebounce) clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => loadDocuments(true), 300);
  }

  function handleSortChange() {
    loadDocuments(true);
  }

  async function handleRetry(event: CustomEvent<{ uuid: string }>) {
    try {
      const updated = await reparseDocument(event.detail.uuid);
      documents = documents.map((d) => (d.uuid === updated.uuid ? updated : d));
      toastStore.success($t('documents.addedToQueue'));
    } catch (err) {
      toastStore.error($t('documents.detailLoadFailed'));
    }
  }

  function handleUploaded() {
    toastStore.success($t('documents.addedToQueue'));
    // No document-list-scoped websocket event exists yet (document_status is keyed
    // and consumed by the detail page only — see stores/websocket.ts) — a short poll
    // picks up the new row and any status transitions cheaply for however many
    // documents a user reasonably has in flight at once (v1 scope; the media gallery's
    // server-driven pagination is overkill while document volumes stay far below file
    // volumes, per the plan this task was built against).
    loadDocuments(true);
    if (!pollHandle) {
      pollHandle = setInterval(() => loadDocuments(true), 4000);
      setTimeout(() => {
        if (pollHandle) {
          clearInterval(pollHandle);
          pollHandle = null;
        }
      }, 60000);
    }
  }

  onMount(() => loadDocuments(true));
  onDestroy(() => {
    if (pollHandle) clearInterval(pollHandle);
    if (searchDebounce) clearTimeout(searchDebounce);
  });
</script>

<svelte:head>
  <title>{$t('documents.pageTitle')} - OpenTranscribe</title>
</svelte:head>

<div class="documents-page">
  <header class="page-header">
    <h1>{$t('documents.pageTitle')}</h1>
    <p class="page-subtitle">{$t('documents.pageSubtitle')}</p>
  </header>

  <section class="upload-section">
    <DocumentUploadPanel on:uploaded={handleUploaded} />
  </section>

  {#if !loading && (documents.length > 0 || search)}
    <section class="controls-section">
      <input
        type="search"
        class="search-input"
        placeholder={$t('documents.searchPlaceholder')}
        bind:value={search}
        on:input={handleSearchInput}
      />
      <select class="sort-select" bind:value={sortBy} on:change={handleSortChange}>
        <option value="created_at">{$t('documents.sortNewest')}</option>
        <option value="filename">{$t('documents.sortName')}</option>
      </select>
      <button
        type="button"
        class="sort-order-btn"
        title={sortOrder === 'desc' ? $t('documents.sortOldest') : $t('documents.sortNewest')}
        on:click={() => {
          sortOrder = sortOrder === 'desc' ? 'asc' : 'desc';
          loadDocuments(true);
        }}
      >
        {sortOrder === 'desc' ? '↓' : '↑'}
      </button>
    </section>
  {/if}

  <section class="list-section">
    {#if loading}
      <div class="loading-state">
        <Spinner size="medium" />
      </div>
    {:else if error}
      <EmptyState title={error} />
    {:else if documents.length === 0}
      <EmptyState title={$t('documents.emptyTitle')} description={$t('documents.emptyDescription')}>
        <svelte:fragment slot="icon">
          <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
            <polyline points="14 2 14 8 20 8"></polyline>
          </svg>
        </svelte:fragment>
      </EmptyState>
    {:else}
      <div class="document-grid">
        {#each documents as doc (doc.uuid)}
          <DocumentCard {doc} on:retry={handleRetry} />
        {/each}
      </div>
      {#if hasMore}
        <div class="load-more-row">
          <button
            type="button"
            class="load-more-btn"
            disabled={loadingMore}
            on:click={() => loadDocuments(false)}
          >
            {#if loadingMore}
              <Spinner size="small" />
            {:else}
              {$t('documents.loadMore')} ({documents.length}/{total})
            {/if}
          </button>
        </div>
      {/if}
    {/if}
  </section>
</div>

<style>
  .documents-page {
    max-width: 1100px;
    margin: 0 auto;
    padding: 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
  }

  .page-header h1 {
    margin: 0 0 0.25rem;
    font-size: 1.5rem;
    color: var(--text-primary);
  }

  .page-subtitle {
    margin: 0;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  .upload-section {
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 1.25rem;
    background: var(--surface-color);
  }

  .loading-state {
    display: flex;
    justify-content: center;
    padding: 3rem 0;
  }

  .document-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 0.875rem;
  }

  .controls-section {
    display: flex;
    gap: 0.625rem;
    align-items: center;
  }

  .search-input {
    flex: 1;
    min-width: 0;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    color: var(--text-primary);
    font-size: 0.875rem;
  }

  .sort-select {
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    color: var(--text-primary);
    font-size: 0.875rem;
  }

  .sort-order-btn {
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    color: var(--text-primary);
    cursor: pointer;
    font-size: 0.9rem;
    line-height: 1;
  }

  .sort-order-btn:hover {
    border-color: var(--primary-color, #3b82f6);
  }

  .load-more-row {
    display: flex;
    justify-content: center;
    padding: 1rem 0;
  }

  .load-more-btn {
    padding: 0.5rem 1.25rem;
    border: 1px solid var(--border-color);
    border-radius: 999px;
    background: var(--surface-color);
    color: var(--text-primary);
    cursor: pointer;
    font-size: 0.85rem;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .load-more-btn:hover:not(:disabled) {
    border-color: var(--primary-color, #3b82f6);
  }

  .load-more-btn:disabled {
    cursor: default;
    opacity: 0.7;
  }
</style>
