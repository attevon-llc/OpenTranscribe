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

  async function resetCounter(account: LockedAccount) {
    if (!confirm($t('settings.lockedAccounts.confirmReset', { identifier: account.identifier }))) {
      return;
    }
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

  async function unlockNow(account: LockedAccount) {
    if (!account.user_uuid) return;
    if (!confirm($t('settings.lockedAccounts.confirmUnlock', { identifier: account.identifier }))) {
      return;
    }
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
      <button class="retry-btn" on:click={() => loadPage()}>{$t('common.retry')}</button>
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
        <button class="load-more-btn" disabled={loading} on:click={() => loadPage(nextCursor, true)}>
          {loading ? $t('common.loading') : $t('common.loadMore')}
        </button>
      {/if}
    {/if}
  {/if}
</div>

<style>
  .locked-accounts-panel {
    margin-top: 1.5rem;
  }

  .panel-title {
    margin: 0 0 0.75rem;
    font-size: 0.9375rem;
    font-weight: 600;
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

  .state-row.disabled {
    color: var(--text-secondary, var(--text-color));
  }

  .retry-btn {
    padding: 0.25rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: transparent;
    color: inherit;
    cursor: pointer;
  }

  .degraded-banner {
    padding: 0.5rem 0.75rem;
    margin-bottom: 0.75rem;
    border-radius: 6px;
    background: rgba(217, 119, 6, 0.12);
    color: #b45309;
    font-size: 0.8125rem;
  }

  :global([data-theme='dark']) .degraded-banner {
    background: rgba(217, 119, 6, 0.2);
    color: #fbbf24;
  }

  .locked-accounts-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .locked-accounts-table th,
  .locked-accounts-table td {
    padding: 0.5rem 0.5rem;
    border-bottom: 1px solid var(--border-color);
    text-align: left;
  }

  .identifier-cell {
    word-break: break-all;
  }

  .no-match {
    font-style: italic;
    color: var(--text-secondary, var(--text-color));
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

  .load-more-btn {
    margin-top: 0.75rem;
    padding: 0.375rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-color);
    color: var(--text-color);
    cursor: pointer;
    font-size: 0.8125rem;
  }
</style>
