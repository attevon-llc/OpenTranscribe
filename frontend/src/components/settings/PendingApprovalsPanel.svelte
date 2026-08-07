<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import Badge from '../ui/Badge.svelte';
  import { UserApprovalsApi, isAlreadyDecided, type PendingAccount } from '$lib/api/userApprovals';

  /**
   * The account-approval queue (`GET /admin/user-approvals`, admin tier).
   *
   * Only fills while a super_admin has `require_account_approval` on
   * (Authentication → Local). With it off the queue is empty and the panel folds
   * itself away rather than occupying the Users screen with a permanent blank.
   */
  export let onDecided: () => void = () => {};

  const dispatch = createEventDispatcher<{ countchange: number }>();

  let accounts: PendingAccount[] = [];
  let loading = false;
  let loaded = false;
  /** UUID currently being decided — disables just that row's two buttons. */
  let deciding: string | null = null;

  onMount(load);

  export async function load() {
    loading = true;
    try {
      accounts = await UserApprovalsApi.list();
      dispatch('countchange', accounts.length);
    } catch (err: unknown) {
      accounts = [];
      toastStore.error(getErrorMessage(err, $t('settings.approvals.loadFailed')));
    } finally {
      loading = false;
      loaded = true;
    }
  }

  async function decide(account: PendingAccount, action: 'approve' | 'reject') {
    deciding = account.uuid;
    try {
      if (action === 'approve') {
        await UserApprovalsApi.approve(account.uuid);
        toastStore.success($t('settings.approvals.toast.approved', { email: account.email }));
      } else {
        await UserApprovalsApi.reject(account.uuid);
        toastStore.success($t('settings.approvals.toast.rejected', { email: account.email }));
      }
      onDecided();
    } catch (err: unknown) {
      // 409 means somebody else worked the queue first. Reporting it as a
      // generic failure would invite a retry that can only 409 again, so it
      // gets its own message — and the list is reloaded either way so the row
      // that is no longer pending disappears.
      if (isAlreadyDecided(err)) {
        toastStore.error($t('settings.approvals.toast.alreadyDecided', { email: account.email }));
      } else {
        toastStore.error(getErrorMessage(err, $t('settings.approvals.toast.decisionFailed')));
      }
    } finally {
      deciding = null;
      await load();
    }
  }

  function formatDate(value: string): string {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  }
</script>

{#if !loaded && loading}
  <div class="approvals-loading">{$t('common.loading')}</div>
{:else if accounts.length > 0}
  <section class="approvals">
    <div class="approvals-head">
      <h4>{$t('settings.approvals.title')}</h4>
      <Badge variant="warning">{accounts.length}</Badge>
    </div>
    <p class="approvals-note">{$t('settings.approvals.description')}</p>

    <div class="table-wrap">
      <table class="approvals-table">
        <thead>
          <tr>
            <th>{$t('settings.approvals.account')}</th>
            <th>{$t('settings.approvals.authType')}</th>
            <th>{$t('settings.approvals.requested')}</th>
            <th class="actions-col">{$t('common.actions')}</th>
          </tr>
        </thead>
        <tbody>
          {#each accounts as account (account.uuid)}
            <tr>
              <td>
                <span class="email">{account.email}</span>
                {#if account.full_name}
                  <span class="name">{account.full_name}</span>
                {/if}
                {#if !account.email_verified}
                  <!-- The case an approver most needs flagged: nobody has proved
                       control of this address. -->
                  <span class="flag">
                    <Badge variant="error">{$t('settings.approvals.unverifiedEmail')}</Badge>
                  </span>
                {/if}
              </td>
              <td><Badge variant="default">{account.auth_type}</Badge></td>
              <td class="requested">{formatDate(account.created_at)}</td>
              <td class="actions-col">
                <button
                  type="button"
                  class="btn btn-primary btn-row"
                  disabled={deciding === account.uuid}
                  on:click={() => decide(account, 'approve')}
                >
                  {$t('settings.approvals.approve')}
                </button>
                <button
                  type="button"
                  class="btn btn-danger btn-row"
                  disabled={deciding === account.uuid}
                  on:click={() => decide(account, 'reject')}
                >
                  {$t('settings.approvals.reject')}
                </button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </section>
{/if}

<style>
  .approvals-loading {
    padding: 1rem 0;
    color: var(--text-secondary);
    font-size: 0.875rem;
  }

  .approvals {
    margin-bottom: 1.5rem;
    padding: 1rem;
    border: 1px solid rgba(245, 158, 11, 0.4);
    border-radius: 8px;
    background: rgba(245, 158, 11, 0.08);
  }

  .approvals-head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .approvals-head h4 {
    margin: 0;
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .approvals-note {
    margin: 0.35rem 0 0.85rem 0;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .table-wrap {
    overflow-x: auto;
  }

  .approvals-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .approvals-table th {
    text-align: left;
    padding: 0.4rem 0.6rem;
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border-color);
    white-space: nowrap;
  }

  .approvals-table td {
    padding: 0.55rem 0.6rem;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-color);
    vertical-align: top;
  }

  .email {
    display: block;
    font-weight: 500;
    word-break: break-all;
  }

  .name {
    display: block;
    font-size: 0.75rem;
    color: var(--text-secondary);
  }

  .flag {
    display: inline-block;
    margin-top: 0.25rem;
  }

  .requested {
    white-space: nowrap;
    color: var(--text-secondary);
  }

  .actions-col {
    text-align: right;
    white-space: nowrap;
  }

  .btn-row {
    padding: 0.3rem 0.7rem;
    font-size: 0.75rem;
    margin-left: 0.35rem;
  }

  @media (max-width: 768px) {
    .btn-row {
      min-height: 40px;
    }
  }
</style>
