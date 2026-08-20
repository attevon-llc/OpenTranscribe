<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import DocumentUploadPanel from '$components/documents/DocumentUploadPanel.svelte';
  import DocumentCard from '$components/documents/DocumentCard.svelte';
  import { listDocuments } from '$lib/api/documents';
  import type { DocumentResponse } from '$lib/types/document';

  let documents: DocumentResponse[] = [];
  let loading = true;
  let error = '';
  let pollHandle: ReturnType<typeof setInterval> | null = null;

  async function loadDocuments() {
    try {
      const response = await listDocuments(0, 200);
      documents = response.documents;
      error = '';
    } catch (err) {
      error = $t('documents.listLoadFailed');
    } finally {
      loading = false;
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
    loadDocuments();
    if (!pollHandle) {
      pollHandle = setInterval(loadDocuments, 4000);
      setTimeout(() => {
        if (pollHandle) {
          clearInterval(pollHandle);
          pollHandle = null;
        }
      }, 60000);
    }
  }

  onMount(loadDocuments);
  onDestroy(() => {
    if (pollHandle) clearInterval(pollHandle);
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
          <DocumentCard {doc} />
        {/each}
      </div>
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
</style>
