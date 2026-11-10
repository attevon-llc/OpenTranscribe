<script lang="ts">
  /**
   * Admin view of locked accounts (issue #570) — mounted inside the existing
   * `admin-users` settings section, beside `AccountStatusDashboard`. No new
   * `SettingsSection`, no `SECTION_MIN_ROLE` change: `admin-users` is already
   * `'admin'`.
   *
   * Three empty states, not one (§A.4.4): lockout DISABLED, lockout enabled
   * with NO locked accounts, and a FAILED read. Collapsing the first two is
   * the exact shape that has made a disabled control read as a healthy one
   * elsewhere in this repo; collapsing the third is the classic false-green.
   */
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import Spinner from '$components/ui/Spinner.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import { AdminApi, type LockedAccount } from '$lib/api/admin';

  let accounts: LockedAccount[] = [];
  let nextCursor: string | null = null;
  let lockoutEnabled = true;
  let storeBackend: 'redis' | 'memory' = 'redis';
  let loading = false;
  let loadFailed = false;
  let resettingIdentifier: string | null = null;

  async function loadPage(cursor: string | null = null, append = false) {
    loading = true;
    loadFailed = false;
    try {
      const result = await AdminApi.listLockedAccounts({ cursor, limit: 50 });
      accounts = append ? [...accounts, ...result.accounts] : result.accounts;
      nextCursor = result.next_cursor;
      lockoutEnabled = result.lockout_enabled;
      storeBackend = result.store_backend;
    } catch (err) {
      loadFailed = true;
      toastStore.error(getErrorMessage(err, $t('settings.lockedAccounts.loadFailed')));
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    loadPage();
  });

  const confirmDialog = createConfirmDialog();
  const { isOpen: confirmOpen, title: confirmTitle, message: confirmMessage } = confirmDialog;

  function resetCounter(account: LockedAccount) {
    confirmDialog.request(
      $t('settings.lockedAccounts.resetCounter'),
      $t('settings.lockedAccounts.confirmReset', { identifier: account.identifier }),
      () => doResetCounter(account)
    );
  }

  async function doResetCounter(account: LockedAccount) {
    resettingIdentifier = account.identifier;
    try {
      await AdminApi.resetLockoutCounter(account.identifier);
      toastStore.success($t('settings.lockedAccounts.resetSuccess', { identifier: account.identifier }));
      await loadPage();
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.lockedAccounts.resetFailed')));
    } finally {
      resettingIdentifier = null;
    }
  }

  function unlockNow(account: LockedAccount) {
    if (!account.user_uuid) return;
    confirmDialog.request(
      $t('settings.lockedAccounts.unlockNow'),
      $t('settings.lockedAccounts.confirmUnlock', { identifier: account.identifier }),
      () => doUnlock(account)
    );
  }

  async function doUnlock(account: LockedAccount) {
    if (!account.user_uuid) return;
    resettingIdentifier = account.identifier;
    try {
      await AdminApi.unlockAccount(account.user_uuid);
      toastStore.success($t('settings.lockedAccounts.unlockSuccess', { identifier: account.identifier }));
      await loadPage();
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.lockedAccounts.unlockFailed')));
    } finally {
      resettingIdentifier = null;
    }
  }
</script>

<div class="locked-accounts-panel">
  <h4 class="panel-title">{$t('settings.lockedAccounts.title')}</h4>

  {#if loading && accounts.length === 0}
    <div class="state-row">
      <Spinner size="small" />
      <span>{$t('common.loading')}</span>
    </div>
  {:else if loadFailed}
    <div class="state-row error">
      <span>{$t('settings.lockedAccounts.loadFailed')}</span>
      <button class="btn btn-secondary" on:click={() => loadPage()}>{$t('common.retry')}</button>
    </div>
  {:else if !lockoutEnabled}
    <div class="state-row disabled">
      <span>{$t('settings.lockedAccounts.lockoutDisabled')}</span>
    </div>
  {:else}
    {#if storeBackend === 'memory'}
      <div class="degraded-banner">
        {$t('settings.lockedAccounts.degradedStoreBanner')}
      </div>
    {/if}

    {#if accounts.length === 0}
      <div class="state-row empty">
        <span>{$t('settings.lockedAccounts.noneLocked')}</span>
      </div>
    {:else}
      <table class="locked-accounts-table">
        <thead>
          <tr>
            <th>{$t('settings.lockedAccounts.columnIdentifier')}</th>
            <th>{$t('settings.lockedAccounts.columnAccount')}</th>
            <th>{$t('settings.lockedAccounts.columnLockedUntil')}</th>
            <th>{$t('settings.lockedAccounts.columnActions')}</th>
          </tr>
        </thead>
        <tbody>
          {#each accounts as account (account.identifier)}
            <tr>
              <!-- Attacker-supplied strings in an admin UI: rendered as plain
                   text data, never as a link (J-A3). -->
              <td class="identifier-cell">{account.identifier}</td>
              <td>
                {#if account.user_uuid}
                  {account.full_name || account.identifier}
                {:else}
                  <span class="no-match">{$t('settings.lockedAccounts.noMatchingAccount')}</span>
                {/if}
              </td>
              <td>{account.locked_until || '—'}</td>
              <td class="actions-cell">
                <button
                  class="action-link"
                  disabled={resettingIdentifier === account.identifier}
                  on:click={() => resetCounter(account)}
                >
                  {$t('settings.lockedAccounts.resetCounter')}
                </button>
                {#if account.user_uuid}
                  <button
                    class="action-link"
                    disabled={resettingIdentifier === account.identifier}
                    on:click={() => unlockNow(account)}
                  >
                    {$t('settings.lockedAccounts.unlockNow')}
                  </button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>

      {#if nextCursor}
        <button class="btn btn-secondary load-more-btn" disabled={loading} on:click={() => loadPage(nextCursor, true)}>
          {loading ? $t('common.loading') : $t('common.loadMore')}
        </button>
      {/if}
    {/if}
  {/if}
</div>

<ConfirmationModal
  bind:isOpen={$confirmOpen}
  title={$confirmTitle}
  message={$confirmMessage}
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<style>
  .locked-accounts-panel {
    margin-top: var(--space-6);
  }

  .panel-title {
    margin: 0 0 var(--space-3);
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-semibold);
  }

  .state-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-3);
    border-radius: var(--radius-md);
    background: var(--bg-secondary);
    font-size: var(--font-size-sm);
  }

  .state-row.error {
    color: #dc2626;
    justify-content: space-between;
  }

  .state-row.disabled {
    color: var(--text-secondary);
  }

  .degraded-banner {
    padding: var(--space-2) var(--space-3);
    margin-bottom: var(--space-3);
    border-radius: var(--radius-md);
    background: rgba(217, 119, 6, 0.12);
    color: #b45309;
    font-size: var(--font-size-sm);
  }

  :global([data-theme='dark']) .degraded-banner {
    background: rgba(217, 119, 6, 0.2);
    color: #fbbf24;
  }

  .locked-accounts-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-sm);
  }

  .locked-accounts-table th,
  .locked-accounts-table td {
    padding: var(--space-2) var(--space-2);
    border-bottom: 1px solid var(--border-color);
    text-align: left;
  }

  .identifier-cell {
    word-break: break-all;
  }

  .no-match {
    font-style: italic;
    color: var(--text-secondary);
  }

  .actions-cell {
    display: flex;
    gap: var(--space-3);
  }

  .action-link {
    border: none;
    background: none;
    color: var(--primary-on-surface);
    cursor: pointer;
    padding: 0;
    font-size: var(--font-size-sm);
  }

  .action-link:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .load-more-btn {
    margin-top: var(--space-3);
  }
</style>
