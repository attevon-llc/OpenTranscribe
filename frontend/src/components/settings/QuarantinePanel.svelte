<script lang="ts">
  /**
   * Abuse/DMCA takedown review queue (issue #576). Deployment-wide — every
   * owner/org, which is correct for this platform-admin surface (§B.6 E7); do
   * not reuse for an org-scoped view.
   *
   * §B.2's corollary applies here too: this panel offers Release only. It has
   * no button that reaches a GDPR erasure route — `AdminApi` carries zero GDPR
   * methods (`admin.gdpr.absence.test.ts` guards that), and a row's owner link
   * (when present) must go to User Management, never to an erasure action.
   */
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import Spinner from '$components/ui/Spinner.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import { AdminApi, type QuarantinedFile } from '$lib/api/admin';

  const PAGE_SIZE = 50;

  let files: QuarantinedFile[] = [];
  let total = 0;
  let offset = 0;
  let loading = false;
  let loadFailed = false;
  let releasingUuid: string | null = null;
  /** Quarantined queue (default) vs the released-but-still-held view (§B.4 /
   * issue #689's remedy). Not a subset of the first — a released file whose
   * hold was kept has `is_quarantined=false, legal_hold=true`. */
  let showLegalHoldsOnly = false;

  async function load(newOffset = 0) {
    loading = true;
    loadFailed = false;
    try {
      const result = await AdminApi.listQuarantinedFiles({
        limit: PAGE_SIZE,
        offset: newOffset,
        include_legal_holds: showLegalHoldsOnly,
      });
      files = result.files;
      total = result.total;
      offset = newOffset;
    } catch (err) {
      loadFailed = true;
      toastStore.error(getErrorMessage(err, $t('settings.quarantine.loadFailed')));
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    load();
  });

  function toggleView() {
    showLegalHoldsOnly = !showLegalHoldsOnly;
    load(0);
  }

  /**
   * Release, or lift a held-but-released file's hold (issue #689/#576 §B.1.2).
   * A 409 here means someone else already released it — refetch, never retry
   * (§B.6 E3).
   */
  async function release(file: QuarantinedFile, clearLegalHold: boolean) {
    releasingUuid = file.uuid;
    try {
      await AdminApi.releaseFile(file.uuid, clearLegalHold);
      toastStore.success($t('settings.quarantine.releaseSuccess'));
      total = Math.max(0, total - 1);
      await load(offset);
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.quarantine.releaseFailed')));
      await load(offset);
    } finally {
      releasingUuid = null;
    }
  }
</script>

<div class="quarantine-panel">
  <h3 class="section-title">{$t('settings.quarantine.title')}</h3>
  <p class="section-description">{$t('settings.quarantine.description')}</p>
  <!-- §B.5 P1: scope copy names what quarantine hides — never "everywhere". -->
  <p class="scope-note">{$t('settings.quarantine.scopeNote')}</p>

  <div class="view-toggle">
    <button
      class="toggle-btn"
      class:active={!showLegalHoldsOnly}
      on:click={() => showLegalHoldsOnly && toggleView()}
    >
      {$t('settings.quarantine.tabQuarantined')}
    </button>
    <button
      class="toggle-btn"
      class:active={showLegalHoldsOnly}
      on:click={() => !showLegalHoldsOnly && toggleView()}
    >
      {$t('settings.quarantine.tabLegalHold')}
    </button>
  </div>

  {#if loading && files.length === 0}
    <div class="state-row">
      <Spinner size="small" />
      <span>{$t('common.loading')}</span>
    </div>
  {:else if loadFailed}
    <div class="state-row error">
      <span>{$t('settings.quarantine.loadFailed')}</span>
      <button class="retry-btn" on:click={() => load(offset)}>{$t('common.retry')}</button>
    </div>
  {:else if total === 0}
    <EmptyState
      title={$t('settings.quarantine.emptyTitle')}
      description={$t('settings.quarantine.emptyMessage')}
    />
  {:else}
    <table class="quarantine-table">
      <thead>
        <tr>
          <th>{$t('settings.quarantine.columnFile')}</th>
          <th>{$t('settings.quarantine.columnOwner')}</th>
          <th>{$t('settings.quarantine.columnReason')}</th>
          <th>{$t('settings.quarantine.columnTakenDown')}</th>
          <th>{$t('settings.quarantine.columnBy')}</th>
          <th>{$t('settings.quarantine.columnLegalHold')}</th>
          <th>{$t('settings.quarantine.columnActions')}</th>
        </tr>
      </thead>
      <tbody>
        {#each files as file (file.uuid)}
          <tr>
            <td>
              <a href={`/files/${file.uuid}`} target="_blank" rel="noopener noreferrer">
                {file.title || file.filename || file.uuid}
              </a>
            </td>
            <!-- A person's email, never a bare integer id (issue #576 FINDING 3). -->
            <td>{file.owner_email}</td>
            <td class="reason-cell">{file.quarantine_reason || '—'}</td>
            <td>{file.quarantined_at || '—'}</td>
            <td>{file.quarantined_by_email || '—'}</td>
            <td>
              {#if file.legal_hold}
                <span class="badge badge-hold">{$t('settings.quarantine.legalHoldBadge')}</span>
              {/if}
            </td>
            <td class="actions-cell">
              {#if file.is_quarantined}
                <button
                  class="action-link"
                  disabled={releasingUuid === file.uuid}
                  on:click={() => release(file, true)}
                >
                  {$t('settings.quarantine.release')}
                </button>
              {:else if file.legal_hold}
                <button
                  class="action-link"
                  disabled={releasingUuid === file.uuid}
                  on:click={() => release(file, true)}
                >
                  {$t('settings.quarantine.liftHold')}
                </button>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>

    <div class="pagination">
      <button disabled={offset === 0 || loading} on:click={() => load(Math.max(0, offset - PAGE_SIZE))}>
        {$t('common.previous')}
      </button>
      <span>{offset + 1}-{Math.min(offset + PAGE_SIZE, total)} / {total}</span>
      <button disabled={offset + PAGE_SIZE >= total || loading} on:click={() => load(offset + PAGE_SIZE)}>
        {$t('common.next')}
      </button>
    </div>
  {/if}
</div>

<style>
  .quarantine-panel {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .scope-note {
    font-size: 0.75rem;
    color: var(--text-secondary, var(--text-color));
    margin: 0;
  }

  .view-toggle {
    display: flex;
    gap: 0.25rem;
    margin: 0.5rem 0;
  }

  .toggle-btn {
    padding: 0.375rem 0.875rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-color);
    color: var(--text-color);
    cursor: pointer;
    font-size: 0.8125rem;
  }

  .toggle-btn.active {
    background: var(--accent-color, #2563eb);
    color: white;
    border-color: var(--accent-color, #2563eb);
  }

  .state-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.75rem;
    border-radius: 6px;
    background: var(--bg-secondary, rgba(0, 0, 0, 0.03));
    font-size: 0.8125rem;
  }

  .state-row.error {
    color: #dc2626;
    justify-content: space-between;
  }

  .retry-btn {
    padding: 0.25rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: transparent;
    color: inherit;
    cursor: pointer;
  }

  .quarantine-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .quarantine-table th,
  .quarantine-table td {
    padding: 0.5rem;
    border-bottom: 1px solid var(--border-color);
    text-align: left;
    vertical-align: top;
  }

  .reason-cell {
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .badge {
    display: inline-block;
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    font-size: 0.6875rem;
    font-weight: 600;
  }

  .badge-hold {
    background: rgba(217, 119, 6, 0.15);
    color: #b45309;
  }

  :global([data-theme='dark']) .badge-hold {
    background: rgba(217, 119, 6, 0.25);
    color: #fbbf24;
  }

  .actions-cell {
    display: flex;
    gap: 0.75rem;
  }

  .action-link {
    border: none;
    background: none;
    color: var(--accent-color, #2563eb);
    cursor: pointer;
    padding: 0;
    font-size: 0.8125rem;
  }

  .action-link:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .pagination {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-top: 0.75rem;
    font-size: 0.8125rem;
  }

  .pagination button {
    padding: 0.25rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-color);
    color: var(--text-color);
    cursor: pointer;
  }

  .pagination button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
