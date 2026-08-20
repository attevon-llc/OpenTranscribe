<!--
  DocumentQuarantinePanel.svelte — admin review of taken-down documents (v399/#362
  lane C4 built `GET /documents/admin/quarantined` + `POST /documents/{uuid}/release`;
  nothing consumed them until this, v400 lane C3-remainder).

  Mounted as a SettingsModal admin section (`document-quarantine`), same pattern every
  other admin panel in `$components/settings/` uses — there is no dedicated `/admin`
  route in this SPA, admin surfaces are SettingsModal tabs.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import Spinner from '$components/ui/Spinner.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { formatDate } from '$lib/utils/formatting';
  import { listQuarantinedDocuments, releaseDocument } from '$lib/api/documents';
  import type { QuarantinedDocument } from '$lib/types/document';

  let documents: QuarantinedDocument[] = [];
  let total = 0;
  let loading = true;
  let error = '';
  let releasingUuid: string | null = null;
  let confirmTarget: QuarantinedDocument | null = null;

  async function load() {
    loading = true;
    try {
      const data = await listQuarantinedDocuments();
      documents = data.documents;
      total = data.total;
      error = '';
    } catch {
      error = $t('documents.adminQuarantineLoadFailed');
    } finally {
      loading = false;
    }
  }

  function requestRelease(doc: QuarantinedDocument) {
    confirmTarget = doc;
  }

  async function confirmRelease() {
    if (!confirmTarget) return;
    const target = confirmTarget;
    confirmTarget = null;
    releasingUuid = target.uuid;
    try {
      await releaseDocument(target.uuid, true);
      documents = documents.filter((d) => d.uuid !== target.uuid);
      total -= 1;
      toastStore.success($t('documents.adminQuarantineReleased', { filename: target.filename }));
    } catch {
      toastStore.error($t('documents.adminQuarantineReleaseFailed'));
    } finally {
      releasingUuid = null;
    }
  }

  onMount(load);
</script>

<div class="quarantine-panel">
  <div class="panel-header">
    <h3>{$t('documents.adminQuarantineTitle')}</h3>
    {#if !loading}
      <span class="count-chip">{total}</span>
    {/if}
  </div>

  {#if loading}
    <div class="loading-state">
      <Spinner size="medium" />
    </div>
  {:else if error}
    <EmptyState title={error} />
  {:else if documents.length === 0}
    <EmptyState title={$t('documents.adminQuarantineEmpty')}>
      <svelte:fragment slot="icon">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M9 12l2 2 4-4"></path>
          <circle cx="12" cy="12" r="10"></circle>
        </svg>
      </svelte:fragment>
    </EmptyState>
  {:else}
    <ul class="quarantine-list">
      {#each documents as doc (doc.uuid)}
        <li class="quarantine-row">
          <div class="row-main">
            <span class="row-filename" title={doc.filename}>{doc.filename}</span>
            {#if doc.legal_hold}
              <span class="legal-hold-badge">{$t('documents.adminQuarantineLegalHold')}</span>
            {/if}
          </div>
          {#if doc.quarantine_reason}
            <p class="row-reason">{doc.quarantine_reason}</p>
          {/if}
          <div class="row-meta">
            {#if doc.quarantined_at}
              <span>{$t('documents.adminQuarantineDate')}: {formatDate(doc.quarantined_at)}</span>
            {/if}
          </div>
          <div class="row-actions">
            <button
              type="button"
              class="release-btn"
              disabled={releasingUuid === doc.uuid}
              on:click={() => requestRelease(doc)}
            >
              {#if releasingUuid === doc.uuid}
                <Spinner size="small" />
              {:else}
                {$t('documents.adminQuarantineRelease')}
              {/if}
            </button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<ConfirmationModal
  isOpen={confirmTarget !== null}
  title={$t('documents.adminQuarantineRelease')}
  message={confirmTarget
    ? $t('documents.adminQuarantineReleaseConfirm', { filename: confirmTarget.filename })
    : ''}
  confirmText={$t('documents.adminQuarantineRelease')}
  cancelText={$t('documents.cancel')}
  confirmButtonClass="modal-primary-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={confirmRelease}
  on:cancel={() => (confirmTarget = null)}
  on:close={() => (confirmTarget = null)}
/>

<style>
  .quarantine-panel {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .panel-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .panel-header h3 {
    margin: 0;
    font-size: 1.05rem;
    color: var(--text-primary);
  }

  .count-chip {
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    background: rgba(239, 68, 68, 0.12);
    color: #ef4444;
    font-weight: 600;
    font-size: 0.75rem;
  }

  .loading-state {
    display: flex;
    justify-content: center;
    padding: 2rem 0;
  }

  .quarantine-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.625rem;
  }

  .quarantine-row {
    padding: 0.875rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 10px;
    background: var(--surface-color);
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .row-main {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .row-filename {
    font-weight: 600;
    font-size: 0.9rem;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 100%;
  }

  .legal-hold-badge {
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    background: rgba(234, 179, 8, 0.15);
    color: #ca8a04;
    font-weight: 600;
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.02em;
  }

  .row-reason {
    margin: 0;
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  .row-meta {
    font-size: 0.72rem;
    color: var(--text-secondary);
  }

  .row-actions {
    display: flex;
    justify-content: flex-end;
  }

  .release-btn {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.35rem 0.9rem;
    border: 1px solid var(--primary-color, #3b82f6);
    border-radius: 8px;
    background: transparent;
    color: var(--primary-color, #3b82f6);
    cursor: pointer;
    font-size: 0.8rem;
    font-weight: 600;
  }

  .release-btn:hover:not(:disabled) {
    background: rgba(59, 130, 246, 0.08);
  }

  .release-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
