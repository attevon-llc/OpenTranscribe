<script lang="ts">
  import OffsetPager from '$components/ui/OffsetPager.svelte';
  import { resolve } from '$app/paths';
  /**
   * Abuse/DMCA takedown review queue (issue #576).
   *
   * `scope="platform"` (default) is the deployment-wide operator queue — every
   * owner/org, which is correct for that surface (§B.6 E7). `scope="organization"`
   * (issue #1253) is an organization administrator's own queue: it talks to a
   * different client (`OrgAdminApi`, never `AdminApi`), lists only that
   * organization's files, and offers Release only where the server says
   * `can_release` — a takedown the operator placed can only be released by the
   * operator, and its reason and actor are not sent at all.
   *
   * §B.2's corollary applies here too: this panel offers Release only. It has
   * no button that reaches a GDPR erasure route — `AdminApi` carries zero GDPR
   * methods (`admin.gdpr.absence.test.ts` guards that), and a row's owner link
   * (when present) must go to User Management, never to an erasure action.
   *
   * The operator queue also offers "Quarantine files": the gallery's takedown form
   * (`gallery/QuarantineModal`) with a search-and-select picker in place of a fixed
   * selection, so no file ID is ever typed. Organization scope does not offer it, matching
   * the gallery, where only instance administrators see Quarantine.
   */
  import { onMount } from 'svelte';
  import { t, locale } from '$stores/locale';
  import { formatDateTime } from '$lib/utils/formatting';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import Spinner from '$components/ui/Spinner.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import { AdminApi } from '$lib/api/admin';
  import { mediaRevocationIncomplete } from '$lib/takedownRevocation';
  import { OrgAdminApi } from '$lib/api/orgAdmin';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import TakedownStatusChip from '$components/takedown/TakedownStatusChip.svelte';
  import QuarantineModal from '$components/gallery/QuarantineModal.svelte';
  import { toRows, type QueueRow, type QueueScope } from './quarantineRows';
  import { TAKEDOWN_NOTICE_TYPE_KEYS, labelFor } from '$lib/i18n/keyMaps';

  const PAGE_SIZE = 50;

  export let scope: QueueScope = 'platform';

  $: isOrg = scope === 'organization';

  let files: QueueRow[] = [];
  let total = 0;
  let offset = 0;
  let loading = false;
  let loadFailed = false;
  let releasingUuid: string | null = null;
  /** Quarantined queue (default) vs the released-but-still-held view (§B.4 /
   * issue #689's remedy). Not a subset of the first — a released file whose
   * hold was kept has `is_quarantined=false, legal_hold=true`. */
  let showLegalHoldsOnly = false;
  let quarantining = false;

  async function load(newOffset = 0) {
    loading = true;
    loadFailed = false;
    try {
      const params = {
        limit: PAGE_SIZE,
        offset: newOffset,
        include_legal_holds: showLegalHoldsOnly,
      };
      const result = isOrg
        ? await OrgAdminApi.listQuarantinedFiles(params)
        : await AdminApi.listQuarantinedFiles(params);
      files = toRows(scope, result.files);
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
    void load();
  });

  function toggleView() {
    showLegalHoldsOnly = !showLegalHoldsOnly;
    void load(0);
  }

  /**
   * Release, or lift a held-but-released file's hold (issue #689/#576 §B.1.2).
   * A 409 here means someone else already released it — refetch, never retry
   * (§B.6 E3).
   */
  async function release(file: QueueRow, clearLegalHold: boolean) {
    releasingUuid = file.uuid;
    try {
      const result = isOrg
        ? await OrgAdminApi.releaseFile(file.uuid)
        : await AdminApi.releaseFile(file.uuid, clearLegalHold);
      // Released in the database, but storage kept the block on the media: not a clean success.
      if (mediaRevocationIncomplete(result)) {
        toastStore.warning($t('settings.quarantine.releaseRestoreIncomplete'));
      } else {
        toastStore.success($t('settings.quarantine.releaseSuccess'));
      }
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
  <h3 class="section-title">
    {$t(isOrg ? 'settings.quarantine.orgTitle' : 'settings.quarantine.title')}
  </h3>
  <p class="section-description">
    {$t(isOrg ? 'settings.quarantine.orgDescription' : 'settings.quarantine.description')}
  </p>
  <!-- §B.5 P1: scope copy names what quarantine hides — never "everywhere". -->
  <p class="scope-note">
    {$t(isOrg ? 'settings.quarantine.orgScopeNote' : 'settings.quarantine.scopeNote')}
  </p>

  <!-- Quarantine and legal hold are different features (issue #576): say so where they meet. -->
  <p class="scope-note hold-help" data-testid="quarantine-hold-help">
    {$t('settings.quarantine.holdHelp')}
    {#if !isOrg}
      <button
        type="button"
        class="action-link"
        data-testid="quarantine-open-holds"
        on:click={() => settingsModalStore.setActiveSection('platform-holds')}
      >
        {$t('settings.quarantine.holdHelpLink')}
      </button>
    {/if}
  </p>

  <div class="toolbar">
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
    {#if !isOrg}
      <button
        class="btn btn-primary"
        type="button"
        data-testid="quarantine-choose-files"
        on:click={() => (quarantining = true)}
      >
        {$t('settings.quarantine.quarantineFiles')}
      </button>
    {/if}
  </div>

  {#if loading && files.length === 0}
    <div class="settings-state-row">
      <Spinner size="small" />
      <span>{$t('common.loading')}</span>
    </div>
  {:else if loadFailed}
    <div class="settings-state-row error">
      <span>{$t('settings.quarantine.loadFailed')}</span>
      <button class="btn btn-secondary" on:click={() => load(offset)}>{$t('common.retry')}</button>
    </div>
  {:else if total === 0}
    <EmptyState
      title={$t('settings.quarantine.emptyTitle')}
      description={$t('settings.quarantine.emptyMessage')}
    />
  {:else}
    <div class="table-scroll">
    <table class="quarantine-table">
      <thead>
        <tr>
          <th>{$t('settings.quarantine.columnFile')}</th>
          <th>{$t('settings.quarantine.columnReason')}</th>
          <th>{$t('settings.quarantine.columnOwner')} / {$t('settings.quarantine.columnBy')}</th>
          <th>{$t('settings.quarantine.columnTakenDown')}</th>
          <th>{$t('settings.quarantine.columnHeldBy')}</th>
          <th>{$t('settings.quarantine.columnActions')}</th>
        </tr>
      </thead>
      <tbody>
        {#each files as file (file.uuid)}
          <tr>
            <td class="file-cell">
              <a
                href={resolve('/files/[id]', { id: file.uuid })}
                target="_blank"
                rel="noopener noreferrer"
                title={file.label}>{file.label}</a
              >
            </td>
            <td class="reason-cell">
              {#if file.reasonWithheld}
                {$t('settings.quarantine.operatorHeldReason')}
              {:else}
                {file.reason || '—'}
              {/if}
              <!-- One chip per notice record (a file can carry two); issue #1254. -->
              {#each file.notices as n (n.uuid)}
                <div class="notice-ref">
                  <span>{labelFor($t, TAKEDOWN_NOTICE_TYPE_KEYS, n.type)}</span>
                  <TakedownStatusChip status={n.status as 'open'} />
                </div>
              {/each}
            </td>
            <td class="people-cell">
              <div class="person" title={file.owner}>{file.owner}</div>
              <div class="person person-by" title={file.by || undefined}>{file.by || '—'}</div>
            </td>
            <td>{formatDateTime(file.quarantinedAt, '—', $locale)}</td>
            <td>
              <div class="badges-cell">
              {#if file.origin}
                <span class="badge badge-origin" class:badge-operator={file.origin === 'platform'}>
                  {$t(
                    file.origin === 'platform'
                      ? 'settings.quarantine.heldByOperator'
                      : 'settings.quarantine.heldByOrganization'
                  )}
                </span>
              {/if}
              {#if file.legalHold}
                <span class="badge badge-hold">{$t('settings.quarantine.legalHoldBadge')}</span>
              {/if}
              </div>
            </td>
            <td class="actions-cell">
              {#if !file.canRelease}
                <span class="action-disabled" title={$t('settings.quarantine.operatorOnlyRelease')}>
                  {$t('settings.quarantine.operatorOnlyRelease')}
                </span>
              {:else if file.isQuarantined}
                <button
                  class="action-link"
                  disabled={releasingUuid === file.uuid}
                  on:click={() => release(file, true)}
                >
                  {$t('settings.quarantine.release')}
                </button>
              {:else if file.legalHold}
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
    </div>

    <OffsetPager {offset} {total} pageSize={PAGE_SIZE} {loading} onpage={load} />
  {/if}
</div>

{#if !isOrg}
  <QuarantineModal
    isOpen={quarantining}
    chooseFiles
    on:quarantined={() => load(0)}
    on:close={() => (quarantining = false)}
  />
{/if}

<style>
  .quarantine-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .action-link {
    border: none;
    background: none;
    color: var(--primary-on-surface);
    cursor: pointer;
    padding: 0;
    font-size: var(--font-size-sm);
  }

  .hold-help .action-link {
    font-size: inherit;
    margin-inline-start: var(--space-1);
  }

  .scope-note {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin: 0;
  }

  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    margin: var(--space-2) 0;
  }

  .view-toggle {
    display: flex;
    gap: var(--space-1);
  }

  .toggle-btn {
    padding: var(--space-1-5) var(--space-3-5);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--bg-color);
    color: var(--text-color);
    cursor: pointer;
    font-size: var(--font-size-sm);
  }

  .toggle-btn.active {
    background: var(--primary-color);
    color: var(--text-on-solid);
    border-color: var(--accent-color);
  }

  .settings-state-row.error {
    color: #dc2626;
    justify-content: space-between;
  }

  .quarantine-table {
    width: 100%;
    border-collapse: collapse;
    overflow-wrap: normal;
    word-break: normal;
    font-size: var(--font-size-sm);
  }

  .quarantine-table th,
  .quarantine-table td {
    padding: var(--space-2);
    border-bottom: 1px solid var(--border-color);
    text-align: start;
    vertical-align: top;
  }

  .table-scroll {
    overflow-x: auto;
  }

  .quarantine-table th {
    white-space: nowrap;
  }

  .file-cell {
    min-width: 120px;
    max-width: 180px;
  }

  .file-cell a,
  .person {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .people-cell {
    max-width: 200px;
  }

  .person-by {
    color: var(--text-secondary);
  }

  .badges-cell {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-1);
  }

  .reason-cell {
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .notice-ref {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1-5);
    margin-top: var(--space-1);
    white-space: normal;
    font-size: var(--font-size-xs);
  }

  .badge {
    white-space: nowrap;
    display: inline-block;
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-pill);
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
  }

  .badge-hold {
    background: rgba(217, 119, 6, 0.15);
    color: #b45309;
  }

  :global([data-theme='dark']) .badge-hold {
    background: rgba(217, 119, 6, 0.25);
    color: #fbbf24;
  }

  .badge-origin {
    background: rgba(37, 99, 235, 0.12);
    color: #1d4ed8;
  }

  .badge-operator {
    background: rgba(100, 116, 139, 0.16);
    color: #475569;
  }

  :global([data-theme='dark']) .badge-origin {
    background: rgba(96, 165, 250, 0.22);
    color: #93c5fd;
  }

  :global([data-theme='dark']) .badge-operator {
    background: rgba(148, 163, 184, 0.24);
    color: #cbd5e1;
  }

  .action-disabled {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .actions-cell {
    display: flex;
    gap: var(--space-3);
    white-space: nowrap;
  }

  .action-link:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

</style>
